import { QueryClient } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { refreshRemoteView, reconnectOutcome } from './remoteRecovery'
import type { Integration } from './models'

describe('Remote wake reads', () => {
  let client: QueryClient
  let fetchMock: ReturnType<typeof vi.fn>
  beforeEach(() => {
    vi.useFakeTimers()
    client = new QueryClient()
    fetchMock = vi.fn(async (url: string) => new Response(JSON.stringify({ data: url.endsWith('/bootstrap') ? { activeRemoteId: 'remote-b' } : [] })))
    vi.stubGlobal('fetch', fetchMock)
  })
  afterEach(() => { client.clear(); vi.unstubAllGlobals(); vi.useRealTimers() })

  it.each([
    [0, ['/bootstrap', '/status', '/integrations']],
    [1, ['/bootstrap', '/status', '/diagnostics/orphaned-entities', '/diagnostics/unused-activity-entities']],
    [2, ['/bootstrap', '/status', '/system-messages?markRead=false']],
  ] as const)('refreshes only page %s with GET requests and respects the newly selected Remote', async (page, paths) => {
    client.setQueryData(['bootstrap'], { activeRemoteId: 'remote-a' })
    await refreshRemoteView(client, () => page, () => true)
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(paths.map(path => `/api/v1${path}`))
    expect(fetchMock.mock.calls.every(([, init]) => !init.method || init.method === 'GET')).toBe(true)
    expect(client.getQueryData(['remote-view', 'remote-b', 'status'])).toBeDefined()
    expect(client.getQueryData(['remote-view', 'remote-a', 'status'])).toBeUndefined()
  })

  it('uses the page displayed after bootstrap finishes', async () => {
    let page = 0
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith('/bootstrap')) page = 2
      return new Response(JSON.stringify({ data: url.endsWith('/bootstrap') ? { activeRemoteId: 'remote-b' } : [] }))
    })
    await refreshRemoteView(client, () => page, () => true)
    expect(fetchMock.mock.calls.map(([url]) => url)).toContain('/api/v1/system-messages?markRead=false')
    expect(fetchMock.mock.calls.map(([url]) => url)).not.toContain('/api/v1/integrations')
  })

  it('retries transient Wi-Fi failures with backoff, then stops after a bounded number of attempts', async () => {
    fetchMock.mockRejectedValue(new TypeError('Network unavailable'))
    const result = refreshRemoteView(client, () => 0, () => true).catch(error => error)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(await result).toBeInstanceOf(TypeError)
    expect(fetchMock).toHaveBeenCalledTimes(5)
    expect(fetchMock.mock.calls.every(([url]) => url === '/api/v1/bootstrap')).toBe(true)
  })

  it('recovers after Wi-Fi becomes available without repeating any mutation', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Wi-Fi waking'))
    const result = refreshRemoteView(client, () => 0, () => true)
    await vi.advanceTimersByTimeAsync(600)
    await result
    expect(fetchMock).toHaveBeenCalledTimes(4)
    expect(fetchMock.mock.calls.every(([, init]) => !init.method || init.method === 'GET')).toBe(true)
  })

  it('does not retry authentication errors', async () => {
    fetchMock.mockResolvedValue(new Response('{}', { status: 401 }))
    await expect(refreshRemoteView(client, () => 0, () => true)).rejects.toThrow('Request failed')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('does not start recovery while hidden', async () => {
    await refreshRemoteView(client, () => 0, () => false)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('stops retries and subsequent reads when hidden or unmounted during recovery', async () => {
    let active = true
    fetchMock.mockImplementation(async () => { active = false; throw new TypeError('Offline') })
    await expect(refreshRemoteView(client, () => 0, () => active)).rejects.toThrow('Offline')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})

describe('Update outcome after standby', () => {
  const item = { connectionState: 'connected', updateAvailable: false } as Integration
  it('confirms a completed update even when waking after the deadline', () => {
    expect(reconnectOutcome(item, 300_000, 10_000, 70_000)).toBe('connected')
  })
  it('does not declare an outcome from stale pre-update data', () => {
    expect(reconnectOutcome(item, 11_000, 10_000, 70_000)).toBeNull()
    expect(reconnectOutcome({ ...item, connectionState: 'disconnected' }, 30_000, 10_000, 70_000)).toBeNull()
  })
  it('ends reconnecting only after a fresh unsuccessful check reaches the wall-clock deadline', () => {
    expect(reconnectOutcome(undefined, 300_000, 10_000, 70_000)).toBe('unconfirmed')
    expect(reconnectOutcome({ ...item, updateAvailable: true }, 70_000, 10_000, 70_000)).toBe('unconfirmed')
  })
})

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { observeRemoteLifecycle, type DeviceHost } from './remoteLifecycle'

class Page extends EventTarget { visibilityState = 'visible' }
class Host extends EventTarget implements DeviceHost { screen = 'on'; powerProfile = 'balanced' }

describe('Remote page lifecycle', () => {
  let page: Page
  let host: Host
  let dispose: (() => void) | undefined
  beforeEach(() => { vi.useFakeTimers(); page = new Page(); host = new Host() })
  afterEach(() => { dispose?.(); vi.useRealTimers() })
  const event = (target: EventTarget, name: string) => target.dispatchEvent(new Event(name))
  const observe = (wake = vi.fn(async () => {}), device: Host | null = host) => {
    const active = vi.fn()
    dispose = observeRemoteLifecycle(page as unknown as Document, device ?? undefined, active, wake)
    return { active, wake }
  }

  it('reads initial state without triggering an extra startup refresh', async () => {
    host.screen = 'off'
    const { active, wake } = observe()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(active).toHaveBeenLastCalledWith(false)
    expect(wake).not.toHaveBeenCalled()
  })

  it('coalesces document and host resume, screen and visibility events into one refresh', async () => {
    const { active, wake } = observe()
    event(page, 'freeze'); event(host, 'suspend')
    host.screen = 'off'; event(host, 'screenchange')
    page.visibilityState = 'hidden'; event(page, 'visibilitychange')
    event(page, 'resume'); event(host, 'resume')
    expect(active).toHaveBeenLastCalledWith(false)
    await vi.advanceTimersByTimeAsync(200)
    expect(wake).not.toHaveBeenCalled()
    host.screen = 'on'; event(host, 'screenchange')
    page.visibilityState = 'visible'; event(page, 'visibilitychange')
    await vi.advanceTimersByTimeAsync(200)
    expect(active).toHaveBeenLastCalledWith(true)
    expect(wake).toHaveBeenCalledTimes(1)
  })

  it('recovers using only page visibility in ordinary browsers', async () => {
    const { wake } = observe(vi.fn(async () => {}), null)
    page.visibilityState = 'hidden'; event(page, 'visibilitychange')
    page.visibilityState = 'visible'; event(page, 'visibilitychange')
    await vi.advanceTimersByTimeAsync(200)
    expect(wake).toHaveBeenCalledTimes(1)
  })

  it('does not treat dimming as sleep or refresh on unrelated visibility events', async () => {
    const { active, wake } = observe()
    host.screen = 'dim'; event(host, 'screenchange')
    event(page, 'visibilitychange')
    await vi.advanceTimersByTimeAsync(200)
    expect(active).toHaveBeenCalledTimes(1)
    expect(active).toHaveBeenLastCalledWith(true)
    expect(wake).not.toHaveBeenCalled()
  })

  it('cancels a scheduled recovery if hidden again, and removes listeners on disposal', async () => {
    const { wake } = observe()
    event(page, 'freeze'); event(page, 'resume')
    page.visibilityState = 'hidden'; event(page, 'visibilitychange')
    await vi.advanceTimersByTimeAsync(200)
    expect(wake).not.toHaveBeenCalled()
    page.visibilityState = 'visible'; event(page, 'visibilitychange')
    dispose?.()
    event(host, 'resume')
    await vi.advanceTimersByTimeAsync(200)
    expect(wake).not.toHaveBeenCalled()
  })

  it('serializes recovery across another sleep while a read is in flight', async () => {
    let finish!: () => void
    const wake = vi.fn().mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve })).mockResolvedValue(undefined)
    observe(wake)
    event(host, 'suspend'); event(host, 'resume')
    await vi.advanceTimersByTimeAsync(200)
    event(host, 'resume') // duplicate while recovering
    expect(wake).toHaveBeenCalledTimes(1)
    event(host, 'suspend'); event(host, 'resume') // a new wake must still refresh
    finish()
    await vi.advanceTimersByTimeAsync(200)
    expect(wake).toHaveBeenCalledTimes(2)
  })
})

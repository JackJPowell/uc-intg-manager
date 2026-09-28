import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyText } from './clipboard'

afterEach(() => vi.unstubAllGlobals())

function mockLegacyClipboard(result: boolean) {
  const field = { value: '', readOnly: false, style: { position: '', left: '' }, select: vi.fn(), remove: vi.fn() }
  const appendChild = vi.fn()
  const execCommand = vi.fn().mockReturnValue(result)
  vi.stubGlobal('document', { createElement: vi.fn().mockReturnValue(field), body: { appendChild }, execCommand })
  return { field, appendChild, execCommand }
}

describe('copyText', () => {
  it('copies on an HTTP page where navigator.clipboard is unavailable', async () => {
    vi.stubGlobal('navigator', {})
    const { field, appendChild, execCommand } = mockLegacyClipboard(true)

    expect(await copyText('{"backup":true}')).toBe(true)
    expect(field.value).toBe('{"backup":true}')
    expect(appendChild).toHaveBeenCalledWith(field)
    expect(field.select).toHaveBeenCalledOnce()
    expect(execCommand).toHaveBeenCalledWith('copy')
    expect(field.remove).toHaveBeenCalledOnce()
  })

  it('uses the Clipboard API when available', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    const { execCommand } = mockLegacyClipboard(true)

    expect(await copyText('backup data')).toBe(true)
    expect(writeText).toHaveBeenCalledWith('backup data')
    expect(execCommand).not.toHaveBeenCalled()
  })

  it('reports failure when both clipboard methods are blocked', async () => {
    vi.stubGlobal('navigator', { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
    const { field } = mockLegacyClipboard(false)

    expect(await copyText('backup data')).toBe(false)
    expect(field.remove).toHaveBeenCalledOnce()
  })
})

/** Optional UC WebView API. Battery information remains server-backed. */
export interface DeviceHost extends EventTarget {
  readonly screen: string
  readonly powerProfile: string
}

declare global {
  interface Window { readonly deviceHost?: DeviceHost }
}

/** Observe both ordinary browsers and WebViews without changing global query focus. */
export function observeRemoteLifecycle(
  document: Document,
  host: DeviceHost | undefined,
  onActiveChange: (active: boolean) => void,
  onWake: (isActive: () => boolean) => Promise<void>,
) {
  let disposed = false
  let suspended = false
  let active = document.visibilityState !== 'hidden' && host?.screen !== 'off'
  let timer: ReturnType<typeof setTimeout> | undefined
  let running = false
  let pending = false
  const isActive = () => active && !disposed
  const schedule = () => {
    if (!isActive() || running) return
    clearTimeout(timer)
    // resume, screenchange and visibilitychange arrive together on wake.
    timer = setTimeout(async () => {
      timer = undefined
      if (!isActive()) return
      pending = false
      running = true
      try { await onWake(isActive) }
      finally {
        running = false
        if (pending) schedule()
      }
    }, 150)
  }
  const update = (wake = false) => {
    const next = !suspended && document.visibilityState !== 'hidden' && host?.screen !== 'off'
    const becameActive = next && !active
    if (next !== active) { active = next; onActiveChange(active) }
    if (!next) { clearTimeout(timer); timer = undefined; return }
    if (becameActive || (wake && !running)) { pending = true; schedule() }
  }
  const freeze = () => { suspended = true; update() }
  const resume = () => { suspended = false; update(true) }
  const changed = () => update()
  document.addEventListener('visibilitychange', changed)
  document.addEventListener('freeze', freeze)
  document.addEventListener('resume', resume)
  host?.addEventListener('suspend', freeze)
  host?.addEventListener('resume', resume)
  host?.addEventListener('screenchange', changed)
  onActiveChange(active)
  return () => {
    disposed = true
    clearTimeout(timer)
    document.removeEventListener('visibilitychange', changed)
    document.removeEventListener('freeze', freeze)
    document.removeEventListener('resume', resume)
    host?.removeEventListener('suspend', freeze)
    host?.removeEventListener('resume', resume)
    host?.removeEventListener('screenchange', changed)
  }
}

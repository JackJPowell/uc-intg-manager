import { useEffect, useRef, useState } from 'react'
import { observeRemoteLifecycle } from './remoteLifecycle'

export function useRemoteLifecycle(onWake: (isActive: () => boolean) => Promise<void>) {
  const callback = useRef(onWake)
  callback.current = onWake
  const [state, setState] = useState(() => ({
    active: typeof document === 'undefined' || (document.visibilityState !== 'hidden' && window.deviceHost?.screen !== 'off'),
    recovering: false,
  }))
  useEffect(() => {
    let mounted = true
    const dispose = observeRemoteLifecycle(document, window.deviceHost, active => {
      setState(previous => ({ active, recovering: previous.recovering || (active && !previous.active) }))
    }, async isActive => {
      setState(previous => ({ ...previous, recovering: true }))
      try { await callback.current(isActive) }
      catch { /* The query observers display read errors. */ }
      finally { if (mounted) setState(previous => ({ ...previous, recovering: false })) }
    })
    return () => { mounted = false; dispose() }
  }, [])
  return state
}

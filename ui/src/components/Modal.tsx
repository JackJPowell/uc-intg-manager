import { X } from 'lucide-react'
import { createPortal } from 'react-dom'
import { useEffect } from 'react'
import type { PropsWithChildren } from 'react'

export function Modal({ title, close, children }: PropsWithChildren<{ title: string; close: () => void }>) {
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => { if (event.key === 'Escape') close() }
    document.addEventListener('keydown', keydown)
    return () => document.removeEventListener('keydown', keydown)
  }, [close])
  return createPortal(<div className="modal-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) close() }}><section className="app-modal" role="dialog" aria-modal="true" aria-label={title}><header><h2>{title}</h2><button className="icon-action" type="button" title="Close dialog" aria-label="Close dialog" onClick={close}><X /></button></header>{children}</section></div>, document.body)
}

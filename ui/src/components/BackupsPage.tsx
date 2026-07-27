import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, CloudUpload, Copy, Download, Eye, RefreshCw, Trash2, TriangleAlert, X } from 'lucide-react'
import { useRef, useState } from 'react'
import { api } from '../lib/api'

function JsonInspector({ driverId, content, close }: { driverId: string; content: unknown; close: () => void }) {
  const [copied, setCopied] = useState(false)
  const text = JSON.stringify(content, null, 2)
  const copy = async () => { await navigator.clipboard.writeText(text); setCopied(true); window.setTimeout(() => setCopied(false), 1600) }
  return <section className="backup-inspector"><header><div><p className="eyebrow">Configuration snapshot</p><h2>{driverId}</h2><p>{text.split('\n').length} formatted lines · read-only</p></div><div className="action-row"><button className="icon-action" type="button" aria-label="Copy backup JSON" onClick={copy}>{copied ? <Check /> : <Copy />}</button><button className="icon-action" type="button" aria-label="Close backup inspector" onClick={close}><X /></button></div></header><div className="json-shell"><div className="json-gutter" aria-hidden="true">{text.split('\n').map((_, index) => <span key={index}>{index + 1}</span>)}</div><pre><code>{text}</code></pre></div></section>
}

export function BackupsPage() {
  const queryClient = useQueryClient()
  const uploadInput = useRef<HTMLInputElement>(null)
  const [selected, setSelected] = useState<string>()
  const list = useQuery({ queryKey: ['backups'], queryFn: api.backups })
  const detail = useQuery({ queryKey: ['backup', selected], queryFn: () => api.backup(selected!), enabled: Boolean(selected) })
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['backups'] })
  const backupAll = useMutation({ mutationFn: api.backupAll, onSuccess: refresh })
  const remove = useMutation({ mutationFn: api.deleteBackup, onSuccess: (_, id) => { if (selected === id) setSelected(undefined); refresh() } })
  const restore = useMutation({ mutationFn: api.importBackups, onSuccess: refresh })
  const error = list.error ?? detail.error ?? backupAll.error ?? remove.error ?? restore.error
  return <section className="backups-workspace"><header className="backups-hero"><div><p className="eyebrow">Recovery</p><h1>Backups</h1><p>Inspect, export, and restore stored integration configuration.</p></div><div className="action-row"><a className="icon-action" href="/api/v1/backups/export" aria-label="Export all backups"><Download /></a><input ref={uploadInput} className="sr-only" type="file" accept="application/json" onChange={event => { const file = event.target.files?.[0]; if (file) restore.mutate(file); event.currentTarget.value = '' }} /><button className="icon-action" type="button" aria-label="Import backup file" onClick={() => uploadInput.current?.click()} disabled={restore.isPending}><CloudUpload /></button><button className="refresh-button" type="button" onClick={() => backupAll.mutate()} disabled={backupAll.isPending}><RefreshCw className={backupAll.isPending ? 'spin' : ''} /> Back up all</button></div></header>{error && <div className="notice error"><TriangleAlert /> {error.message}</div>}{backupAll.data && <div className="notice success">Backed up {backupAll.data.successful} integration{backupAll.data.successful === 1 ? '' : 's'}; {backupAll.data.failed} failed.</div>}
    <div className="backup-grid">{(list.data ?? []).map(item => <article className={`backup-tile ${selected === item.driverId ? 'selected' : ''}`} key={item.driverId}><button className="backup-tile-main" type="button" onClick={() => setSelected(item.driverId)}><span className="backup-tile-mark"><ArchiveIcon /></span><span><strong>{item.driverId}</strong><small>{item.timestamp ?? 'Unknown capture time'}</small></span></button><div className="action-row"><button className="icon-action" type="button" aria-label={`View ${item.driverId} backup`} onClick={() => setSelected(item.driverId)}><Eye /></button><button className="icon-action destructive" type="button" aria-label={`Delete ${item.driverId} backup`} onClick={() => { if (window.confirm(`Delete backup for ${item.driverId}?`)) remove.mutate(item.driverId) }}><Trash2 /></button></div></article>)}</div>{!list.isLoading && !list.data?.length && <div className="empty-state"><h2>No backups yet</h2><p>Create a complete backup to protect the current configuration.</p></div>}{selected && <JsonInspector driverId={selected} content={detail.data?.content ?? {}} close={() => setSelected(undefined)} />}</section>
}
function ArchiveIcon() { return <span aria-hidden="true">⌁</span> }

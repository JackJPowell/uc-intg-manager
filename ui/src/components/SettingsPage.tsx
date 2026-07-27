import { useMutation, useQuery } from '@tanstack/react-query'
import { ArchiveRestore, Download, LockKeyhole, RefreshCw, Save, Server, Settings2, TriangleAlert, Upload } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import type { SettingsPayload } from '../lib/models'

const sections: Array<{ title: string; detail: string; fields: Array<[keyof SettingsPayload['settings'], string, string]> }> = [
  { title: 'Manager availability', detail: 'Control how the manager behaves when the Remote leaves its dock.', fields: [['shutdown_on_battery', 'Pause on battery', 'Stop manager operations while the Remote is undocked to conserve battery.']] },
  { title: 'Updates', detail: 'Choose the safety defaults used when new integration versions arrive.', fields: [['auto_update', 'Automatically update integrations', 'Only eligible integrations with safe backup support are updated.' ], ['auto_register_entities', 'Re-register configured entities', 'Restore entities that were configured before an integration update.' ], ['show_beta_releases', 'Show beta releases', 'Include pre-release versions in version selection.']] },
  { title: 'Backups', detail: 'Keep recoverable configuration snapshots on a predictable schedule.', fields: [['backup_configs', 'Automatically back up configuration', 'Capture configuration for supported installed integrations.']] },
]

export function SettingsPage() {
  const query = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  const [values, setValues] = useState<SettingsPayload | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const save = useMutation({ mutationFn: api.saveSettings, onSuccess: setValues })
  const lock = useQuery({ queryKey: ['operation-lock'], queryFn: api.operationLock, refetchInterval: 10_000 })
  const release = useMutation({ mutationFn: api.releaseOperationLock, onSuccess: () => lock.refetch() })
  const restore = useMutation({ mutationFn: api.importBackups })
  useEffect(() => { if (query.data) setValues(query.data) }, [query.data])
  if (!values) return <div className="loading-grid">Loading manager settings…</div>
  const runtime = values.runtime ?? { remoteAddress: null, webServerPort: 9999, external: false }
  const update = (key: keyof SettingsPayload['settings'], value: boolean | string) => setValues({ ...values, settings: { ...values.settings, [key]: value } })
  return <section className="settings-workspace">
    <header className="settings-hero"><div><p className="eyebrow">Manager behavior</p><h1>Settings</h1><p>Set the maintenance policy, recovery schedule, and operational safeguards for this manager.</p></div><button className="refresh-button" type="button" onClick={() => save.mutate(values)} disabled={save.isPending}><Save /> {save.isPending ? 'Saving…' : 'Save settings'}</button></header>
    {(save.isError || restore.isError || release.isError) && <div className="notice error"><TriangleAlert /> {(save.error ?? restore.error ?? release.error)?.message}</div>}
    <div className="settings-layout"><div className="settings-main">{sections.map(section => <article className="settings-section" key={section.title}><header><h2>{section.title}</h2><p>{section.detail}</p></header><div>{section.fields.map(([key,label,description]) => <label className="policy-row" key={key}><span><strong>{label}</strong><small>{key === 'shutdown_on_battery' && runtime.external ? 'Not applicable on an external host; the web server remains available.' : description}</small></span><span className="switch"><input type="checkbox" disabled={key === 'shutdown_on_battery' && runtime.external} checked={Boolean(values.settings[key])} onChange={event => update(key,event.target.checked)} /><span /></span></label>)}</div>{section.title === 'Backups' && <label className="backup-time"><span><strong>Daily backup time</strong><small>Local time used for scheduled backup work.</small></span><input type="time" value={values.settings.backup_time} onChange={event => update('backup_time',event.target.value)} /></label>}</article>)}</div>
      <aside className="settings-aside"><article><header><ArchiveRestore /><div><h2>Backup & restore</h2><p>Move the manager safely between installations.</p></div></header><a className="primary-action" href="/api/v1/backups/export"><Download /> Export complete backup</a><input ref={input} className="sr-only" type="file" accept="application/json" onChange={event => { const file=event.target.files?.[0]; if(file) restore.mutate(file); event.currentTarget.value='' }} /><button className="secondary-action" type="button" onClick={() => input.current?.click()} disabled={restore.isPending}><Upload /> {restore.isPending ? 'Importing…' : 'Import backup file'}</button></article><article><header><Server /><div><h2>Connection</h2><p>Current manager endpoint.</p></div></header><dl><div><dt>Remote</dt><dd>{runtime.remoteAddress ?? 'Not configured'}</dd></div><div><dt>Manager port</dt><dd>{runtime.webServerPort}</dd></div><div><dt>Runtime</dt><dd>{runtime.external ? 'External host' : 'Remote bundle'}</dd></div></dl></article><article className="recovery"><header><LockKeyhole /><div><h2>Operation recovery</h2><p>Use only if an interrupted install or update left the manager locked.</p></div></header><p className={lock.data?.locked ? 'lock-state active' : 'lock-state'}>{lock.data?.locked ? `An operation is locked${lock.data.elapsedSeconds ? ` for ${lock.data.elapsedSeconds}s` : ''}.` : 'No operation lock is active.'}</p><button className="secondary-action" type="button" disabled={!lock.data?.locked || release.isPending} onClick={() => release.mutate()}><RefreshCw className={release.isPending ? 'spin' : ''} /> Resume operations</button></article></aside></div>
  </section>
}

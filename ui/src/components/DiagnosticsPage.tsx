import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, ChevronRight, CircleCheck, Cpu, ExternalLink, Link2, Power, RefreshCw, ShieldAlert, Trash2, TriangleAlert, Wrench } from 'lucide-react'
import { useState } from 'react'
import { api, type DiagnosticActivityResult, type DiagnosticEntity, type IrCodeset } from '../lib/api'
import { Modal } from './Modal'

function EntityReference({ entity }: { entity: DiagnosticEntity }) {
  const [showId, setShowId] = useState(false)
  const entityId = String(entity.entity_id ?? entity.id ?? '')
  const name = String((entity.localized_name ?? entityId) || 'Unknown entity')
  return <li className={showId ? 'entity-reference id-visible' : 'entity-reference'}><div><span>{name}</span>{entity.integration?.localized_name && <small>{entity.integration.localized_name}</small>} {entityId && <button type="button" className="entity-id-toggle" aria-expanded={showId} onClick={() => setShowId(value => !value)}>{showId ? 'Hide ID' : 'ID'}</button>}</div>{showId && <code>{entityId}</code>}</li>
}

function ScanResult({ result, empty, warning }: { result?: DiagnosticActivityResult; empty: string; warning: string }) {
  if (!result) return null
  const activities = Object.entries(result.activities)
  if (!activities.length) return <div className="diagnostics-clear"><CircleCheck />{empty}</div>
  return <div className="diagnostics-results">{activities.map(([id, activity]) => <div className="diagnostics-activity" key={id}><div><strong>{activity.name}</strong><span>{activity.entities.length} {activity.entities.length === 1 ? 'entity' : 'entities'}</span></div><ul>{activity.entities.map((entity, index) => <EntityReference entity={entity} key={`${entity.id ?? entity.entity_id ?? index}`} />)}</ul></div>)}<p className="diagnostics-help"><ShieldAlert />{warning}</p></div>
}

function ScanPanel({ title, description, queryKey, load, empty, warning, tone }: { title: string; description: string; queryKey: string; load: () => Promise<DiagnosticActivityResult>; empty: string; warning: string; tone: 'danger' | 'warning' }) {
  const scan = useQuery({ queryKey: ['diagnostics', queryKey], queryFn: load })
  return <article className={`diagnostics-panel ${tone}`}><header><div className="diagnostics-panel-icon"><Activity /></div><div><h2>{title}</h2><p>{description}</p></div><button className="secondary-action" type="button" onClick={() => scan.refetch()} disabled={scan.isFetching}><RefreshCw className={scan.isFetching ? 'spin' : ''} /> Scan</button></header>{scan.isError ? <div className="notice error"><TriangleAlert />{scan.error.message}</div> : <ScanResult result={scan.data} empty={empty} warning={warning} />}</article>
}

function codesetId(codeset: IrCodeset) { return String(codeset.device_id ?? codeset.id ?? '') }
function codesetName(codeset: IrCodeset) { return String(codeset.device_name ?? codeset.name ?? (codesetId(codeset) || 'Unnamed codeset')) }

export function DiagnosticsPage() {
  const client = useQueryClient()
  const [reassociate, setReassociate] = useState<string | null>(null)
  const [remoteName, setRemoteName] = useState('')
  const [confirmPowerOff, setConfirmPowerOff] = useState(false)
  const [codesetToDelete, setCodesetToDelete] = useState<IrCodeset | null>(null)
  const firmware = useQuery({ queryKey: ['diagnostics', 'firmware'], queryFn: api.checkFirmware })
  const codesets = useQuery({ queryKey: ['diagnostics', 'ir-codesets'], queryFn: api.orphanedIrCodesets })
  const removeCodeset = useMutation({ mutationFn: api.deleteIrCodeset, onSuccess: () => { setCodesetToDelete(null); return client.invalidateQueries({ queryKey: ['diagnostics', 'ir-codesets'] }) } })
  const attachCodeset = useMutation({ mutationFn: ({ id, name }: { id: string; name: string }) => api.reassociateIrCodeset(id, name), onSuccess: () => { setReassociate(null); setRemoteName(''); client.invalidateQueries({ queryKey: ['diagnostics', 'ir-codesets'] }) } })
  const reboot = useMutation({ mutationFn: api.rebootRemote })
  const powerOff = useMutation({ mutationFn: api.powerOffRemote, onSuccess: () => setConfirmPowerOff(false) })
  const update = firmware.data

  return <section className="page-section diagnostics-workspace"><header className="page-heading diagnostics-heading"><div><p className="eyebrow">Remote health</p><h1>Diagnostics</h1><p>Inspect the Remote, find stale configuration, and take care of maintenance without leaving the manager.</p></div><button className="primary-action" type="button" onClick={() => firmware.refetch()} disabled={firmware.isFetching}><RefreshCw className={firmware.isFetching ? 'spin' : ''} /> {firmware.isFetching ? 'Checking…' : 'Check firmware'}</button></header>

    <section className={`firmware-status ${update?.updateAvailable ? 'update-ready' : ''}`}><div className="firmware-icon"><Cpu /></div><div className="firmware-copy"><p className="eyebrow">System firmware</p><h2>{update ? update.updateAvailable ? 'An update is ready' : 'Your Remote is up to date' : 'Firmware status'}</h2><p>{update ? update.updateAvailable ? `${update.availableVersion ?? 'A newer version'} · ${update.title ?? 'System update'}` : `Installed version ${update.installedVersion}` : 'Run a live check to see the currently installed version and any available update.'}</p></div><div className="firmware-meta">{update && <><span>Installed</span><strong>{update.installedVersion}</strong></>}{update?.releaseNotesUrl && <a href={update.releaseNotesUrl} target="_blank" rel="noreferrer">Release notes <ExternalLink /></a>}</div>{firmware.isError && <div className="notice error"><TriangleAlert />{firmware.error.message}</div>}</section>

    <div className="diagnostics-section-heading"><div><p className="eyebrow">Configuration hygiene</p><h2>Activity references</h2></div><p>These checks are read-only. Fix references in the Remote Configurator after reviewing the result.</p></div>
    <div className="diagnostics-scan-grid"><ScanPanel title="Orphaned entities" description="Entities referenced by an activity but no longer supplied by its integration." queryKey="orphaned" load={api.orphanedEntities} empty="No orphaned entity references found." warning="Missing references can prevent an activity from working correctly." tone="danger" /><ScanPanel title="Unused activity entities" description="Entities available to an activity that are not used by any step." queryKey="unused" load={api.unusedActivityEntities} empty="Every activity entity is currently in use." warning="Review these before removal; an unused entity may be intended for a future step." tone="warning" /></div>

    <section className="diagnostics-section-heading"><div><p className="eyebrow">Infrared library</p><h2>Orphaned IR codesets</h2></div><button className="secondary-action" type="button" onClick={() => codesets.refetch()} disabled={codesets.isFetching}><RefreshCw className={codesets.isFetching ? 'spin' : ''} /> Scan</button></section>
    <article className="diagnostics-codesets">{codesets.isError ? <div className="notice error"><TriangleAlert />{codesets.error.message}</div> : !codesets.data?.length ? <div className="diagnostics-clear"><CircleCheck />No unassigned custom IR codesets found.</div> : <div className="codeset-list">{codesets.data.map((codeset, index) => { const id = codesetId(codeset); const active = reassociate === id; return <div className="codeset-row" key={id || index}><div className="codeset-name"><Wrench /><div><strong>{codesetName(codeset)}</strong><span>{id || 'Unknown codeset ID'}</span></div></div>{active ? <form className="codeset-form" onSubmit={(event) => { event.preventDefault(); if (remoteName.trim()) attachCodeset.mutate({ id, name: remoteName.trim() }) }}><input autoFocus value={remoteName} onChange={(event) => setRemoteName(event.target.value)} placeholder="New remote name" /><button className="primary-action" disabled={attachCodeset.isPending || !remoteName.trim()}>Create remote</button><button className="codeset-cancel" type="button" onClick={() => { setReassociate(null); setRemoteName('') }}>Cancel</button></form> : <div className="action-row"><button className="secondary-action" type="button" onClick={() => setReassociate(id)} disabled={!id || removeCodeset.isPending}><Link2 /> Re-associate</button><button className="icon-action danger" type="button" aria-label={`Delete ${codesetName(codeset)}`} onClick={() => setCodesetToDelete(codeset)} disabled={!id || removeCodeset.isPending}><Trash2 /></button></div>}</div>})}</div>}</article>
    {codesetToDelete && <Modal title={`Delete ${codesetName(codesetToDelete)}?`} close={() => { if (!removeCodeset.isPending) setCodesetToDelete(null) }}><div className="confirm-dialog"><p>This removes the unassigned IR codeset from the Remote. This cannot be undone.</p><div><button className="secondary-action" type="button" onClick={() => setCodesetToDelete(null)} disabled={removeCodeset.isPending}>Cancel</button><button className="danger-action" type="button" onClick={() => removeCodeset.mutate(codesetId(codesetToDelete))} disabled={removeCodeset.isPending}>{removeCodeset.isPending ? 'Deleting…' : 'Delete codeset'}</button></div></div></Modal>}

    <section className="diagnostics-section-heading power-heading"><div><p className="eyebrow">Remote controls</p><h2>Power &amp; restart</h2></div><p>These commands are sent directly to the active Remote.</p></section>
    <article className="diagnostics-power"><div><Power /><div><h2>Reboot Remote</h2><p>Restart the operating system. The Remote will briefly go offline.</p></div><button className="secondary-action" type="button" disabled={reboot.isPending} onClick={() => { if (window.confirm('Reboot the active Remote now?')) reboot.mutate() }}><RefreshCw className={reboot.isPending ? 'spin' : ''} /> Reboot</button></div><div><Power /><div><h2>Power off Remote</h2><p>Shut the Remote down completely. You will need to power it back on physically.</p></div><button className="danger-action" type="button" disabled={powerOff.isPending} onClick={() => setConfirmPowerOff(true)}>Power off <ChevronRight /></button></div>{(reboot.data?.message ?? powerOff.data?.message) && <div className="notice success"><CircleCheck />{reboot.data?.message ?? powerOff.data?.message}</div>}{(reboot.isError || powerOff.isError) && <div className="notice error"><TriangleAlert />{(reboot.error ?? powerOff.error)?.message}</div>}</article>
    {confirmPowerOff && <Modal title="Power off Remote" close={() => { if (!powerOff.isPending) setConfirmPowerOff(false) }}><div className="confirm-dialog"><p>Power off the active Remote? It will go offline immediately and must be turned back on physically.</p><div><button className="secondary-action" type="button" onClick={() => setConfirmPowerOff(false)} disabled={powerOff.isPending}>Cancel</button><button className="danger-action" type="button" onClick={() => powerOff.mutate()} disabled={powerOff.isPending}>{powerOff.isPending ? 'Powering off…' : 'Power off Remote'}</button></div></div></Modal>}
  </section>
}

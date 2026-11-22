import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowUp, Check, ChevronLeft, ChevronRight, CircleAlert, Layers, Power, RefreshCw, Trash2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { api, type SystemMessage, type DiagnosticActivityResult } from '../lib/api'
import { activityCleanupDescription, invalidateActivityScans, type ActivityCleanupSelection } from '../lib/activityCleanup'
import type { Integration } from '../lib/models'
import './remote.css'

const pages = ['Updates', 'Entity issues', 'System'] as const
type RemovalSelection = ActivityCleanupSelection
type Confirmation = { kind: 'update'; item: Integration } | { kind: 'restart' | 'shutdown' } | { kind: 'remove'; selection: RemovalSelection }

export function RemoteAttention({ items }: { items: Integration[] }) {
  const attention = items.filter(item => item.connectionState === 'disconnected' || item.connectionState === 'error')
  if (!attention.length) return null
  return <section className="remote-attention"><h2>Needs attention <span>{attention.length}</span></h2>{attention.map(item => <article key={item.id}><h3>{item.name}</h3><p className="remote-error">{item.connectionState === 'error' ? 'Connection error' : 'Disconnected'}</p></article>)}</section>
}

export function RemoteMessages({ messages }: { messages: SystemMessage[] }) {
  return <section className="remote-messages"><h2>Unread messages <span>{messages.length}</span></h2>{messages.length ? messages.map(message => <article key={message.id}>
    <h3>{message.title}</h3><small>{message.date}{['critical', 'high'].includes(message.priority) ? ` · ${message.priority} priority` : ''}</small>
    {message.content.length > 180 ? <><p>{message.content.slice(0, 180).trimEnd()}…</p><details><summary>Read full message</summary><p>{message.content}</p></details></> : <p>{message.content}</p>}
  </article>) : <p className="remote-clear"><Check /> No unread messages</p>}</section>
}

export function IssueList({ title, result, loading, error, retry, onRemove, disabled = false, tone = 'warning' }: {
  title: string; tone?: 'danger' | 'warning'; result?: DiagnosticActivityResult; loading: boolean; error: Error | null; retry: () => void; disabled?: boolean; onRemove?: (selection: Omit<RemovalSelection, 'remoteId' | 'kind'>) => void
}) {
  const activities = Object.entries(result?.activities ?? {}).filter(([, activity]) => activity.entities.length)
  const count = activities.reduce((sum, [, activity]) => sum + activity.entities.length, 0)
  const Icon = tone === 'danger' ? CircleAlert : Layers
  return <section className={`remote-issues ${tone}`} aria-label={title}>
    <header className="remote-issues-heading"><Icon aria-hidden="true" /><h2>{title}</h2>{result && !error && <span className="remote-issue-count">{count}</span>}</header>
    {loading ? <p role="status">Checking entities…</p> : error ? <div role="alert"><p>{error.message}</p><button onClick={retry}>Try again</button></div> : result && !activities.length ? <p className="remote-clear"><Check /> None found</p> : activities.map(([id, activity]) => <article key={id}>
      <h3>{activity.name}</h3>
      <ul>{activity.entities.map((entity, index) => <li key={`${entity.entity_id ?? entity.id ?? index}`}>
        <div className="remote-entity-copy"><strong>{String(entity.localized_name ?? entity.entity_id ?? entity.id ?? 'Unknown entity')}</strong>
        {entity.integration?.localized_name && <small>{entity.integration.localized_name}</small>}
        <code>{String(entity.entity_id ?? entity.id ?? '')}</code></div>
        {onRemove && (entity.entity_id || entity.id) && <button className="remote-entity-remove" title="Remove from activity" aria-label={`Remove ${String(entity.localized_name ?? entity.entity_id ?? entity.id)} from activity`} disabled={disabled} onClick={() => onRemove({ activityId: id, activityName: activity.name, entityId: String(entity.entity_id ?? entity.id), entityName: String(entity.localized_name ?? entity.entity_id ?? entity.id) })}><Trash2 aria-hidden="true" /></button>}
      </li>)}</ul>
    </article>)}
  </section>
}

export function RemotePage() {
  const client = useQueryClient()
  const [page, setPage] = useState(0)
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null)
  const [message, setMessage] = useState('')
  const [reconnecting, setReconnecting] = useState(false)
  const scroll = useRef<HTMLDivElement>(null)
  const focusAfterConfirmation = useRef<HTMLElement | null>(null)
  const cancelButton = useRef<HTMLButtonElement>(null)
  const touch = useRef<{ x: number; y: number } | null>(null)
  const bootstrap = useQuery({ queryKey: ['bootstrap'], queryFn: api.bootstrap, refetchInterval: 60_000 })
  const remoteId = bootstrap.data?.activeRemoteId
  const remote = bootstrap.data?.remotes.find(item => item.id === remoteId)
  const status = useQuery({ queryKey: ['remote-view', remoteId, 'status'], queryFn: api.status, enabled: Boolean(remoteId), refetchInterval: 60_000 })
  const integrations = useQuery({ queryKey: ['remote-view', remoteId, 'integrations'], queryFn: api.integrations, enabled: Boolean(remoteId) && page === 0, refetchInterval: reconnecting ? 3_000 : 60_000 })
  const orphaned = useQuery({ queryKey: ['remote-view', remoteId, 'orphaned'], queryFn: api.orphanedEntities, enabled: Boolean(remoteId) && page === 1 })
  const unused = useQuery({ queryKey: ['remote-view', remoteId, 'unused'], queryFn: api.unusedActivityEntities, enabled: Boolean(remoteId) && page === 1 })
  const messages = useQuery({ queryKey: ['remote-view', 'system-messages'], queryFn: api.systemMessageSummary, enabled: page === 2, refetchInterval: 60_000 })
  const action = useMutation({
    mutationFn: async (value: Confirmation) => value.kind === 'update' ? api.updateIntegration(value.item.id) : value.kind === 'remove' ? api.removeActivityEntity(value.selection) : value.kind === 'restart' ? api.rebootRemote() : api.powerOffRemote(),
    onSuccess: (result, value) => {
      if (value.kind === 'remove' && value.selection.remoteId !== remoteId) return
      setConfirmation(null)
      setMessage(value.kind === 'remove' ? `${value.selection.entityName} removed from ${value.selection.activityName}.` : value.kind === 'update' ? `${value.item.name} updated.${'reconnecting' in result && result.reconnecting ? ' Reconnecting…' : ''}` : value.kind === 'restart' ? 'Restart requested. The Remote will briefly go offline.' : 'Shutdown requested. Turn the Remote back on using its power button.')
      if (value.kind === 'update') {
        setReconnecting('reconnecting' in result && result.reconnecting)
        void client.invalidateQueries({ queryKey: ['remote-view', remoteId, 'integrations'] })
        void client.invalidateQueries({ queryKey: ['installed', 'integrations'] })
      }
    },
    onSettled: (_result, _error, value) => {
      if (value.kind === 'remove') {
        // Also rescan after a failed request: the helper may have partially completed.
        invalidateActivityScans(client, value.selection.remoteId)
      }
    },
  })
  const refresh = useMutation({ mutationFn: api.refreshIntegrations, onSuccess: () => client.invalidateQueries({ queryKey: ['remote-view', remoteId, 'integrations'] }) })
  const busy = action.isPending || refresh.isPending || reconnecting
  const online = status.data?.online === true && !status.isError && !bootstrap.isError
  const updates = (integrations.data ?? []).filter(item => item.updateAvailable).sort((a, b) => a.name.localeCompare(b.name))
  const changePage = (direction: number) => { if (!confirmation && !action.isPending) setPage(current => (current + direction + pages.length) % pages.length) }
  const confirm = (value: Confirmation) => {
    focusAfterConfirmation.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    action.reset()
    setMessage('')
    setConfirmation(value)
  }

  useEffect(() => {
    document.body.classList.add('remote-mode')
    return () => document.body.classList.remove('remote-mode')
  }, [])
  useEffect(() => {
    scroll.current?.scrollTo(0, 0)
    if (confirmation) cancelButton.current?.focus()
    else focusAfterConfirmation.current?.focus()
  }, [page, confirmation])
  useEffect(() => { setConfirmation(null); setMessage(''); action.reset(); setReconnecting(false) }, [remoteId]) // A confirmation must never carry over to another Remote.
  useEffect(() => {
    if (!reconnecting) return
    const timer = window.setTimeout(() => {
      setReconnecting(false)
      setMessage('Update sent. Check the integration status in the full manager if it has not reconnected.')
    }, 60_000)
    return () => window.clearTimeout(timer)
  }, [reconnecting])
  useEffect(() => {
    const value = action.variables
    if (reconnecting && value?.kind === 'update' && integrations.dataUpdatedAt > action.submittedAt + 5_000) {
      const item = integrations.data?.find(item => item.id === value.item.id)
      if (item && ['connected', 'ok'].includes(item.connectionState) && !item.updateAvailable) {
        setReconnecting(false)
        setMessage(`${item.name} updated and connected.`)
      }
    }
  }, [reconnecting, integrations.data, integrations.dataUpdatedAt, action.variables, action.submittedAt])
  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if (event.altKey || event.ctrlKey || event.metaKey) return
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault()
        if (!event.repeat) changePage(event.key === 'ArrowLeft' ? -1 : 1)
      } else if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End'].includes(event.key)) {
        event.preventDefault()
        const area = scroll.current
        if (!area) return
        if (event.key === 'Home' || event.key === 'End') area.scrollTop = event.key === 'Home' ? 0 : area.scrollHeight
        else area.scrollTop += (event.key.includes('Up') ? -1 : 1) * (event.key.startsWith('Page') ? area.clientHeight * .8 : 80)
      } else if (event.key === 'Escape' && confirmation && !action.isPending) {
        event.preventDefault(); setConfirmation(null)
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [confirmation, action.isPending])

  const refreshPage = () => {
    setMessage('')
    void bootstrap.refetch(); void status.refetch()
    if (page === 0) refresh.mutate()
    if (page === 1) { void orphaned.refetch(); void unused.refetch() }
    if (page === 2) void messages.refetch()
  }
  const refreshing = bootstrap.isFetching || status.isFetching || refresh.isPending || (page === 0 ? integrations.isFetching : page === 1 ? orphaned.isFetching || unused.isFetching : messages.isFetching)

  return <main className="remote-app" aria-label="Integration Manager for Remote">
    <header className="remote-header"><div><span>Integration Manager</span><strong>{remote?.name ?? 'Remote'}</strong>{remoteId && <p className={`remote-connection ${online ? '' : 'remote-error'}`}>{status.isPending ? 'Checking connection…' : status.isError ? 'Connection check failed' : online ? `${status.data?.docked ? 'Docked' : 'On battery'}${status.data?.batteryPercent != null ? ` · ${status.data.batteryPercent}%` : ''}` : 'Remote offline'}</p>}</div><button aria-label="Refresh current page" disabled={busy || refreshing || Boolean(confirmation)} onClick={refreshPage}><RefreshCw className={refreshing ? 'spin' : ''} /></button></header>
    <div className="remote-scroll" ref={scroll} tabIndex={0} aria-label={`${pages[page]} page`} onTouchStart={event => { const point = event.touches[0]; touch.current = { x: point.clientX, y: point.clientY } }} onTouchEnd={event => {
      const start = touch.current; touch.current = null
      if (!start) return
      const point = event.changedTouches[0], dx = point.clientX - start.x, dy = point.clientY - start.y
      if (Math.abs(dx) > 70 && Math.abs(dx) > Math.abs(dy) * 2) changePage(dx < 0 ? 1 : -1)
    }}>
      {confirmation ? <section className="remote-confirm" aria-labelledby="remote-confirm-title">
        <p className="remote-eyebrow">Confirm action</p>
        <h1 id="remote-confirm-title">{confirmation.kind === 'update' ? `Update ${confirmation.item.name}?` : confirmation.kind === 'remove' ? `Remove ${confirmation.selection.kind === 'orphaned' ? 'missing' : 'unused'} entity?` : confirmation.kind === 'restart' ? 'Restart Remote?' : 'Shut down Remote?'}</h1>
        <p>{confirmation.kind === 'update' ? `${confirmation.item.version ?? 'Installed'} → ${confirmation.item.bundle?.nextVersion ?? confirmation.item.latestVersion ?? 'latest'}. The integration will be briefly unavailable.` : confirmation.kind === 'remove' ? activityCleanupDescription(confirmation.selection) : confirmation.kind === 'restart' ? 'The Remote will briefly go offline while it restarts.' : 'The Remote will go offline. You will need to turn it back on using its power button.'}</p>
        <p className="remote-target">{remote?.name}</p>
        {action.isError && <p role="alert" className="remote-error">{action.error.message}</p>}
        <button ref={cancelButton} disabled={action.isPending} onClick={() => setConfirmation(null)}>Cancel</button>
        <button className={confirmation.kind === 'shutdown' ? 'remote-danger' : 'remote-primary'} disabled={busy || !online} onClick={() => action.mutate(confirmation)}>{action.isPending ? 'Sending…' : confirmation.kind === 'update' ? 'Update integration' : confirmation.kind === 'remove' ? 'Remove from activity' : confirmation.kind === 'restart' ? 'Restart Remote' : 'Shut down'}</button>
      </section> : <>
        <div className="remote-title"><p className="remote-eyebrow">{page === 0 ? 'Integration maintenance' : page === 1 ? 'Activity references' : 'Remote controls'}</p><h1>{pages[page]}</h1></div>
        {bootstrap.isPending ? <p role="status">Connecting…</p> : bootstrap.isError ? <p role="alert" className="remote-error">Unable to load Remote. Use refresh to reconnect.</p> : !remoteId ? <p>No Remote configured. Add one in the full manager.</p> : <>
          {message && <p className="remote-message" role="status">{message}</p>}
          {page === 0 && <>
            {!integrations.isError && integrations.data && <RemoteAttention items={integrations.data} />}
            {refresh.isError && <p className="remote-error" role="alert">{refresh.error.message}</p>}
            {integrations.isPending ? <p role="status">Checking updates…</p> : integrations.isError ? <p className="remote-error" role="alert">{integrations.error.message}</p> : !updates.length ? <div className="remote-empty"><Check /><h2>All up to date</h2><p>No integration updates available.</p></div> : <>
              <p>{updates.length} {updates.length === 1 ? 'update available' : 'updates available'}</p>
              {updates.map(item => {
                const canUpdate = item.capabilities.update && !item.capabilities.selfUpdate && item.inplaceUpgradeAvailable === true
                return <article className="remote-update" key={item.id}>
                  <h2>{item.name}</h2><p>{item.version ?? 'Installed'} <span aria-label="to">→</span> {item.bundle?.nextVersion ?? item.latestVersion ?? 'Latest'}</p>
                  {canUpdate ? <button className="remote-primary" disabled={busy || !online} onClick={() => confirm({ kind: 'update', item })}><ArrowUp /> Update</button> : <small>{item.capabilities.selfUpdate ? 'Update Integration Manager from the full manager.' : item.inplaceUpgradeAvailable !== true ? 'Compatible firmware is required for in-place updates. Check the full manager.' : 'Manage this update in the full manager.'}</small>}
                </article>
              })}
            </>}
          </>}
          {page === 1 && <>
            <IssueList title="Missing entities" tone="danger" result={orphaned.data} loading={orphaned.isPending} error={orphaned.error} retry={() => void orphaned.refetch()} disabled={busy || !online} onRemove={selection => confirm({ kind: 'remove', selection: { ...selection, kind: 'orphaned', remoteId: remoteId! } })} />
            <IssueList title="Unused" result={unused.data} loading={unused.isPending} error={unused.error} retry={() => void unused.refetch()} disabled={busy || !online} onRemove={selection => confirm({ kind: 'remove', selection: { ...selection, kind: 'unused', remoteId: remoteId! } })} />
            <p className="remote-footnote">Missing entities are no longer available from their integration, but may still be referenced by activity steps or controls. Removing a missing entity also clears its broken activity steps and controls. Unused cleanup preserves any entity that has become referenced.</p>
          </>}
          {page === 2 && <>
            {messages.isPending ? <p role="status">Loading messages…</p> : messages.isError ? <p className="remote-error" role="alert">Unable to load system messages. Use refresh to try again.</p> : <RemoteMessages messages={messages.data?.unread ?? []} />}
            <section className="remote-system"><h2>Restart</h2><p>Restart the Remote. It will reconnect shortly.</p><button disabled={busy || !online} onClick={() => confirm({ kind: 'restart' })}><RefreshCw /> Restart Remote</button></section>
            <section className="remote-system"><h2>Shut down</h2><p>Power off completely. Turn it back on using its power button.</p><button className="remote-danger" disabled={busy || !online} onClick={() => confirm({ kind: 'shutdown' })}><Power /> Shut down Remote</button></section>
            <p className="remote-footnote">Manager {bootstrap.data?.managerVersion ?? '—'}<br />Firmware {bootstrap.data?.firmwareVersion ?? '—'}</p>
          </>}
        </>}
      </>}
    </div>
    <nav className="remote-pagination" aria-label="Remote pages">
      <button aria-label="Previous page" disabled={Boolean(confirmation) || action.isPending} onClick={() => changePage(-1)}><ChevronLeft /></button>
      <div><span>{pages[page]}</span><div className="remote-dots" aria-hidden="true">{pages.map((name, index) => <i key={name} className={page === index ? 'active' : undefined} />)}</div></div>
      <button aria-label="Next page" disabled={Boolean(confirmation) || action.isPending} onClick={() => changePage(1)}><ChevronRight /></button>
    </nav>
  </main>
}

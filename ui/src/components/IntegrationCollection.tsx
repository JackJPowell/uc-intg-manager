import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { ArrowDownAZ, ArrowUpAZ, Boxes, Cable, CircleAlert, CircleCheck, Filter, RefreshCw, Search, Settings2, SlidersHorizontal, TriangleAlert, Upload } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../lib/api'
import { IntegrationCard } from './IntegrationCard'

type CompletedOperation = 'install' | 'update' | 'backup' | 'delete'

export function IntegrationCollection({ mode }: { mode: 'installed' | 'catalog' }) {
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState('all')
  const [category, setCategory] = useState('all')
  const [sortBy, setSortBy] = useState('original')
  const [sortReverse, setSortReverse] = useState(false)
  const [completion, setCompletion] = useState<{ name: string; operation: CompletedOperation } | null>(null)
  const [managerUpdate, setManagerUpdate] = useState<{ version: string; startedAt: number } | null>(null)
  const completionTimer = useRef<number | null>(null)
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: [mode, 'integrations'], queryFn: mode === 'catalog' ? api.catalog : api.integrations })
  const catalog = useQuery({ queryKey: ['catalog', 'integrations'], queryFn: api.catalog, enabled: mode === 'installed' })
  const showCompletion = (name: string, operation: CompletedOperation) => {
    if (completionTimer.current) window.clearTimeout(completionTimer.current)
    setCompletion({ name, operation })
    completionTimer.current = window.setTimeout(() => setCompletion(null), 3600)
  }
  useEffect(() => () => { if (completionTimer.current) window.clearTimeout(completionTimer.current) }, [])
  const refresh = useMutation({ mutationFn: api.refreshIntegrations, onSuccess: () => queryClient.invalidateQueries({ queryKey: [mode, 'integrations'] }) })
  const install = useMutation({ mutationFn: ({ id, version }: { id: string; version?: string; name: string }) => api.installIntegration(id, version), onSuccess: async (_, variables) => { await queryClient.invalidateQueries({ queryKey: [mode, 'integrations'] }); showCompletion(variables.name, 'install') } })
  const update = useMutation({ mutationFn: ({ id, version }: { id: string; version?: string; name: string }) => api.updateIntegration(id, version), onSuccess: async (_, variables) => { await queryClient.refetchQueries({ queryKey: [mode, 'integrations'], type: 'active' }); showCompletion(variables.name, 'update') } })
  const selfUpdate = useMutation({ mutationFn: ({ version }: { version?: string }) => api.selfUpdate(version), onSuccess: result => setManagerUpdate({ version: result.targetVersion, startedAt: Date.now() }) })
  const backup = useMutation({ mutationFn: ({ id }: { id: string; name: string }) => api.backupIntegration(id), onSuccess: async (_, variables) => { await queryClient.invalidateQueries({ queryKey: [mode, 'integrations'] }); showCompletion(variables.name, 'backup') } })
  const remove = useMutation({ mutationFn: ({ id, scope }: { id: string; name: string; scope: 'configuration' | 'full' }) => api.deleteIntegration(id, scope), onSuccess: async (_, variables) => { await queryClient.invalidateQueries({ queryKey: [mode, 'integrations'] }); showCompletion(variables.name, 'delete') } })
  useEffect(() => {
    if (!managerUpdate) return
    let cancelled = false
    const checkHealth = async () => {
      try {
        if (await api.managerHealth() === 'OK' && !cancelled) window.location.replace('/manager')
      } catch {
        // The manager is expected to be unavailable while the Remote restarts it.
      }
    }
    void checkHealth()
    const timer = window.setInterval(() => void checkHealth(), 2000)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [managerUpdate])
  const items = useMemo(() => (query.data ?? []).filter(item => {
    const haystack = `${item.name} ${item.description} ${item.developer ?? ''} ${item.categories.join(' ')}`.toLowerCase()
    if (!haystack.includes(search.toLowerCase())) return false
    if (filter === 'updates' && !item.updateAvailable) return false
    if (filter === 'needs-config' && item.connectionState !== 'not_configured') return false
    if (filter === 'connected' && item.connectionState !== 'connected' && item.connectionState !== 'ok') return false
    if (filter === 'disconnected' && item.connectionState !== 'disconnected' && item.connectionState !== 'error') return false
    if (filter === 'available' && item.driverInstalled) return false
    if (filter === 'installed' && !item.driverInstalled && !item.installed) return false
    if (filter === 'supports-backup' && !item.capabilities.backup) return false
    if (mode === 'catalog' && category !== 'all' && !item.categories.some(value => value.toLowerCase() === category.toLowerCase())) return false
    return true
  }).sort((a, b) => { const direction = sortReverse ? -1 : 1; if (sortBy === 'stars') return (b.repository.stars - a.repository.stars) * direction; if (sortBy === 'downloads') return (b.repository.downloads - a.repository.downloads) * direction; if (sortBy === 'created') return String(b.repository.createdAt ?? '').localeCompare(String(a.repository.createdAt ?? '')) * direction; if (sortBy === 'updated') return String(b.repository.updatedAt ?? '').localeCompare(String(a.repository.updatedAt ?? '')) * direction; if (sortBy === 'name') return a.name.localeCompare(b.name) * direction; if (sortBy === 'developer') return String(a.developer ?? '').localeCompare(String(b.developer ?? '')) * direction; return (a.originalIndex - b.originalIndex) * direction }), [category, filter, mode, query.data, search, sortBy, sortReverse])
  const installedCount = query.data?.length ?? 0
  const updatesCount = (query.data ?? []).filter(
    item => item.updateAvailable && item.management !== 'official' && item.management !== 'external',
  ).length
  const attentionCount = (query.data ?? []).filter(
    item => item.connectionState === 'disconnected' || item.connectionState === 'error',
  ).length
  const customIntegrationCount = (query.data ?? []).filter(item => item.management === 'custom').length
  const catalogCount = catalog.data?.length ?? 0
  const title = mode === 'catalog' ? 'Integration catalog' : 'Your integrations'
  const subtitle = mode === 'catalog' ? 'Browse what your Remote can become.' : 'Updates, health, and configuration in one working view.'

  return <section className="page-section">
    <header className="page-heading"><div><p className="eyebrow">Integration workspace</p><h1>{title}</h1><p>{subtitle}</p></div>
      <button className="refresh-button" type="button" onClick={() => refresh.mutate()} disabled={refresh.isPending}><RefreshCw className={refresh.isPending ? 'spin' : ''} /> Refresh</button>
    </header>
    {completion && <div className="integration-complete-notice" role="status"><CircleCheck aria-hidden="true" /><span><strong>{{ install: `Installed ${completion.name}`, update: `Updated ${completion.name}`, backup: 'Backup created', delete: `Removed ${completion.name}` }[completion.operation]}</strong><small>{{ install: 'Ready to configure here in Integration Manager.', update: 'The latest Remote state is now loaded.', backup: `${completion.name} configuration is safely stored.`, delete: 'The integrations list has been refreshed.' }[completion.operation]}</small></span></div>}
    {mode === 'installed' && <section className="integration-overview" aria-label="Integration summary"><button type="button" className={filter === 'all' ? 'active' : ''} onClick={() => setFilter('all')}><Cable /><span><strong>{installedCount}</strong><small>Installed integrations</small></span></button><button type="button" className={filter === 'updates' ? 'active update-summary' : 'update-summary'} onClick={() => setFilter('updates')}><Upload /><span><strong>{updatesCount}</strong><small>{updatesCount === 1 ? 'Update available' : 'Updates available'}</small></span></button><button type="button" className={filter === 'disconnected' ? 'active attention-summary' : 'attention-summary'} onClick={() => setFilter('disconnected')}><CircleAlert /><span><strong>{attentionCount}</strong><small>Needs attention</small></span></button><Link className="integration-overview-catalog" to="/catalog"><Boxes /><span><strong>{catalogCount}</strong><small>Browse integrations</small></span></Link></section>}
    {mode === 'catalog' && <p className="catalog-discovery"><Boxes aria-hidden="true" /><strong>{customIntegrationCount.toLocaleString()}</strong><span>custom integrations and counting</span></p>}
    <div className={`filters ${mode === 'catalog' ? 'catalog-filters' : ''}`}><label><Search /><span className="sr-only">Search integrations</span><input value={search} onChange={event => setSearch(event.target.value)} placeholder={mode === 'catalog' ? 'Search available integrations' : 'Search integrations'} /></label>
      <label className="filter-select"><Filter /><span className="sr-only">Filter integrations</span><select value={filter} onChange={event => setFilter(event.target.value)}>{mode === 'catalog' ? <><option value="all">All</option><option value="available">Not installed</option><option value="installed">Installed</option><option value="updates">Updates available</option><option value="supports-backup">Supports restore</option></> : <><option value="all">All states</option><option value="updates">Updates available</option><option value="needs-config">Needs configuration</option><option value="connected">Connected</option><option value="disconnected">Disconnected</option></>}</select></label>{mode === 'catalog' && <><label className="filter-select"><SlidersHorizontal /><span className="sr-only">Category</span><select value={category} onChange={event => setCategory(event.target.value)}><option value="all">All categories</option>{['Home Automation','AV Receivers','Media Players','Streaming','TVs','Projectors','Lighting','Audio','Gaming','Security','Utilities','Covers & Shades','Video Processors'].map(value => <option key={value} value={value}>{value}</option>)}</select></label><label className="filter-select"><span className="sr-only">Sort catalog</span><select value={sortBy} onChange={event => setSortBy(event.target.value)}><option value="original">Original order</option><option value="stars">Most stars</option><option value="downloads">Most downloads</option><option value="created">Newest</option><option value="updated">Recently updated</option><option value="name">Alphabetical</option><option value="developer">Developer</option></select></label><button className="icon-action sort-direction" type="button" onClick={() => setSortReverse(value => !value)} title="Reverse sort order" aria-label="Reverse sort order">{sortReverse ? <ArrowUpAZ /> : <ArrowDownAZ />}</button></>}</div>
    {[install, update, selfUpdate, backup, remove].find(mutation => mutation.isError)?.error && <div className="notice error"><TriangleAlert /> {[install, update, selfUpdate, backup, remove].find(mutation => mutation.isError)?.error?.message}</div>}
    {query.isError ? <div className="notice error"><TriangleAlert /> {query.error.message}</div> : query.isLoading ? <div className="loading-grid">Loading integrations…</div> : items.length ? <div className="integration-grid">{items.map(item => { const operation = install.isPending && install.variables.id === item.id ? 'install' : update.isPending && update.variables.id === item.id ? 'update' : backup.isPending && backup.variables.id === item.id ? 'backup' : remove.isPending && remove.variables.id === item.id ? 'delete' : undefined; return <IntegrationCard key={item.source === 'catalog' ? item.catalogId : item.id} item={item} onInstall={mode === 'catalog' ? (value, version) => install.mutate({ id: value.id, name: value.name, version }) : undefined} onUpdate={(value, version) => update.mutate({ id: value.id, name: value.name, version })} onSelfUpdate={(value, version) => selfUpdate.mutate({ version: version ?? value.latestVersion ?? undefined })} onBackup={value => backup.mutate({ id: value.id, name: value.name })} onDelete={value => remove.mutate({ id: value.id, name: value.name, scope: 'full' })} operation={operation} pending={Boolean(operation) || selfUpdate.isPending} /> })}</div> : <div className="empty-state"><Settings2 /><h2>No integrations match this view</h2><p>Try a different filter or refresh the list.</p></div>}
    {managerUpdate && <div className="manager-update-overlay" role="status" aria-live="polite"><div><RefreshCw className="spin" aria-hidden="true" /><p className="eyebrow">Update in progress</p><h2>Updating Integration Manager</h2><p>Installing {managerUpdate.version}. This page will reconnect automatically when the manager is ready.</p><small>{Math.floor((Date.now() - managerUpdate.startedAt) / 1000) > 90 ? 'This is taking longer than usual. Keep this page open while the Remote finishes restarting.' : 'The manager may be unavailable briefly while it restarts.'}</small></div></div>}
  </section>
}

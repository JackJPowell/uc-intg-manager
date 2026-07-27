import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDownAZ, ArrowUpAZ, Cable, CircleCheck, Filter, RefreshCw, Search, Settings2, SlidersHorizontal, TriangleAlert, Upload } from 'lucide-react'
import { useMemo, useState } from 'react'
import { api } from '../lib/api'
import { IntegrationCard } from './IntegrationCard'

export function IntegrationCollection({ mode }: { mode: 'installed' | 'catalog' }) {
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState('all')
  const [category, setCategory] = useState('all')
  const [sortBy, setSortBy] = useState('original')
  const [sortReverse, setSortReverse] = useState(false)
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: [mode, 'integrations'], queryFn: mode === 'catalog' ? api.catalog : api.integrations })
  const refresh = useMutation({ mutationFn: api.refreshIntegrations, onSuccess: () => queryClient.invalidateQueries({ queryKey: [mode, 'integrations'] }) })
  const install = useMutation({ mutationFn: ({ id, version }: { id: string; version?: string }) => api.installIntegration(id, version), onSuccess: () => queryClient.invalidateQueries({ queryKey: [mode, 'integrations'] }) })
  const update = useMutation({ mutationFn: ({ id, version }: { id: string; version?: string }) => api.updateIntegration(id, version), onSuccess: () => queryClient.invalidateQueries({ queryKey: [mode, 'integrations'] }) })
  const backup = useMutation({ mutationFn: api.backupIntegration })
  const remove = useMutation({ mutationFn: ({ id, scope }: { id: string; scope: 'configuration' | 'full' }) => api.deleteIntegration(id, scope), onSuccess: () => queryClient.invalidateQueries({ queryKey: [mode, 'integrations'] }) })
  const items = useMemo(() => (query.data ?? []).filter(item => {
    const haystack = `${item.name} ${item.description} ${item.developer ?? ''} ${item.categories.join(' ')}`.toLowerCase()
    if (!haystack.includes(search.toLowerCase())) return false
    if (filter === 'updates' && !item.updateAvailable) return false
    if (filter === 'connected' && item.connectionState !== 'connected' && item.connectionState !== 'ok') return false
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
  const configuredCount = (query.data ?? []).filter(item => item.installed).length
  const title = mode === 'catalog' ? 'Integration catalog' : 'Your integrations'
  const subtitle = mode === 'catalog' ? 'Browse what your Remote can become.' : 'Updates, health, and configuration in one working view.'

  return <section className="page-section">
    <header className="page-heading"><div><p className="eyebrow">Integration workspace</p><h1>{title}</h1><p>{subtitle}</p></div>
      <button className="refresh-button" type="button" onClick={() => refresh.mutate()} disabled={refresh.isPending}><RefreshCw className={refresh.isPending ? 'spin' : ''} /> Refresh</button>
    </header>
    {mode === 'installed' && <section className="integration-overview" aria-label="Integration summary"><button type="button" className={filter === 'all' ? 'active' : ''} onClick={() => setFilter('all')}><Cable /><span><strong>{installedCount}</strong><small>Installed integrations</small></span></button><button type="button" className={filter === 'updates' ? 'active update-summary' : 'update-summary'} onClick={() => setFilter('updates')}><Upload /><span><strong>{updatesCount}</strong><small>{updatesCount === 1 ? 'Update available' : 'Updates available'}</small></span></button><div className="integration-overview-note"><CircleCheck /><span>{configuredCount} configured and ready to use</span></div></section>}
    <div className={`filters ${mode === 'catalog' ? 'catalog-filters' : ''}`}><label><Search /><span className="sr-only">Search integrations</span><input value={search} onChange={event => setSearch(event.target.value)} placeholder={mode === 'catalog' ? 'Search available integrations' : 'Search integrations'} /></label>
      <label className="filter-select"><Filter /><span className="sr-only">Filter integrations</span><select value={filter} onChange={event => setFilter(event.target.value)}>{mode === 'catalog' ? <><option value="all">All</option><option value="available">Not installed</option><option value="installed">Installed</option><option value="updates">Updates available</option><option value="supports-backup">Supports restore</option></> : <><option value="all">All states</option><option value="updates">Updates available</option><option value="connected">Connected</option></>}</select></label>{mode === 'catalog' && <><label className="filter-select"><SlidersHorizontal /><span className="sr-only">Category</span><select value={category} onChange={event => setCategory(event.target.value)}><option value="all">All categories</option>{['Home Automation','AV Receivers','Media Players','Streaming','TVs','Projectors','Lighting','Audio','Gaming','Security','Utilities','Covers & Shades','Video Processors'].map(value => <option key={value} value={value}>{value}</option>)}</select></label><label className="filter-select"><span className="sr-only">Sort catalog</span><select value={sortBy} onChange={event => setSortBy(event.target.value)}><option value="original">Original order</option><option value="stars">Most stars</option><option value="downloads">Most downloads</option><option value="created">Newest</option><option value="updated">Recently updated</option><option value="name">Alphabetical</option><option value="developer">Developer</option></select></label><button className="icon-action sort-direction" type="button" onClick={() => setSortReverse(value => !value)} title="Reverse sort order" aria-label="Reverse sort order">{sortReverse ? <ArrowUpAZ /> : <ArrowDownAZ />}</button></>}</div>
    {[install, update, backup, remove].find(mutation => mutation.isError)?.error && <div className="notice error"><TriangleAlert /> {[install, update, backup, remove].find(mutation => mutation.isError)?.error?.message}</div>}
    {query.isError ? <div className="notice error"><TriangleAlert /> {query.error.message}</div> : query.isLoading ? <div className="loading-grid">Loading integrations…</div> : items.length ? <div className="integration-grid">{items.map(item => { const operation = install.isPending && install.variables.id === item.id ? 'install' : update.isPending && update.variables.id === item.id ? 'update' : backup.isPending && backup.variables === item.id ? 'backup' : remove.isPending && remove.variables.id === item.id ? 'delete' : undefined; return <IntegrationCard key={item.id} item={item} onInstall={mode === 'catalog' ? (value, version) => install.mutate({ id: value.id, version }) : undefined} onUpdate={(value, version) => update.mutate({ id: value.id, version })} onBackup={value => backup.mutate(value.id)} onDelete={value => remove.mutate({ id: value.id, scope: 'full' })} operation={operation} pending={Boolean(operation)} /> })}</div> : <div className="empty-state"><Settings2 /><h2>No integrations match this view</h2><p>Try a different filter or refresh the list.</p></div>}
  </section>
}

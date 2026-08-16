import { useQuery } from '@tanstack/react-query'
import { Download, Filter, RefreshCw, Search, ServerCog, TriangleAlert } from 'lucide-react'
import { useMemo, useState } from 'react'
import { api } from '../lib/api'

const levels = [[7, 'All levels — Debug+'], [6, 'Info+'], [5, 'Notice+'], [4, 'Warning+'], [3, 'Error+'], [2, 'Critical+'], [1, 'Alert+'], [0, 'Emergency only']] as const
const levelName = (entry: Record<string, unknown>) => String(entry.level ?? entry.priority ?? entry.prio ?? 'debug').toLowerCase()
const levelTone = (entry: Record<string, unknown>) => { const level = levelName(entry); if (/(emerg|alert|crit|error|3|2|1|0)/.test(level)) return 'critical'; if (/(warn|4)/.test(level)) return 'warning'; if (/(notice|info|5|6)/.test(level)) return 'info'; return 'debug' }
const messageFor = (entry: Record<string, unknown>) => String(entry.message ?? entry.msg ?? entry.m ?? entry.text ?? JSON.stringify(entry))
const clientLevels = ['all', 'debug', 'info', 'notice', 'warning', 'error', 'critical', 'alert', 'emergency'] as const
const clientLevelName = (entry: Record<string, unknown>) => {
  const level = levelName(entry)
  const numericLevels: Record<string, string> = { '7': 'debug', '6': 'info', '5': 'notice', '4': 'warning', '3': 'error', '2': 'critical', '1': 'alert', '0': 'emergency' }
  if (numericLevels[level]) return numericLevels[level]
  return clientLevels.find(candidate => candidate !== 'all' && level.includes(candidate)) ?? 'debug'
}
const timestampFor = (entry: Record<string, unknown>) => {
  const direct = entry.timestamp ?? entry.ts ?? entry.time ?? entry.datetime
  if (direct) return String(direct)
  const match = messageFor(entry).match(/^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)(?:\s|\])/)
  return match?.[1] ?? '—'
}

export function IntegrationLogsPage() {
  const [services, setServices] = useState<string[]>([])
  const [priority, setPriority] = useState(7)
  const [textFilter, setTextFilter] = useState('')
  const [clientLevel, setClientLevel] = useState<(typeof clientLevels)[number]>('all')
  const [autoRefresh, setAutoRefresh] = useState(false)
  const catalog = useQuery({ queryKey: ['integration-log-services'], queryFn: api.integrationLogServices })
  const logs = useQuery({ queryKey: ['integration-logs', services, priority], queryFn: () => api.integrationLogs(services, priority), enabled: services.length > 0, refetchInterval: autoRefresh ? 10_000 : false })
  const allSelected = Boolean(catalog.data?.length) && services.length === catalog.data?.length
  const selectedNames = useMemo(() => new Set(services), [services])
  const toggle = (id: string) => setServices(current => current.includes(id) ? current.filter(item => item !== id) : [...current, id])
  const toggleAll = () => setServices(allSelected ? [] : (catalog.data ?? []).map(service => service.id))
  const download = `/api/v1/integration-logs/export?service=${encodeURIComponent(services.join(','))}&priority=${priority}`
  const entries = logs.data ?? []
  const visibleEntries = useMemo(() => {
    const query = textFilter.trim().toLowerCase()
    return entries.filter(entry => {
      if (clientLevel !== 'all' && clientLevelName(entry) !== clientLevel) return false
      return !query || `${timestampFor(entry)} ${clientLevelName(entry)} ${messageFor(entry)}`.toLowerCase().includes(query)
    })
  }, [clientLevel, entries, textFilter])
  return <section className="log-workspace">
    <header className="page-heading log-hero"><div><p className="eyebrow">Remote activity</p><h1>Integration logs</h1><p>Inspect live service output without leaving the manager.</p></div><div className="log-actions"><button className="secondary-action" type="button" onClick={() => logs.refetch()} disabled={!services.length || logs.isFetching}><RefreshCw className={logs.isFetching ? 'spin' : ''} /> Refresh</button><a className={`primary-action ${!services.length ? 'disabled-link' : ''}`} href={services.length ? download : undefined} aria-disabled={!services.length}><Download /> Download</a></div></header>
    {(catalog.isError || logs.isError) && <div className="notice error"><TriangleAlert /> {(catalog.error ?? logs.error)?.message}</div>}
    <section className="log-controls"><div className="service-control"><div className="control-label"><ServerCog /><span>Integration services</span><small>{services.length ? `${services.length} selected` : 'Select one or more services'}</small></div><div className="service-options"><label className="service-choice select-all"><input type="checkbox" checked={allSelected} onChange={toggleAll} /><span>Select all</span></label>{(catalog.data ?? []).map(service => <label className="service-choice" key={service.id}><input type="checkbox" checked={selectedNames.has(service.id)} onChange={() => toggle(service.id)} /><span><strong>{service.name}</strong><small>{service.id}</small></span></label>)}</div></div><div className="log-filters"><label><span><Filter /> Minimum level</span><select value={priority} onChange={event => setPriority(Number(event.target.value))}>{levels.map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select></label><label className="client-log-search"><span><Search /> Search returned logs</span><input value={textFilter} onChange={event => setTextFilter(event.target.value)} placeholder="Text, service, timestamp…" /></label><label><span><Filter /> Show only</span><select value={clientLevel} onChange={event => setClientLevel(event.target.value as (typeof clientLevels)[number])}>{clientLevels.map(level => <option key={level} value={level}>{level === 'all' ? 'All returned levels' : `${level[0].toUpperCase()}${level.slice(1)}`}</option>)}</select></label><label className="auto-refresh"><input type="checkbox" checked={autoRefresh} onChange={event => setAutoRefresh(event.target.checked)} /><span>Refresh every 10 seconds</span></label></div></section>
    <div className="log-legend"><span>Priority</span><i className="critical" /> Errors and critical <i className="warning" /> Warnings <i className="info" /> Notice and info <i className="debug" /> Debug</div>
    <section className="remote-log-stream" aria-live="polite"><header><span>{services.length ? `${visibleEntries.length}${visibleEntries.length === entries.length ? '' : ` of ${entries.length}`} recent entries` : 'Waiting for service selection'}</span>{autoRefresh && <span className="streaming"><i /> Live refresh enabled</span>}</header>{services.length ? <div className="log-rows">{visibleEntries.map((entry, index) => <article className={`remote-log-row ${levelTone(entry)}`} key={`${timestampFor(entry)}-${index}`}><time>{timestampFor(entry)}</time><strong>{levelName(entry)}</strong><p>{messageFor(entry)}</p></article>)}{!logs.isLoading && !visibleEntries.length && <div className="log-empty"><Search /><p>{entries.length ? 'No returned entries match the client-side filters.' : 'No entries match this service and level filter.'}</p></div>}</div> : <div className="log-empty"><ServerCog /><p>Select services above to start reading their logs.</p></div>}</section>
  </section>
}

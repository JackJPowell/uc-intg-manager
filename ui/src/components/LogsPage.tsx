import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Search, Trash2, TriangleAlert } from 'lucide-react'
import { useMemo, useState } from 'react'
import { api } from '../lib/api'
import { Modal } from './Modal'

const tone = (level: string) => { const value = level.toLowerCase(); if (/(critical|error|fatal)/.test(value)) return 'critical'; if (/warn/.test(value)) return 'warning'; if (/info/.test(value)) return 'info'; return 'debug' }

export function LogsPage() {
  const client = useQueryClient()
  const [autoRefresh, setAutoRefresh] = useState(false)
  const [query, setQuery] = useState('')
  const [level, setLevel] = useState('all')
  const [confirmClear, setConfirmClear] = useState(false)
  const [manualRefresh, setManualRefresh] = useState(false)
  const logs = useQuery({ queryKey: ['logs'], queryFn: api.logs, refetchInterval: autoRefresh ? 10_000 : false })
  const clear = useMutation({ mutationFn: api.clearLogs, onSuccess: () => { client.setQueryData(['logs'], []); setConfirmClear(false) } })
  const refreshLogs = async () => { setManualRefresh(true); try { await logs.refetch() } finally { setManualRefresh(false) } }
  const entries = useMemo(() => (logs.data ?? []).filter(entry => (level === 'all' || entry.level.toLowerCase() === level) && `${entry.logger} ${entry.message}`.toLowerCase().includes(query.toLowerCase())), [logs.data, level, query])
  const refreshing = logs.isFetching || manualRefresh
  return <section className="manager-log-workspace"><header className="page-heading manager-log-hero"><div><p className="eyebrow">Manager activity</p><h1>Manager logs</h1><p>Local operational history for the Integration Manager.</p></div><div className="log-actions"><button className="secondary-action" type="button" onClick={refreshLogs} disabled={refreshing}><RefreshCw className={refreshing ? 'spin' : ''} /> Refresh</button><button className="danger-action" type="button" disabled={clear.isPending} onClick={() => setConfirmClear(true)}><Trash2 /> Clear logs</button></div></header>
    {(logs.isError || clear.isError) && <div className="notice error"><TriangleAlert /> {(logs.error ?? clear.error)?.message}</div>}
    <section className="manager-log-toolbar"><label className="manager-log-search"><Search /><input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search logger or message" /></label><label className="manager-log-level"><span>Level</span><select value={level} onChange={event => setLevel(event.target.value)}><option value="all">All levels</option><option value="error">Error</option><option value="warning">Warning</option><option value="info">Info</option><option value="debug">Debug</option></select></label><label className="manager-auto-refresh"><input type="checkbox" checked={autoRefresh} onChange={event => setAutoRefresh(event.target.checked)} /><span>Refresh every 10 seconds</span></label></section>
    <div className="log-legend"><span>Severity</span><i className="critical" /> Error <i className="warning" /> Warning <i className="info" /> Info <i className="debug" /> Debug <b>{entries.length} shown</b></div>
    <section className="manager-log-stream"><header><span>{logs.data?.length ?? 0} stored entries</span>{autoRefresh && <span className="streaming"><i /> Live refresh enabled</span>}</header><div className="manager-log-rows">{entries.map((entry,index) => <article className={`manager-log-row ${tone(entry.level)}`} key={`${entry.timestamp}-${index}`}><time>{entry.timestamp}</time><strong>{entry.level}</strong><span>{entry.logger}</span><p>{entry.message}</p></article>)}{!logs.isLoading && !entries.length && <div className="log-empty"><Search /><p>{logs.data?.length ? 'No logs match the current filters.' : 'No manager logs yet.'}</p></div>}</div></section>
    {confirmClear && <Modal title="Clear manager logs?" close={() => setConfirmClear(false)}><div className="confirm-dialog"><p>This permanently clears all manager log entries. This cannot be undone.</p><div><button className="secondary-action" type="button" onClick={() => setConfirmClear(false)}>Cancel</button><button className="danger-action" type="button" disabled={clear.isPending} onClick={() => clear.mutate()}><Trash2 /> {clear.isPending ? 'Clearing…' : 'Clear logs'}</button></div></div></Modal>}
  </section>
}

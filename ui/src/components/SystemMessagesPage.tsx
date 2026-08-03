import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Archive, CheckCircle2, ChevronDown, Inbox, RefreshCw, TriangleAlert } from 'lucide-react'
import { api, type SystemMessage } from '../lib/api'

function priorityTone(priority: string) { if (priority === 'critical') return 'critical'; if (priority === 'high') return 'high'; if (priority === 'low') return 'low'; return 'normal' }
function MessageSheet({ message, archived = false }: { message: SystemMessage; archived?: boolean }) { const tone = priorityTone(message.priority); return <article className={`message-sheet ${tone} ${archived ? 'archived' : ''}`}><header><div><p className="message-meta"><span>{message.date}</span>{(message.priority === 'critical' || message.priority === 'high') && <b>{message.priority === 'critical' ? 'Critical' : 'High priority'}</b>}</p><h2>{message.title}</h2></div><span className="message-mark" aria-label={`${message.priority} priority`} /></header><p className="message-body">{message.content}</p></article> }

export function SystemMessagesPage() {
  const client = useQueryClient()
  const messages = useQuery({ queryKey: ['system-messages'], queryFn: api.systemMessages })
  const refresh = useMutation({ mutationFn: api.refreshSystemMessages, onSuccess: () => client.invalidateQueries({ queryKey: ['system-messages'] }) })
  const unread = messages.data?.unread ?? []
  const read = messages.data?.read ?? []
  return <section className="messages-workspace"><header className="messages-hero"><div><p className="eyebrow">Manager announcements</p><h1>System messages</h1><p>Release notices and operational updates from Integration Manager.</p></div><button className="refresh-button" type="button" onClick={() => refresh.mutate()} disabled={refresh.isPending}><RefreshCw className={refresh.isPending ? 'spin' : ''} /> Refresh</button></header>{(messages.isError || refresh.isError) && <div className="notice error"><TriangleAlert /> {(messages.error ?? refresh.error)?.message}</div>}
    {unread.length ? <section className="message-section"><header><div><Inbox /><h2>New messages</h2></div><span>{unread.length}</span></header><div className="message-stack">{unread.map(message => <MessageSheet key={message.id} message={message} />)}</div></section> : <section className="messages-caught-up"><CheckCircle2 /><h2>All caught up</h2><p>There are no new announcements.</p></section>}
    {read.length ? <details className="message-archive"><summary><div><Archive /><span>Previous messages</span><small>{read.length}</small></div><ChevronDown /></summary><div className="message-stack">{read.map(message => <MessageSheet key={message.id} message={message} archived />)}</div></details> : null}
  </section>
}

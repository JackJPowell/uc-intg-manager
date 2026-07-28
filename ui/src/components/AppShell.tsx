import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useRouterState } from '@tanstack/react-router'
import { Activity, Archive, Bell, BookOpen, Boxes, Cable, ChevronDown, CircleAlert, ExternalLink, FileText, Inbox, Plus, Settings } from 'lucide-react'
import { useEffect, useRef, useState, type PropsWithChildren } from 'react'
import { api } from '../lib/api'

const navigation = [
  ['/', 'Your integrations', Cable],
  ['/catalog', 'Catalog', Boxes],
  ['/diagnostics', 'Diagnostics', Activity],
  ['/system-messages', 'System messages', Inbox],
  ['/settings', 'Settings', Settings],
  ['/notifications', 'Notifications', Bell],
  ['/integration-logs', 'Integration logs', FileText],
  ['/backups', 'Backups', Archive],
] as const

export function AppShell({ children }: PropsWithChildren) {
  const queryClient = useQueryClient()
  const location = useRouterState({ select: state => state.location.pathname })
  const [remoteMenuOpen, setRemoteMenuOpen] = useState(false)
  const remotePickerRef = useRef<HTMLDivElement>(null)
  const bootstrap = useQuery({ queryKey: ['bootstrap'], queryFn: api.bootstrap, refetchInterval: 30_000 })
  const status = useQuery({ queryKey: ['status'], queryFn: api.status, refetchInterval: 30_000 })
  const selectRemote = useMutation({
    mutationFn: api.setActiveRemote,
    onSuccess: () => queryClient.invalidateQueries(),
  })
  const remote = bootstrap.data?.remotes.find(item => item.active)

  useEffect(() => {
    if (!remoteMenuOpen) return
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (remotePickerRef.current && !remotePickerRef.current.contains(event.target as Node)) setRemoteMenuOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setRemoteMenuOpen(false)
    }
    document.addEventListener('mousedown', closeOnOutsideClick)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('mousedown', closeOnOutsideClick)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [remoteMenuOpen])

  return <div className="app-shell">
    <header className="topbar">
      <Link to="/" className="brand" aria-label="Integration Manager home">
        <img src="/static/img/intg-manager.png" alt="" />
        <span>Integration <em>Manager</em></span>
      </Link>
      <div className="topbar-actions">
        <span className={`connection ${status.data?.online ? 'is-online' : 'is-offline'}`}>
          <i />{status.data?.online ? (status.data.docked ? 'Docked' : 'On battery') : 'Remote offline'}
        </span>
        <div className="remote-picker" ref={remotePickerRef}>
          <button className="remote-picker-trigger" type="button" onClick={() => setRemoteMenuOpen(open => !open)} aria-haspopup="menu" aria-expanded={remoteMenuOpen} disabled={!bootstrap.data?.remotes.length}>
            <span className={`remote-status-dot ${remote?.online ? 'online' : 'offline'}`} aria-hidden="true" />
            <span className="remote-picker-label"><strong>{remote?.name ?? 'Choose a Remote'}</strong><small>{remote?.address ?? 'No Remote configured'}</small></span>
            <ChevronDown className={remoteMenuOpen ? 'open' : ''} aria-hidden="true" />
          </button>
          {remoteMenuOpen && <div className="remote-picker-menu" role="menu" aria-label="Choose active Remote">
            <div className="remote-picker-list">
              {bootstrap.data?.remotes.map(item => <button key={item.id} className={item.active ? 'active' : ''} type="button" role="menuitemradio" aria-checked={item.active} disabled={item.active || selectRemote.isPending} onClick={() => selectRemote.mutate(item.id, { onSuccess: () => setRemoteMenuOpen(false) })}>
                <span className={`remote-status-dot ${item.online ? 'online' : 'offline'}`} aria-hidden="true" />
                <span><strong>{item.name}</strong><small>{item.address}</small></span>
                {item.active && <em>Active</em>}
              </button>)}
            </div>
            {bootstrap.data?.remoteConfiguratorUrl && <a className="remote-picker-add" href={bootstrap.data.remoteConfiguratorUrl} target="_blank" rel="noreferrer" role="menuitem" onClick={() => setRemoteMenuOpen(false)}><Plus aria-hidden="true" /><span><strong>Add another Remote</strong><small>Open the configurator to connect one.</small></span><ExternalLink aria-hidden="true" /></a>}
            {selectRemote.isError && <p className="remote-picker-error" role="alert">Unable to switch the active Remote. Try again.</p>}
          </div>}
        </div>
      </div>
    </header>
    <div className="workspace">
      <nav className="sidebar" aria-label="Manager navigation">
        <p>Workspace</p>
        {navigation.map(([to, label, Icon]) => <Link key={to} to={to} className={location === to ? 'active' : ''}>
          <Icon aria-hidden="true" /><span>{label}</span>
        </Link>)}
        <div className="sidebar-foot">
          {bootstrap.data?.remoteConfiguratorUrl && <a href={bootstrap.data.remoteConfiguratorUrl} target="_blank" rel="noreferrer"><ExternalLink /><span>Web Configurator</span></a>}
          <Link to="/logs"><BookOpen /><span>Manager logs</span></Link>
        </div>
      </nav>
      <main className="content">
        {bootstrap.isError && <div className="notice error"><CircleAlert /> Unable to load manager context.</div>}
        {children}
      </main>
    </div>
  </div>
}

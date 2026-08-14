import { useMutation, useQuery } from '@tanstack/react-query'
import { Bell, Building2, Check, CircleDot, KeyRound, RefreshCw, Save, Send, SlidersHorizontal, TriangleAlert, Webhook } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { api, type NotificationSettings } from '../lib/api'

type ProviderId = 'home_assistant' | 'webhook' | 'discord' | 'ntfy' | 'pushover'
type Field = { key: string; label: string; hint: string; type?: 'text' | 'url' | 'password' | 'textarea' }
const providers: Array<{ id: ProviderId; label: string; description: string; tone: string; icon: typeof Bell; test: 'home-assistant' | 'webhook' | 'discord' | 'ntfy' | 'pushover'; fields: Field[] }> = [
  { id: 'home_assistant', label: 'Home Assistant', description: 'Deliver to a Home Assistant notify service.', tone: 'blue', icon: Building2, test: 'home-assistant', fields: [{ key: 'url', label: 'Home Assistant URL', hint: 'For example, http://homeassistant.local:8123', type: 'url' }, { key: 'token', label: 'Long-lived access token', hint: 'Profile → Security → Long-lived access tokens', type: 'password' }] },
  { id: 'webhook', label: 'Webhook', description: 'POST notification events to a custom endpoint.', tone: 'green', icon: Webhook, test: 'webhook', fields: [{ key: 'url', label: 'Webhook URL', hint: 'The endpoint that receives the notification payload.', type: 'url' }, { key: 'headers', label: 'Custom headers', hint: 'Optional JSON object, such as {"Authorization":"Bearer …"}.', type: 'textarea' }] },
  { id: 'discord', label: 'Discord', description: 'Send a concise alert to a Discord channel.', tone: 'indigo', icon: CircleDot, test: 'discord', fields: [{ key: 'webhook_url', label: 'Webhook URL', hint: 'Create one in Server Settings → Integrations → Webhooks.', type: 'url' }] },
  { id: 'ntfy', label: 'ntfy', description: 'Publish pushes through ntfy.sh or a self-hosted server.', tone: 'violet', icon: Bell, test: 'ntfy', fields: [{ key: 'server', label: 'Server URL', hint: 'Use https://ntfy.sh or your own server.', type: 'url' }, { key: 'topic', label: 'Topic', hint: 'Subscribe to this topic in the ntfy app.' }, { key: 'token', label: 'Access token', hint: 'Optional, but recommended for protected topics.', type: 'password' }] },
  { id: 'pushover', label: 'Pushover', description: 'Send a priority alert through the Pushover app.', tone: 'amber', icon: KeyRound, test: 'pushover', fields: [{ key: 'user_key', label: 'User key', hint: 'Found on your Pushover dashboard.' }, { key: 'app_token', label: 'Application API token', hint: 'Create an application in Pushover.', type: 'password' }] },
]
const triggers = [['integration_update_available', 'Integration updates', 'When an installed integration has a new version.'], ['new_integration_in_registry', 'New catalog additions', 'When a new integration enters the registry.'], ['integration_error_state', 'Integration errors', 'When an integration enters an error state.'], ['orphaned_entities_detected', 'Orphaned entities', 'When activity entities need attention.'], ['firmware_update_available', 'Firmware updates', 'When the Remote or dock has an update.']] as const

function valueFor(config: Record<string, unknown>, key: string) { const value = config[key]; return typeof value === 'object' && value !== null ? JSON.stringify(value, null, 2) : String(value ?? '') }

export function NotificationsPage() {
  const query = useQuery({ queryKey: ['notifications'], queryFn: api.notifications })
  const [settings, setSettings] = useState<NotificationSettings | null>(null)
  const [services, setServices] = useState<string[]>([])
  const [manualServiceRefresh, setManualServiceRefresh] = useState(false)
  const loadedServiceCredentials = useRef<string | null>(null)
  const save = useMutation({ mutationFn: api.saveNotifications, onSuccess: setSettings })
  const test = useMutation({ mutationFn: ({ provider, values }: { provider: Parameters<typeof api.testNotification>[0]; values: Record<string, unknown> }) => api.testNotification(provider, values) })
  const fetchServices = useMutation({ mutationFn: api.homeAssistantServices, onSuccess: setServices })
  useEffect(() => { if (query.data) setSettings(query.data) }, [query.data])
  useEffect(() => {
    const homeAssistant = query.data?.home_assistant ?? {}
    const url = valueFor(homeAssistant, 'url').trim()
    const token = valueFor(homeAssistant, 'token').trim()
    const credentials = `${url}\u0000${token}`
    if (!url || !token || loadedServiceCredentials.current === credentials) return
    loadedServiceCredentials.current = credentials
    fetchServices.mutate()
  }, [fetchServices, query.data])
  if (!settings) return <div className="loading-grid">Loading notification settings…</div>
  const edit = (section: ProviderId | 'triggers', key: string, value: unknown) => setSettings({ ...settings, [section]: { ...settings[section], [key]: value } })
  const saveAll = () => {
    const webhook = settings.webhook ?? {}
    const rawHeaders = webhook.headers
    if (typeof rawHeaders === 'string') { try { settings.webhook = { ...webhook, headers: rawHeaders.trim() ? JSON.parse(rawHeaders) : {} } } catch { return } }
    save.mutate(settings)
  }
  const refreshServices = async () => { setManualServiceRefresh(true); try { await fetchServices.mutateAsync() } finally { setManualServiceRefresh(false) } }
  return <section className="notification-page">
    <header className="page-heading notification-hero"><div><p className="eyebrow">Delivery preferences</p><h1>Notifications</h1><p>Choose where manager activity reaches you, then decide which events matter.</p></div><button className="refresh-button" type="button" onClick={saveAll} disabled={save.isPending}><Save />{save.isPending ? 'Saving…' : 'Save all changes'}</button></header>
    {(save.isError || test.isError || fetchServices.isError) && <div className="notice error"><TriangleAlert /> {(save.error ?? test.error ?? fetchServices.error)?.message}</div>}
    {test.isSuccess && <div className="notice success"><Check /> Test notification sent.</div>}
    <div className="provider-stack">{providers.map(provider => { const config = settings[provider.id] ?? {}; const Icon = provider.icon; const refreshingServices = fetchServices.isPending || manualServiceRefresh; return <article className={`provider-panel ${provider.tone}`} key={provider.id}><header><div className="provider-identity"><span className="provider-icon"><Icon /></span><div><h2>{provider.label}</h2><p>{provider.description}</p></div></div><label className="switch"><input type="checkbox" checked={Boolean(config.enabled)} onChange={event => edit(provider.id, 'enabled', event.target.checked)} /><span /><em>{config.enabled ? 'Enabled' : 'Off'}</em></label></header><div className="provider-fields">{provider.fields.map(field => <label key={field.key}><span>{field.label}</span>{field.type === 'textarea' ? <textarea rows={3} value={valueFor(config, field.key)} onChange={event => edit(provider.id, field.key, event.target.value)} /> : <input type={field.type ?? 'text'} value={valueFor(config, field.key)} onChange={event => edit(provider.id, field.key, event.target.value)} />}<small>{field.hint}</small></label>)}{provider.id === 'home_assistant' && <label><span>Notify service</span><div className="service-picker"><select value={valueFor(config, 'service') || 'notify'} onChange={event => edit('home_assistant', 'service', event.target.value)}><option value="notify">notify — broadcast</option>{services.filter(item => item !== 'notify').map(item => <option key={item} value={item}>{item}</option>)}</select><button type="button" className="icon-action" aria-label="Find Home Assistant notify services" title="Refresh Home Assistant notify services" onClick={refreshServices} disabled={refreshingServices}><RefreshCw className={refreshingServices ? 'spin' : ''} /></button></div><small>Available services load automatically when saved Home Assistant credentials are present.</small></label>}</div><footer><button className="secondary-action" type="button" onClick={() => test.mutate({ provider: provider.test, values: config })} disabled={test.isPending || !config.enabled}><Send /> Send test</button></footer></article> })}</div>
    <article className="trigger-panel"><header><div className="provider-identity"><span className="provider-icon"><SlidersHorizontal /></span><div><h2>Notification triggers</h2><p>Control the events delivered through every enabled provider.</p></div></div></header><div className="trigger-list">{triggers.map(([key, label, description]) => <label key={key}><span><strong>{label}</strong><small>{description}</small></span><span className="switch"><input type="checkbox" checked={Boolean(settings.triggers?.[key])} onChange={event => edit('triggers', key, event.target.checked)} /><span /></span></label>)}</div></article>
  </section>
}

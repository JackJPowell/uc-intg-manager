import { useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ArrowLeft, Box, CheckCircle2, CircleDot, CircleX, ExternalLink, Gamepad2, Gauge, Lightbulb, ListFilter, LoaderCircle, Minus, MonitorPlay, Plus, Search, Settings2, SlidersHorizontal, Thermometer, ToggleLeft } from 'lucide-react'
import { Fragment, type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { api, ApiError } from '../lib/api'
import type { Integration, IntegrationSetupEntity, IntegrationSetupField, IntegrationSetupInfo, IntegrationSetupPage } from '../lib/models'
import { Modal } from './Modal'

function initialValues(page: IntegrationSetupPage | null): Record<string, string> {
  if (!page) return {}
  const values: Record<string, string> = {}
  for (const field of page.fields) {
    if (field.type === 'label' || field.type === 'unknown') continue
    if (field.type === 'checkbox') values[field.id] = field.value ? 'true' : 'false'
    else if (field.type === 'dropdown') values[field.id] = field.value || field.items[0]?.id || ''
    else values[field.id] = field.value ?? ''
  }
  return values
}

function setupActionSignature(info: IntegrationSetupInfo | null): string | null {
  if (!info || info.state !== 'WAIT_USER_ACTION') return null
  return JSON.stringify(info.action ?? null)
}

function imageSource(value: string): string {
  if (/^data:image\/(?:png|jpe?g|svg\+xml);base64,/i.test(value)) return value
  if (value.startsWith('iVBOR')) return `data:image/png;base64,${value}`
  if (value.startsWith('/9j/')) return `data:image/jpeg;base64,${value}`
  if (value.startsWith('PHN2Zy') || value.startsWith('PD94bWw')) return `data:image/svg+xml;base64,${value}`
  return `data:image/png;base64,${value}`
}

function inlineMarkdown(text: string, keyPrefix: string): ReactNode[] {
  const tokenPattern = /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)|\*\*([^*]+)\*\*|__([^_]+)__|`([^`]+)`|\*([^*\n]+)\*|_([^_\n]+)_|(https?:\/\/[^\s<]+)/g
  const nodes: ReactNode[] = []
  let cursor = 0
  let index = 0
  for (const match of text.matchAll(tokenPattern)) {
    const start = match.index ?? 0
    if (start > cursor) nodes.push(text.slice(cursor, start))
    const key = `${keyPrefix}-${index++}`
    if (match[1] && match[2]) {
      nodes.push(<a key={key} href={match[2]} target="_blank" rel="noreferrer">{match[1]}<ExternalLink aria-hidden="true" /></a>)
    } else if (match[3] || match[4]) {
      nodes.push(<strong key={key}>{match[3] || match[4]}</strong>)
    } else if (match[5]) {
      nodes.push(<code key={key}>{match[5]}</code>)
    } else if (match[6] || match[7]) {
      nodes.push(<em key={key}>{match[6] || match[7]}</em>)
    } else if (match[8]) {
      nodes.push(<a key={key} href={match[8]} target="_blank" rel="noreferrer">{match[8]}<ExternalLink aria-hidden="true" /></a>)
    }
    cursor = start + match[0].length
  }
  if (cursor < text.length) nodes.push(text.slice(cursor))
  return nodes
}

function MarkdownText({ text }: { text: string }) {
  if (!text) return null
  const lines = text.replace(/\r\n?/g, '\n').split('\n')
  const blocks: ReactNode[] = []
  let index = 0

  while (index < lines.length) {
    const line = lines[index]
    if (!line.trim()) { index += 1; continue }

    const heading = line.match(/^(#{1,6})\s+(.+)$/)
    if (heading) {
      blocks.push(<h4 key={`heading-${index}`} className={`md-heading level-${heading[1].length}`}>{inlineMarkdown(heading[2], `heading-${index}`)}</h4>)
      index += 1
      continue
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      const items: ReactNode[] = []
      while (index < lines.length && /^\s*[-*+]\s+/.test(lines[index])) {
        const content = lines[index].replace(/^\s*[-*+]\s+/, '')
        items.push(<li key={`ul-${index}`}>{inlineMarkdown(content, `ul-${index}`)}</li>)
        index += 1
      }
      blocks.push(<ul key={`ul-block-${index}`}>{items}</ul>)
      continue
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items: ReactNode[] = []
      while (index < lines.length && /^\s*\d+[.)]\s+/.test(lines[index])) {
        const content = lines[index].replace(/^\s*\d+[.)]\s+/, '')
        items.push(<li key={`ol-${index}`}>{inlineMarkdown(content, `ol-${index}`)}</li>)
        index += 1
      }
      blocks.push(<ol key={`ol-block-${index}`}>{items}</ol>)
      continue
    }

    if (/^\s*>\s?/.test(line)) {
      const quote: string[] = []
      const start = index
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quote.push(lines[index].replace(/^\s*>\s?/, ''))
        index += 1
      }
      blocks.push(<blockquote key={`quote-${start}`}>{quote.map((part, partIndex) => <Fragment key={`quote-${start}-${partIndex}`}>{inlineMarkdown(part, `quote-${start}-${partIndex}`)}{partIndex < quote.length - 1 && <br />}</Fragment>)}</blockquote>)
      continue
    }

    const paragraph: string[] = []
    const start = index
    while (index < lines.length && lines[index].trim() && !/^(#{1,6})\s+/.test(lines[index]) && !/^\s*[-*+]\s+/.test(lines[index]) && !/^\s*\d+[.)]\s+/.test(lines[index]) && !/^\s*>\s?/.test(lines[index])) {
      paragraph.push(lines[index])
      index += 1
    }
    blocks.push(<p key={`paragraph-${start}`}>{paragraph.map((part, partIndex) => <Fragment key={`paragraph-${start}-${partIndex}`}>{inlineMarkdown(part, `paragraph-${start}-${partIndex}`)}{partIndex < paragraph.length - 1 && <br />}</Fragment>)}</p>)
  }

  return <div className="setup-rich-text">{blocks}</div>
}

function SetupForm({ page, submitLabel, busy, onSubmit, secondaryAction }: { page: IntegrationSetupPage | null; submitLabel: string; busy: boolean; onSubmit: (values: Record<string, string>) => void; secondaryAction?: ReactNode }) {
  const defaults = useMemo(() => initialValues(page), [page])
  const [values, setValues] = useState<Record<string, string>>(defaults)

  useEffect(() => setValues(defaults), [defaults])

  const setValue = (id: string, value: string) => setValues(current => ({ ...current, [id]: value }))
  const renderField = (field: IntegrationSetupField) => {
    if (field.type === 'label') return <div className="setup-info-field" key={field.id}>{field.label && field.label !== field.text && <strong className="setup-info-title">{field.label}</strong>}<MarkdownText text={field.text || field.label} /></div>
    if (field.type === 'unknown') return <div className="notice warning setup-field-warning" key={field.id}><AlertTriangle /> Unsupported setup field: {field.label || field.id}</div>
    if (field.type === 'checkbox') return <label className="setup-checkbox" key={field.id}><input type="checkbox" checked={values[field.id] === 'true'} onChange={event => setValue(field.id, event.target.checked ? 'true' : 'false')} /><span>{field.label}</span></label>
    if (field.type === 'dropdown') return <label className="setup-field" key={field.id}><span>{field.label}</span><select value={values[field.id] ?? ''} onChange={event => setValue(field.id, event.target.value)} required>{field.items.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}</select></label>
    if (field.type === 'textarea') return <label className="setup-field" key={field.id}><span>{field.label}</span><textarea value={values[field.id] ?? ''} onChange={event => setValue(field.id, event.target.value)} rows={5} /></label>
    if (field.type === 'number') return <label className="setup-field" key={field.id}><span>{field.label}</span><div className="setup-number"><input type="number" value={values[field.id] ?? ''} min={field.min ?? undefined} max={field.max ?? undefined} step={field.step ?? (field.decimals ? Math.pow(10, -field.decimals) : 1)} onChange={event => setValue(field.id, event.target.value)} required />{field.unit && <em>{field.unit}</em>}</div></label>
    return <label className="setup-field" key={field.id}><span>{field.label}</span><input type={field.type === 'password' ? 'password' : 'text'} value={values[field.id] ?? ''} pattern={field.regex ?? undefined} onChange={event => setValue(field.id, event.target.value)} /></label>
  }

  return <form className="integration-setup-form" onSubmit={event => { event.preventDefault(); onSubmit(values) }}>
    {page?.fields.map(renderField)}
    {!page?.fields.length && <div className="setup-info-field"><p>No initial settings are required. Continue to start the integration setup.</p></div>}
    <div className="setup-dialog-actions">{secondaryAction}<button className="primary-action" type="submit" disabled={busy}>{busy ? <LoaderCircle className="spin" /> : <Settings2 />}{submitLabel}</button></div>
  </form>
}


function entityTypeLabel(value: string): string {
  return value.replaceAll('_', ' ').replace(/\b\w/g, character => character.toUpperCase())
}

function entityReference(entityId: string): string {
  const uuid = entityId.match(/(?:^|[.:])uuid:([0-9a-f-]+)$/i)
  return uuid ? `uuid:${uuid[1]}` : entityId
}

function EntityTypeIcon({ type }: { type: string }) {
  const normalized = type.toLowerCase()
  const Icon = normalized === 'remote' ? Gamepad2
    : normalized === 'media_player' ? MonitorPlay
      : normalized === 'light' ? Lightbulb
        : normalized === 'switch' ? ToggleLeft
          : normalized === 'climate' ? Thermometer
            : normalized === 'select' ? ListFilter
              : normalized === 'button' ? CircleDot
                : normalized === 'cover' ? SlidersHorizontal
                  : normalized.includes('sensor') ? Gauge
                    : Box
  return <Icon aria-hidden="true" />
}

function entityMatchesSearch(entity: IntegrationSetupEntity, query: string): boolean {
  if (!query) return true
  return [entity.name, entity.id, entity.type, entity.area, entity.deviceClass, entity.description]
    .some(value => value.toLowerCase().includes(query))
}

function SetupEntityRow({
  entity,
  action,
  busy,
  onAction,
}: {
  entity: IntegrationSetupEntity
  action: 'add' | 'remove'
  busy: boolean
  onAction: (entityId: string) => void
}) {
  const tooltip = `${entityTypeLabel(entity.type)} · ${entityReference(entity.id)}`
  const rowRef = useRef<HTMLDivElement>(null)
  const [tooltipPosition, setTooltipPosition] = useState<{ top: number; left: number; maxWidth: number } | null>(null)

  const showTooltip = () => {
    const row = rowRef.current
    if (!row) return
    const rect = row.getBoundingClientRect()
    const maxWidth = Math.min(420, Math.max(220, window.innerWidth - 24))
    const halfWidth = maxWidth / 2
    const left = Math.min(window.innerWidth - 12 - halfWidth, Math.max(12 + halfWidth, rect.left + rect.width / 2))
    setTooltipPosition({ top: Math.max(12, rect.top - 8), left, maxWidth })
  }

  useEffect(() => {
    if (!tooltipPosition) return
    const hideTooltip = () => setTooltipPosition(null)
    window.addEventListener('resize', hideTooltip)
    window.addEventListener('scroll', hideTooltip, true)
    return () => {
      window.removeEventListener('resize', hideTooltip)
      window.removeEventListener('scroll', hideTooltip, true)
    }
  }, [tooltipPosition])

  return <>
    <div
      ref={rowRef}
      className="setup-entity-transfer-row"
      tabIndex={0}
      onMouseEnter={showTooltip}
      onMouseLeave={() => setTooltipPosition(null)}
      onFocus={showTooltip}
      onBlur={() => setTooltipPosition(null)}
    >
      <span className="setup-entity-type-icon"><EntityTypeIcon type={entity.type} /></span>
      <strong>{entity.name}</strong>
      <button
        className={`setup-entity-transfer-action ${action}`}
        type="button"
        disabled={busy}
        aria-label={`${action === 'add' ? 'Add' : 'Remove'} ${entity.name}`}
        title={`${action === 'add' ? 'Add' : 'Remove'} ${entity.name}`}
        onClick={() => onAction(entity.id)}
      >
        {action === 'add' ? <Plus /> : <Minus />}
      </button>
    </div>
    {tooltipPosition && createPortal(
      <div
        className="setup-entity-tooltip"
        role="tooltip"
        style={{ top: tooltipPosition.top, left: tooltipPosition.left, maxWidth: tooltipPosition.maxWidth }}
      >
        {tooltip}
      </div>,
      document.body,
    )}
  </>
}

function SetupEntityPicker({
  availableEntities,
  configuredEntities,
  busy,
  onAdd,
  onRemove,
  onBack,
  onDone,
}: {
  availableEntities: IntegrationSetupEntity[]
  configuredEntities: IntegrationSetupEntity[]
  busy: boolean
  onAdd: (entityIds: string[]) => Promise<boolean>
  onRemove: (entityIds: string[]) => Promise<boolean>
  onBack: () => void
  onDone: () => void
}) {
  const [search, setSearch] = useState('')
  const query = search.trim().toLowerCase()
  const visibleAvailable = useMemo(
    () => availableEntities.filter(entity => entityMatchesSearch(entity, query)),
    [availableEntities, query],
  )
  const visibleConfigured = useMemo(
    () => configuredEntities.filter(entity => entityMatchesSearch(entity, query)),
    [configuredEntities, query],
  )

  return <div className="setup-entity-transfer">
    <div className="setup-entity-step-heading">
      <div><strong>Add entities</strong><span>Choose which entities from this integration should be available on the Remote.</span></div>
    </div>

    <label className="setup-entity-search">
      <Search aria-hidden="true" />
      <input
        type="search"
        value={search}
        onChange={event => setSearch(event.target.value)}
        placeholder="Search entities"
        aria-label="Search entities"
      />
    </label>

    <div className="setup-entity-columns">
      <section className="setup-entity-column">
        <header><strong>Available entities</strong><span>{availableEntities.length}</span></header>
        <div className="setup-entity-transfer-list">
          {visibleAvailable.map(entity => <SetupEntityRow key={entity.id} entity={entity} action="add" busy={busy} onAction={entityId => void onAdd([entityId])} />)}
          {!visibleAvailable.length && <div className="setup-entity-empty">{query ? 'No available entities match your search.' : 'No entities available to add.'}</div>}
        </div>
      </section>

      <section className="setup-entity-column">
        <header><strong>Added entities</strong><span>{configuredEntities.length}</span></header>
        <div className="setup-entity-transfer-list">
          {visibleConfigured.map(entity => <SetupEntityRow key={entity.id} entity={entity} action="remove" busy={busy} onAction={entityId => void onRemove([entityId])} />)}
          {!visibleConfigured.length && <div className="setup-entity-empty">{query ? 'No added entities match your search.' : 'No entities have been added yet.'}</div>}
        </div>
      </section>
    </div>

    <div className="setup-entity-navigation">
      <button className="secondary-action" type="button" disabled={busy} onClick={onBack}><ArrowLeft />Back</button>
      <button className="primary-action" type="button" disabled={busy} onClick={onDone}>Done</button>
    </div>
  </div>
}

export function IntegrationSetupModal({ item, close }: { item: Integration; close: () => void }) {
  const queryClient = useQueryClient()
  const [setupQuerySession] = useState(() => `${Date.now()}-${Math.random()}`)
  const definition = useQuery({
    queryKey: ['integration-setup', item.id, setupQuerySession],
    queryFn: () => api.integrationSetup(item.id),
    staleTime: 0,
    gcTime: 0,
    retry: false,
    refetchOnMount: 'always',
    refetchOnWindowFocus: false,
  })
  const [setup, setSetup] = useState<IntegrationSetupInfo | null>(null)
  const [busy, setBusy] = useState(false)
  const [localError, setLocalError] = useState<string | null>(null)
  const [entityBusy, setEntityBusy] = useState(false)
  const [entityError, setEntityError] = useState<string | null>(null)
  const [postSetupStep, setPostSetupStep] = useState<'complete' | 'entities'>('complete')
  const [manageEntitiesDirectly, setManageEntitiesDirectly] = useState(false)
  const initializedFromDefinition = useRef(false)
  const isReconfigure = item.connectionState !== 'not_configured' && (item.installState === 'configured' || Boolean(item.instanceId))
  const entitiesQuery = useQuery({
    queryKey: ['integration-setup-entities', item.id, item.instanceId ?? 'new'],
    queryFn: () => api.integrationSetupEntities(item.id, item.instanceId),
    enabled: manageEntitiesDirectly || (setup?.state === 'OK' && postSetupStep === 'entities'),
    staleTime: 0,
    gcTime: 0,
    retry: (failureCount, error) => failureCount < 5 && error instanceof ApiError && (error.status === 404 || error.status === 503),
    retryDelay: attempt => Math.min(500 * 2 ** attempt, 3_000),
    refetchOnWindowFocus: false,
  })

  useEffect(() => {
    if (initializedFromDefinition.current || !definition.isFetchedAfterMount || !definition.data) return
    initializedFromDefinition.current = true
    const activeSetup = definition.data.activeSetup
    setSetup(activeSetup && (activeSetup.state === 'SETUP' || activeSetup.state === 'WAIT_USER_ACTION') ? activeSetup : null)
  }, [definition.data, definition.isFetchedAfterMount])

  useEffect(() => {
    if (setup?.state !== 'SETUP') return
    let cancelled = false
    let timer = 0
    let missingStatusCount = 0
    const poll = async () => {
      try {
        const next = await api.integrationSetupStatus(item.id)
        if (cancelled) return
        missingStatusCount = 0
        setLocalError(null)
        setSetup(next)
        if (next.state === 'SETUP') timer = window.setTimeout(() => void poll(), 900)
      } catch (error) {
        if (cancelled) return
        if (error instanceof ApiError && error.status === 404) {
          missingStatusCount += 1
          setLocalError(missingStatusCount >= 6 ? 'The Remote no longer reports this setup session. It may have finished or been cancelled.' : null)
          timer = window.setTimeout(() => void poll(), 750)
          return
        }
        missingStatusCount = 0
        setLocalError(error instanceof Error ? error.message : 'Unable to read setup status')
        timer = window.setTimeout(() => void poll(), 1_500)
      }
    }
    timer = window.setTimeout(() => void poll(), 650)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [item.id, setup?.state])

  useEffect(() => {
    if (setup?.state !== 'OK') return
    void queryClient.invalidateQueries({ queryKey: ['installed', 'integrations'] })
    void queryClient.invalidateQueries({ queryKey: ['catalog', 'integrations'] })
  }, [queryClient, setup?.state])

  const waitForSubmittedActionToAdvance = async (
    previousSignature: string | null,
    initial: IntegrationSetupInfo,
  ): Promise<IntegrationSetupInfo> => {
    if (!previousSignature) return initial

    let next = initial
    for (let attempt = 0; attempt < 30; attempt += 1) {
      if (next.state !== 'WAIT_USER_ACTION' || setupActionSignature(next) !== previousSignature) return next

      await new Promise<void>(resolve => window.setTimeout(resolve, attempt < 4 ? 250 : 750))
      try {
        next = await api.integrationSetupStatus(item.id)
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) continue
        throw error
      }
    }

    throw new Error('The Remote accepted this setup step but did not expose the next setup step yet. Please try again.')
  }

  const run = async (operation: () => Promise<IntegrationSetupInfo>, followDynamicStep = false) => {
    const previousSignature = followDynamicStep ? setupActionSignature(setup) : null
    setBusy(true)
    setLocalError(null)
    try {
      const immediate = await operation()
      const next = followDynamicStep
        ? await waitForSubmittedActionToAdvance(previousSignature, immediate)
        : immediate
      setSetup(next)
    }
    catch (error) { setLocalError(error instanceof Error ? error.message : 'Integration setup failed') }
    finally { setBusy(false) }
  }

  const clearSetupCacheAndClose = () => {
    queryClient.removeQueries({ queryKey: ['integration-setup', item.id], exact: false })
    queryClient.removeQueries({ queryKey: ['integration-setup-entities', item.id], exact: false })
    close()
  }

  const addSelectedEntities = async (entityIds: string[]): Promise<boolean> => {
    if (!entityIds.length || entityBusy) return false
    setEntityBusy(true)
    setEntityError(null)
    try {
      await api.addIntegrationSetupEntities(
        item.id,
        entityIds,
        entitiesQuery.data?.integrationId ?? item.instanceId,
      )
      await entitiesQuery.refetch()
      void queryClient.invalidateQueries({ queryKey: ['installed', 'integrations'] })
      void queryClient.invalidateQueries({ queryKey: ['catalog', 'integrations'] })
      return true
    } catch (error) {
      setEntityError(error instanceof Error ? error.message : 'Unable to add selected entities')
      return false
    } finally {
      setEntityBusy(false)
    }
  }

  const removeSelectedEntities = async (entityIds: string[]): Promise<boolean> => {
    if (!entityIds.length || entityBusy) return false
    setEntityBusy(true)
    setEntityError(null)
    try {
      await api.removeIntegrationSetupEntities(
        item.id,
        entityIds,
        entitiesQuery.data?.integrationId ?? item.instanceId,
      )
      await entitiesQuery.refetch()
      void queryClient.invalidateQueries({ queryKey: ['installed', 'integrations'] })
      void queryClient.invalidateQueries({ queryKey: ['catalog', 'integrations'] })
      return true
    } catch (error) {
      setEntityError(error instanceof Error ? error.message : 'Unable to remove selected entities')
      return false
    } finally {
      setEntityBusy(false)
    }
  }

  const abortAndClose = () => {
    if (setup && (setup.state === 'SETUP' || setup.state === 'WAIT_USER_ACTION')) void api.abortIntegrationSetup(item.id).catch(() => undefined)
    clearSetupCacheAndClose()
  }

  const title = `${isReconfigure ? 'Reconfigure' : 'Configure'} — ${item.name}`
  const showEntityStep = manageEntitiesDirectly || (setup?.state === 'OK' && postSetupStep === 'entities')
  const backFromEntityStep = () => {
    setEntityError(null)
    if (manageEntitiesDirectly) setManageEntitiesDirectly(false)
    else setPostSetupStep('complete')
  }
  const error = localError || (setup?.state === 'ERROR' ? `Setup failed: ${setup.error === 'NONE' ? 'Unknown error' : setup.error.replaceAll('_', ' ').toLowerCase()}` : null)

  return <Modal title={title} close={abortAndClose} className="integration-setup-modal"><div className="integration-setup-dialog">
    {definition.isLoading && <div className="setup-progress"><LoaderCircle className="spin" /><div><strong>Loading setup</strong><span>Reading the integration's configuration schema from the Remote…</span></div></div>}
    {definition.isError && <div className="notice error"><CircleX /> {definition.error.message}</div>}
    {error && <div className="notice error"><CircleX /> {error}</div>}

    {!definition.isLoading && definition.data && !setup && !manageEntitiesDirectly && <>
      <div className="setup-intro"><p>{isReconfigure ? 'This starts the integration’s reconfiguration flow on the Remote. You can also manage entities that are already available without reconfiguring the integration.' : 'Complete the integration setup here. The following fields are provided dynamically by the integration driver.'}</p></div>
      {definition.data.setupDataSchema?.title && <h3>{definition.data.setupDataSchema.title}</h3>}
      <SetupForm
        page={definition.data.setupDataSchema}
        submitLabel={isReconfigure ? 'Start reconfiguration' : 'Start setup'}
        busy={busy}
        onSubmit={values => void run(() => api.startIntegrationSetup(item.id, values, isReconfigure))}
        secondaryAction={isReconfigure ? <button className="secondary-action" type="button" disabled={busy} onClick={() => { setEntityError(null); setManageEntitiesDirectly(true) }}><ListFilter />Add available entities</button> : undefined}
      />
    </>}

    {setup?.state === 'SETUP' && <div className="setup-progress"><LoaderCircle className="spin" /><div><strong>Integration setup is running</strong><span>The driver is configuring the integration. This can take a moment.</span></div></div>}

    {setup?.state === 'WAIT_USER_ACTION' && setup.action?.type === 'input' && <>
      {setup.action.page?.title && <h3>{setup.action.page.title}</h3>}
      <SetupForm page={setup.action.page} submitLabel="Continue" busy={busy} onSubmit={values => void run(() => api.submitIntegrationSetupInput(item.id, values), true)} />
    </>}

    {setup?.state === 'WAIT_USER_ACTION' && setup.action?.type === 'confirmation' && <div className="setup-confirmation">
      <h3>{setup.action.title}</h3>
      <MarkdownText text={setup.action.message1} />
      {setup.action.image && <img src={imageSource(setup.action.image)} alt="Integration setup instruction" />}
      <MarkdownText text={setup.action.message2} />
      <div className="setup-dialog-actions"><button className="secondary-action" type="button" disabled={busy} onClick={() => void run(() => api.confirmIntegrationSetup(item.id, false), true)}>No / Cancel</button><button className="primary-action" type="button" disabled={busy} onClick={() => void run(() => api.confirmIntegrationSetup(item.id, true), true)}>{busy ? <LoaderCircle className="spin" /> : <CheckCircle2 />}Continue</button></div>
    </div>}

    {setup?.state === 'WAIT_USER_ACTION' && !setup.action && <div className="notice warning"><AlertTriangle /> The integration is waiting for user action but did not provide an action definition.</div>}

    {setup?.state === 'OK' && postSetupStep === 'complete' && <>
      <div className="setup-success"><CheckCircle2 /><div><strong>Configuration completed</strong><span>{item.name} completed its configuration successfully.</span></div></div>
      <div className="setup-dialog-actions">
        <button className="secondary-action" type="button" onClick={clearSetupCacheAndClose}>Done</button>
        <button className="primary-action" type="button" onClick={() => { setEntityError(null); setPostSetupStep('entities') }}>Add entities</button>
      </div>
    </>}

    {showEntityStep && <>
      {entityError && <div className="notice error"><CircleX /> {entityError}</div>}

      {entitiesQuery.isLoading && <div className="setup-progress"><LoaderCircle className="spin" /><div><strong>Loading entities</strong><span>Reading available and already added entities from the Remote…</span></div></div>}

      {entitiesQuery.isError && <div className="setup-entity-fallback">
        <div className="notice warning"><AlertTriangle /> {entitiesQuery.error instanceof Error ? entitiesQuery.error.message : 'Unable to load entities'}</div>
        <div className="setup-entity-navigation"><button className="secondary-action" type="button" onClick={backFromEntityStep}><ArrowLeft />Back</button><button className="primary-action" type="button" onClick={() => void entitiesQuery.refetch()}>Try again</button></div>
      </div>}

      {!entitiesQuery.isLoading && !entitiesQuery.isError && entitiesQuery.data && <SetupEntityPicker
        availableEntities={entitiesQuery.data.availableEntities}
        configuredEntities={entitiesQuery.data.configuredEntities}
        busy={entityBusy}
        onAdd={addSelectedEntities}
        onRemove={removeSelectedEntities}
        onBack={backFromEntityStep}
        onDone={clearSetupCacheAndClose}
      />}
    </>}

    {setup?.state !== 'OK' && !showEntityStep && <div className="setup-footer"><button className="secondary-action" type="button" disabled={busy} onClick={abortAndClose}>Cancel</button></div>}
  </div></Modal>
}

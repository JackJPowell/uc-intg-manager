import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import { DiagnosticsPage } from './DiagnosticsPage'
import { activityCleanupDescription, type ActivityCleanupSelection } from '../lib/activityCleanup'

function renderDiagnostics(activeRemoteId = 'living-room', scanError = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['bootstrap'], { activeRemoteId, remotes: [{ id: activeRemoteId, name: activeRemoteId }] })
  for (const kind of ['orphaned', 'unused']) client.setQueryData(['diagnostics', 'living-room', kind], {
    activities: { movie: { name: 'Watch movie', entities: [{ entity_id: `light.${kind}`, localized_name: `${kind} light` }] } },
  })
  if (scanError) for (const kind of ['orphaned', 'unused']) {
    client.getQueryCache().find({ queryKey: ['diagnostics', 'living-room', kind] })?.setState({ status: 'error', error: new Error('Activity scan failed midway'), fetchStatus: 'idle' })
  }
  const html = renderToStaticMarkup(<QueryClientProvider client={client}><DiagnosticsPage /></QueryClientProvider>)
  client.clear()
  return html
}

describe('Main diagnostics activity cleanup', () => {
  it('offers a removal action on both orphaned and unused entities', () => {
    const html = renderDiagnostics()
    expect(html.match(/Remove from activity/g)).toHaveLength(2)
    expect(html).toContain('orphaned light')
    expect(html).toContain('unused light')
  })

  it('does not reuse another Remote’s cached scan results', () => {
    const html = renderDiagnostics('bedroom')
    expect(html).not.toContain('Remove from activity')
    expect(html).not.toContain('orphaned light')
    expect(html).not.toContain('unused light')
  })

  it('hides cached findings and cleanup controls when a rescan fails', () => {
    const html = renderDiagnostics('living-room', true)
    expect(html.match(/Activity scan failed midway/g)).toHaveLength(2)
    expect(html).not.toContain('Remove from activity')
    expect(html).not.toContain('orphaned light')
    expect(html).not.toContain('unused light')
    expect(html).not.toContain('No orphaned entity references found')
    expect(html).not.toContain('Every activity entity is currently in use')
  })

  it('explains the different effects of missing and unused cleanup', () => {
    const selection: ActivityCleanupSelection = { kind: 'orphaned', remoteId: 'living-room', activityId: 'movie', activityName: 'Watch movie', entityId: 'light.old', entityName: 'Old light' }
    expect(activityCleanupDescription(selection)).toContain('removes its activity steps, buttons and widgets')
    expect(activityCleanupDescription(selection)).toContain('Other activities and configured Remote entities are kept')
    expect(activityCleanupDescription({ ...selection, kind: 'unused' })).toContain('Removal is skipped if the entity is now referenced')
  })
})

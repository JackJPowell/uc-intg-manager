import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import type { Integration } from '../lib/models'
import { IssueList, RemoteMessages, RemotePage } from './RemotePage'

function renderUpdates(items: Partial<Integration>[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['bootstrap'], { activeRemoteId: 'remote-a', remotes: [{ id: 'remote-a', name: 'Living room' }] })
  client.setQueryData(['remote-view', 'remote-a', 'status'], { online: true, docked: true })
  client.setQueryData(['remote-view', 'remote-a', 'integrations'], items.map((item, index) => ({
    id: `integration-${index}`, name: `Integration ${index}`, updateAvailable: true,
    version: '1.0', latestVersion: '2.0', capabilities: { update: true }, inplaceUpgradeAvailable: true, ...item,
  })))
  const html = renderToStaticMarkup(<QueryClientProvider client={client}><RemotePage /></QueryClientProvider>)
  client.clear()
  return html
}

describe('Remote update controls', () => {
  it('offers updates only for integrations with checked firmware support and update permission', () => {
    const html = renderUpdates([
      { name: 'Allowed' },
      { name: 'Unchecked', inplaceUpgradeAvailable: null },
      { name: 'Old firmware', inplaceUpgradeAvailable: false },
      { name: 'External', capabilities: { update: false } as Integration['capabilities'] },
      { name: 'Manager', capabilities: { update: true, selfUpdate: true } as Integration['capabilities'] },
    ])
    expect(html.match(/class="remote-primary"/g)).toHaveLength(1)
    expect(html).toContain('Compatible firmware is required')
    expect(html).toContain('Manage this update in the full manager')
    expect(html).toContain('Update Integration Manager from the full manager')
  })

  it('shows an empty state when no integration has an update', () => {
    const html = renderUpdates([{ updateAvailable: false }])
    expect(html).toContain('All up to date')
    expect(html).not.toContain('>Update</button>')
  })
})

// Render the shared page sections directly so read-only/mutation affordances
// can be checked independently of viewport and routing.
describe('Remote issue and message sections', () => {
  it('shows disconnected/error integrations even without updates', () => {
    const html = renderUpdates([
      { name: 'Lost connection', updateAvailable: false, connectionState: 'disconnected' },
      { name: 'Failed connection', updateAvailable: false, connectionState: 'error' },
      { name: 'Healthy', updateAvailable: false, connectionState: 'connected' },
    ])
    expect(html).toContain('Needs attention')
    expect(html).toContain('Lost connection')
    expect(html).toContain('Failed connection')
    expect(html).not.toContain('Healthy')
  })

  it('supports cleanup actions for both missing and unused findings', () => {
    const result = { activities: { movie: { name: 'Watch TV', entities: [{ entity_id: 'light.old', localized_name: 'Old light' }] } } }
    const props = { result, loading: false, error: null, retry: () => {} }
    const missing = renderToStaticMarkup(<IssueList title="Missing entities" {...props} onRemove={() => {}} />)
    const unused = renderToStaticMarkup(<IssueList title="Unused" {...props} onRemove={() => {}} />)
    const offline = renderToStaticMarkup(<IssueList title="Unused" {...props} onRemove={() => {}} disabled />)
    expect(missing).toContain('Remove from activity')
    expect(unused).toContain('Remove from activity')
    expect(offline).toContain('disabled=""')
  })

  it('never displays stale scan findings or a clean result after an error', () => {
    const html = renderToStaticMarkup(<IssueList title="Unused" result={{ activities: { movie: { name: 'Cached activity', entities: [{ entity_id: 'light.stale' }] } } }} loading={false} error={new Error('Scan failed')} retry={() => {}} onRemove={() => {}} />)
    expect(html).toContain('Scan failed')
    expect(html).not.toContain('None found')
    expect(html).not.toContain('Cached activity')
    expect(html).not.toContain('light.stale')
    expect(html).not.toContain('Remove from activity')
  })

  it('shows unread counts, priority, and expandable text without executing message HTML', () => {
    const html = renderToStaticMarkup(<RemoteMessages messages={[{ id: 'notice', title: 'Release notice', date: '2026-11-21', priority: 'high', content: '<script>ignored()</script>' + 'More details. '.repeat(30) }]} />)
    expect(html).toContain('Unread messages <span>1</span>')
    expect(html).toContain('high priority')
    expect(html).toContain('Read full message')
    expect(html).not.toContain('<script>')
  })
})

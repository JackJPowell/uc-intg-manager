import type { Bootstrap, Integration, SettingsPayload } from './models'

type Envelope<T> = { data: T }
type ErrorEnvelope = { error?: { code?: string; message?: string } }
export type FirmwareStatus = { installedVersion: string; updateAvailable: boolean; availableVersion?: string; title?: string; releaseNotesUrl?: string; inProgress: boolean; state: string; updatePercent: number; downloadPercent: number; currentStep: number; totalSteps: number; currentStepPercent: number }

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message)
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    headers: { Accept: 'application/json', ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...init?.headers },
    credentials: 'same-origin',
    ...init,
  })
  const payload = (await response.json().catch(() => ({}))) as Envelope<T> & ErrorEnvelope
  if (!response.ok) throw new ApiError(response.status, payload.error?.message ?? 'Request failed')
  return payload.data
}

export const api = {
  bootstrap: () => request<Bootstrap>('/bootstrap'),
  status: () => request<{ online: boolean; docked: boolean | null }>('/status'),
  integrations: () => request<Integration[]>('/integrations'),
  catalog: () => request<Integration[]>('/catalog/integrations'),
  refreshIntegrations: () => request<{ refreshed: boolean }>('/integrations/refresh', { method: 'POST' }),
  installIntegration: (id: string, version?: string) => request<{ integration: Integration; message: string }>(`/integrations/${encodeURIComponent(id)}/install${version ? `?version=${encodeURIComponent(version)}` : ''}`, { method: 'POST' }),
  updateIntegration: (id: string, version?: string) => request<{ integration: Integration; reconnecting: boolean }>(`/integrations/${encodeURIComponent(id)}/update${version ? `?version=${encodeURIComponent(version)}` : ''}`, { method: 'POST' }),
  integrationVersions: (owner: string, repo: string, id: string, all = false, selfUpdate = false) => {
    const params = new URLSearchParams()
    if (all) params.set('all', 'true')
    if (selfUpdate) params.set('self_update', 'true')
    const query = params.size ? `?${params}` : ''
    return request<{ releases: Array<{ tag_name: string; name: string; published_at: string; is_beta: boolean }>; versionFloor: string | null }>(`/version-selector/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/${encodeURIComponent(id)}${query}`)
  },
  selfUpdate: (version?: string) => request<{ started: boolean; targetVersion: string }>('/self-update/inplace', { method: 'POST', body: JSON.stringify(version ? { version } : {}) }),
  managerHealth: async () => {
    const response = await fetch('/health', { cache: 'no-store', credentials: 'same-origin' })
    if (!response.ok) throw new ApiError(response.status, 'Manager is not available yet')
    return response.text()
  },
  backupIntegration: (id: string) => request<{ driverId: string; hasData: boolean }>(`/integrations/${encodeURIComponent(id)}/backup`, { method: 'POST' }),
  deleteIntegration: (id: string, scope: 'configuration' | 'full') => request<{ driverId?: string; removed: boolean }>(`/integrations/${encodeURIComponent(id)}`, { method: 'DELETE', body: JSON.stringify({ scope }) }),
  settings: () => request<SettingsPayload>('/settings'),
  saveSettings: (payload: SettingsPayload) => request<SettingsPayload>('/settings', { method: 'PUT', body: JSON.stringify(payload) }),
  operationLock: () => request<{ locked: boolean; elapsedSeconds: number | null }>('/operations/lock'),
  releaseOperationLock: () => request<{ wasLocked: boolean; elapsedSeconds: number | null }>('/operations/lock/release', { method: 'POST' }),
  logs: () => request<Array<{ timestamp: string; level: string; logger: string; message: string }>>('/logs'),
  clearLogs: () => request<{ cleared: boolean }>('/logs', { method: 'DELETE' }),
  systemMessages: () => request<{ unread: SystemMessage[]; read: SystemMessage[] }>('/system-messages'),
  refreshSystemMessages: () => request<{ refreshed: boolean }>('/system-messages/refresh', { method: 'POST' }),
  notifications: () => request<NotificationSettings>('/notifications'),
  saveNotifications: (settings: NotificationSettings) => request<NotificationSettings>('/notifications', { method: 'PUT', body: JSON.stringify(settings) }),
  testNotification: async (provider: 'home-assistant' | 'webhook' | 'discord' | 'ntfy' | 'pushover', settings?: Record<string, unknown>) => {
    const response = await fetch(`/api/v1/notifications/${provider}/test`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(settings ?? {}), credentials: 'same-origin' })
    const payload = await response.json().catch(() => ({})) as { error?: string }
    if (!response.ok) throw new ApiError(response.status, payload.error ?? 'Test notification failed')
  },
  homeAssistantServices: async () => {
    const response = await fetch('/api/v1/notifications/home-assistant/services', { credentials: 'same-origin' })
    const payload = await response.json().catch(() => ({})) as { error?: string; services?: string[] }
    if (!response.ok) throw new ApiError(response.status, payload.error ?? 'Unable to load Home Assistant services')
    return payload.services ?? []
  },
  checkFirmware: () => request<FirmwareStatus>('/diagnostics/system-update', { method: 'POST' }),
  firmwareUpdateStatus: () => request<FirmwareStatus>('/diagnostics/system-update/status'),
  installFirmware: () => request<FirmwareStatus>('/diagnostics/system-update/install', { method: 'POST' }),
  orphanedEntities: () => request<DiagnosticActivityResult>('/diagnostics/orphaned-entities'),
  unusedActivityEntities: () => request<DiagnosticActivityResult>('/diagnostics/unused-activity-entities'),
  orphanedIrCodesets: () => request<IrCodeset[]>('/diagnostics/orphaned-ir-codesets'),
  deleteIrCodeset: (deviceId: string) => request<{ deleted: boolean }>(`/diagnostics/ir-codesets/${encodeURIComponent(deviceId)}`, { method: 'DELETE' }),
  reassociateIrCodeset: (deviceId: string, remoteName: string) => request<{ created: boolean }>('/diagnostics/ir-codesets/reassociate', { method: 'POST', body: JSON.stringify({ device_id: deviceId, remote_name: remoteName }) }),
  rebootRemote: () => request<{ sent: boolean; message: string }>('/diagnostics/reboot', { method: 'POST' }),
  powerOffRemote: () => request<{ sent: boolean; message: string }>('/diagnostics/power-off', { method: 'POST' }),
  integrationLogServices: () => request<Array<{ id: string; name: string }>>('/integration-logs/services'),
  integrationLogs: (services: string[], priority: number) => request<Array<Record<string, unknown>>>(`/integration-logs?services=${encodeURIComponent(services.join(','))}&priority=${priority}`),
  backups: () => request<Array<{ driverId: string; timestamp: string | null; hasData: boolean }>>('/backups'),
  backupAll: () => request<{ successful: number; failed: number; results: Record<string, boolean> }>('/backups', { method: 'POST' }),
  backup: (id: string) => request<{ driverId: string; content: unknown }>(`/backups/${encodeURIComponent(id)}`),
  deleteBackup: (id: string) => request<{ deleted: boolean }>(`/backups/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  importBackups: async (file: File) => {
    const body = new FormData()
    body.set('file', file)
    const response = await fetch('/api/v1/backups/import', { method: 'POST', body, credentials: 'same-origin' })
    const payload = (await response.json().catch(() => ({}))) as Envelope<{ message: string; integrationCount: number; settingsRestored: boolean; restartRequired: boolean }> & ErrorEnvelope
    if (!response.ok) throw new ApiError(response.status, payload.error?.message ?? 'Backup import failed')
    return payload.data
  },
  setActiveRemote: (remoteId: string) => request<{ activeRemoteId: string }>('/remotes/active', { method: 'POST', body: JSON.stringify({ remoteId }) }),
}

export interface SystemMessage { id: string; date: string; title: string; content: string; priority: string }
export interface NotificationSettings { [key: string]: Record<string, unknown> }
export interface DiagnosticEntity { id?: string; entity_id?: string; localized_name?: string; integration?: { localized_name?: string }; [key: string]: unknown }
export interface DiagnosticActivityResult { activities: Record<string, { name: string; entities: DiagnosticEntity[] }> }
export interface IrCodeset { device_id?: string; id?: string; name?: string; device_name?: string; [key: string]: unknown }

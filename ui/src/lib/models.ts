export type ConnectionState = 'connected' | 'ok' | 'not_configured' | 'disconnected' | 'error' | 'unknown'
export type ManagementMode = 'custom' | 'official' | 'external' | 'self_managed'

export interface Integration {
  id: string
  instanceId: string | null
  source: 'installed' | 'catalog'
  name: string
  description: string
  version: string | null
  latestVersion: string | null
  developer: string | null
  developerHomepage: string | null
  supportLinks: Array<{ platform: string; url: string }>
  homepage: string | null
  categories: string[]
  repository: { stars: number; downloads: number; createdAt: string | null; updatedAt: string | null }
  originalIndex: number
  management: ManagementMode
  installState: 'available' | 'installed' | 'configured' | 'official' | 'external' | 'self_managed'
  connectionState: ConnectionState
  updateAvailable: boolean
  installed: boolean
  driverInstalled: boolean
  configuredEntities: number
  capabilities: {
    install: boolean
    update: boolean
    selfUpdate: boolean
    backup: boolean
    deleteConfiguration: boolean
    deleteDriver: boolean
    selectVersion: boolean
  }
}

export interface Bootstrap {
  activeRemoteId: string | null
  remotes: Array<{ id: string; name: string; address: string; active: boolean; online: boolean }>
  remoteConfiguratorUrl: string | null
}

export interface SettingsPayload {
  settings: {
    shutdown_on_battery: boolean
    auto_update: boolean
    backup_configs: boolean
    backup_time: string
    show_beta_releases: boolean
  }
  preferences: { sort_by: string; sort_reverse: boolean }
  runtime: { remoteAddress: string | null; webServerPort: number; external: boolean }
}

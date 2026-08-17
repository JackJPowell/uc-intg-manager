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
    configure: boolean
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

export type IntegrationSetupField =
  | { id: string; label: string; type: 'text' | 'password'; value: string; regex?: string | null }
  | { id: string; label: string; type: 'textarea'; value: string }
  | { id: string; label: string; type: 'number'; value: string; min?: number | null; max?: number | null; step?: number | null; decimals?: number; unit?: string }
  | { id: string; label: string; type: 'checkbox'; value: boolean }
  | { id: string; label: string; type: 'dropdown'; value: string; items: Array<{ id: string; label: string }> }
  | { id: string; label: string; type: 'label'; text: string }
  | { id: string; label: string; type: 'unknown' }

export interface IntegrationSetupPage { title: string; fields: IntegrationSetupField[] }
export interface IntegrationSetupInfo {
  id: string
  state: 'SETUP' | 'WAIT_USER_ACTION' | 'OK' | 'ERROR'
  error: string
  action: null | { type: 'input'; page: IntegrationSetupPage | null } | { type: 'confirmation'; title: string; message1: string; message2: string; image: string | null }
}
export interface IntegrationSetupDefinition {
  driverId: string
  driverName: string
  setupDataSchema: IntegrationSetupPage | null
  activeSetup: IntegrationSetupInfo | null
}


export interface IntegrationSetupEntity {
  id: string
  type: string
  name: string
  description: string
  area: string
  deviceClass: string
  icon: string
  features: string[]
}

export interface IntegrationSetupEntities {
  integrationId: string
  availableEntities: IntegrationSetupEntity[]
  configuredEntities: IntegrationSetupEntity[]
}

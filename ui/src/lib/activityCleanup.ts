import type { QueryClient } from '@tanstack/react-query'

export type CleanupKind = 'orphaned' | 'unused'
export type ActivityCleanupSelection = {
  kind: CleanupKind
  remoteId: string
  activityId: string
  activityName: string
  entityId: string
  entityName: string
}

export function activityCleanupDescription(selection: ActivityCleanupSelection) {
  const context = `Remove ${selection.entityName} from ${selection.activityName}?`
  return selection.kind === 'orphaned'
    ? `${context} This also removes its activity steps, buttons and widgets, and clears any slider or voice-assistant target pointing to it. Other activities and configured Remote entities are kept.`
    : `${context} It will remain configured on the Remote. Removal is skipped if the entity is now referenced by a step or control.`
}

export function invalidateActivityScans(client: QueryClient, remoteId: string) {
  for (const key of ['unused', 'orphaned']) void client.invalidateQueries({ queryKey: ['remote-view', remoteId, key] })
  void client.invalidateQueries({ queryKey: ['diagnostics'] })
}

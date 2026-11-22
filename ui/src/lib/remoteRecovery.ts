import type { QueryClient, QueryKey } from '@tanstack/react-query'
import { api, ApiError } from './api'
import type { Integration } from './models'

/** Wake recovery only performs reads; bootstrap must establish the current target first. */
export async function refreshRemoteView(client: QueryClient, currentPage: () => number, isActive: () => boolean) {
  const read = <T>(queryKey: QueryKey, queryFn: () => Promise<T>) => client.fetchQuery({
    queryKey, queryFn, staleTime: 0,
    retry: (count, error) => isActive() && count < 4 && !(error instanceof ApiError && error.status < 500 && ![408, 429].includes(error.status)),
    retryDelay: attempt => Math.min(500 * 2 ** attempt, 4_000),
  })
  if (!isActive()) return
  const bootstrap = await read(['bootstrap'], api.bootstrap)
  if (!isActive()) return
  const page = currentPage()
  const remoteId = bootstrap.activeRemoteId
  const reads: Promise<unknown>[] = []
  if (remoteId) {
    reads.push(read(['remote-view', remoteId, 'status'], api.status))
    if (page === 0) reads.push(read(['remote-view', remoteId, 'integrations'], api.integrations))
    if (page === 1) {
      reads.push(read(['remote-view', remoteId, 'orphaned'], api.orphanedEntities))
      reads.push(read(['remote-view', remoteId, 'unused'], api.unusedActivityEntities))
    }
  }
  if (page === 2) reads.push(read(['remote-view', 'system-messages'], api.systemMessageSummary))
  // Let independent reads finish even if one fails; query observers show their errors.
  await Promise.allSettled(reads)
}

export function reconnectOutcome(item: Integration | undefined, checkedAt: number, submittedAt: number, deadline: number) {
  if (checkedAt <= submittedAt + 5_000) return null
  if (item && ['connected', 'ok'].includes(item.connectionState) && !item.updateAvailable) return 'connected'
  return checkedAt >= deadline ? 'unconfirmed' : null
}

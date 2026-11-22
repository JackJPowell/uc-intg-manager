import { createRootRouteWithContext, Outlet, useRouterState } from '@tanstack/react-router'
import type { QueryClient } from '@tanstack/react-query'
import { AppShell } from '../components/AppShell'

export const Route = createRootRouteWithContext<{ queryClient: QueryClient }>()({
  component: RootLayout,
})

function RootLayout() {
  const remote = useRouterState({ select: state => state.matches.some(match => match.routeId === '/remote') })
  return remote ? <Outlet /> : <AppShell><Outlet /></AppShell>
}

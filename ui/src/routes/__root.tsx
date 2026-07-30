import { createRootRouteWithContext, Outlet } from '@tanstack/react-router'
import type { QueryClient } from '@tanstack/react-query'
import { AppShell } from '../components/AppShell'

export const Route = createRootRouteWithContext<{ queryClient: QueryClient }>()({
  component: () => <AppShell><Outlet /></AppShell>,
})

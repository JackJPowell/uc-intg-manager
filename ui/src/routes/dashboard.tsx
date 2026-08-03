import { Navigate, createFileRoute } from '@tanstack/react-router'

export const Route = createFileRoute('/dashboard')({
  component: () => <Navigate to="/" replace />,
})

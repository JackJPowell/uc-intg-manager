import { createFileRoute } from '@tanstack/react-router'
import { NotificationsPage } from '../components/NotificationsPage'
export const Route = createFileRoute('/notifications')({ component: NotificationsPage })

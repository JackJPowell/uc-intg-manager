import { createFileRoute } from '@tanstack/react-router'
import { SystemMessagesPage } from '../components/SystemMessagesPage'
export const Route = createFileRoute('/system-messages')({ component: SystemMessagesPage })

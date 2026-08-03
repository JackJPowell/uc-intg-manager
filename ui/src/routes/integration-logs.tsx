import { createFileRoute } from '@tanstack/react-router'
import { IntegrationLogsPage } from '../components/IntegrationLogsPage'
export const Route = createFileRoute('/integration-logs')({ component: IntegrationLogsPage })

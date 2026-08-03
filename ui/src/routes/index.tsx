import { createFileRoute } from '@tanstack/react-router'
import { IntegrationCollection } from '../components/IntegrationCollection'

export const Route = createFileRoute('/')({
  component: () => <IntegrationCollection mode="installed" />,
})

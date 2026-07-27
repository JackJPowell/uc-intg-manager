import { createFileRoute } from '@tanstack/react-router'
import { PlaceholderPage } from '../components/PlaceholderPage'
export const Route = createFileRoute('/port-moved')({ component: () => <PlaceholderPage title="The manager has moved" description="Use the current Integration Manager port to continue." /> })

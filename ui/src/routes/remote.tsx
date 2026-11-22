import { createFileRoute } from '@tanstack/react-router'
import { RemotePage } from '../components/RemotePage'

export const Route = createFileRoute('/remote')({ component: RemotePage })

import { createFileRoute } from '@tanstack/react-router'
import { DiagnosticsPage } from '../components/DiagnosticsPage'
export const Route = createFileRoute('/diagnostics')({ component: DiagnosticsPage })

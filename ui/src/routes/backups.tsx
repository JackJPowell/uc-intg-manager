import { createFileRoute } from '@tanstack/react-router'
import { BackupsPage } from '../components/BackupsPage'
export const Route=createFileRoute('/backups')({component:BackupsPage})

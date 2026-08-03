import { Construction } from 'lucide-react'

export function PlaceholderPage({ title, description }: { title: string; description: string }) {
  return <section className="page-section"><header className="page-heading"><div><p className="eyebrow">Integration workspace</p><h1>{title}</h1><p>{description}</p></div></header><div className="empty-state"><Construction /><h2>Use the manager workspace</h2><p>Open the current manager address to continue with its single-page workspace.</p></div></section>
}

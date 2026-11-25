import { Card } from '../components/ui'

export default function NotImplemented({ title }: { title: string }) {
  return (
    <div className="stack">
      <h2 style={{ margin: 0 }}>{title}</h2>
      <Card>
        <div className="empty">
          <p style={{ fontSize: 15, margin: '0 0 8px' }}>功能建设中</p>
          <p className="muted small" style={{ margin: 0 }}>
            该模块暂未开放。
          </p>
        </div>
      </Card>
    </div>
  )
}

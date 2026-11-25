import type { ReactNode } from 'react'

export function Card(props: {
  title?: string
  action?: ReactNode
  className?: string
  children: ReactNode
}) {
  return (
    <section className={'card' + (props.className ? ' ' + props.className : '')}>
      {props.title ? (
        <div className="card-head">
          <span>{props.title}</span>
          {props.action}
        </div>
      ) : null}
      <div className="card-body">{props.children}</div>
    </section>
  )
}

const STATUS: Record<string, { cls: string; label: string }> = {
  succeeded: { cls: 'success', label: '成功' },
  failed: { cls: 'failed', label: '失败' },
  partial: { cls: 'partial', label: '部分失败' },
  running: { cls: 'blue', label: '运行中' },
  interrupted: { cls: 'muted', label: '已中断' },
  waiting: { cls: 'muted', label: '等待中' },
  skipped: { cls: 'muted', label: '跳过' },
}

export function StatusBadge({ status }: { status: string }) {
  const meta = STATUS[status] ?? { cls: 'muted', label: status }
  return (
    <span className={'badge ' + meta.cls}>
      <span className="dot" />
      {meta.label}
    </span>
  )
}

export function Spinner() {
  return <div className="spinner" aria-label="加载中" />
}

export function ErrorBox({ message }: { message: string }) {
  return <div className="errorbox">{message}</div>
}

export function EmptyState({ text = '暂无数据' }: { text?: string }) {
  return <div className="empty">{text}</div>
}

export function StatCard({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return (
    <div className="card stat">
      <div className="k">{label}</div>
      <div className={'v ' + (tone ?? '')}>{value}</div>
    </div>
  )
}

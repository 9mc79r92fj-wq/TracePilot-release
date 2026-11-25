import { Handle, Position } from '@xyflow/react'
import type { NodeProps } from '@xyflow/react'

export interface TraceFlowNodeData {
  shortId: string
  stepType: string
  statusClass: string
  isFault: boolean
  isCurrent?: boolean
  toolName?: string
  retryAttempt?: number
  timeout?: boolean
  errorType?: string
}

export default function TraceFlowNode({ data }: NodeProps) {
  const d = data as unknown as TraceFlowNodeData
  const retried = typeof d.retryAttempt === 'number' && d.retryAttempt > 1
  return (
    <div
      className={'dagnode ' + d.statusClass + (d.isCurrent ? ' current' : '')}
      title={d.errorType ? `error_type: ${d.errorType}` : undefined}
    >
      <Handle type="target" position={Position.Left} />
      <div className="dn-type">{d.stepType}</div>
      <div className="dn-id mono">{d.shortId}</div>
      {d.toolName && <div className="dn-id mono">tool: {d.toolName}</div>}
      <div className="row" style={{ gap: 4, flexWrap: 'wrap', marginTop: 2 }}>
        {d.isFault && <span className="badge failed">fault</span>}
        {retried && <span className="badge warning">重试 #{d.retryAttempt}</span>}
        {d.timeout && <span className="badge failed">超时</span>}
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  )
}

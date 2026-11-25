import type { TraceStep } from '../types'

interface Props {
  step: TraceStep
  downstream: string[]
}

type SectionKey = 'confirmed' | 'observed' | 'potential' | 'unknown'

const SECTIONS: { key: SectionKey; label: string; cls: string }[] = [
  { key: 'confirmed', label: 'Confirmed · 已确认事实', cls: 'blue' },
  { key: 'observed', label: 'Observed · 已观察差异', cls: 'green' },
  { key: 'potential', label: 'Potential · 潜在影响', cls: 'warning' },
  { key: 'unknown', label: 'Unknown · 未验证推断', cls: 'muted' },
]

export default function EvidenceCard({ step, downstream }: Props) {
  const fi = step.metadata?.fault_injection as
    | { type?: unknown; injected?: unknown; message?: unknown }
    | undefined

  const items: Record<SectionKey, string[]> = {
    confirmed: fi
      ? [
          `故障类型：${String(fi.type ?? '未记录')}`,
          `注入标记：${fi.injected === true ? '已注入' : fi.injected === false ? '未注入' : '未记录'}`,
          `信息：${String(fi.message ?? '未记录')}`,
        ]
      : ['未发现注入故障标记'],
    observed: [
      `错误类型：${step.error?.error_type ?? '未记录'}`,
      `错误信息：${step.error?.error_message ?? '未记录'}`,
      `发生位置：${step.step_id}`,
      `执行状态：${step.status}`,
    ],
    potential: downstream.length
      ? [`下游依赖节点（${downstream.length} 个）：${downstream.join(', ')}`]
      : ['无下游依赖节点'],
    unknown: ['下游依赖是否实际受影响尚未通过反事实实验验证，不构成因果结论。'],
  }

  return (
    <div className="evidence">
      {SECTIONS.map((s) => (
        <div key={s.key} className={'evidence-sec ' + s.cls}>
          <div className="evidence-label">{s.label}</div>
          <ul>
            {items[s.key].map((it, i) => (
              <li key={i}>{it}</li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}

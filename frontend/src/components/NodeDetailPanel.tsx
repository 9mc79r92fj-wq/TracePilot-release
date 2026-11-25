import { useState } from 'react'
import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { Check, Copy } from 'lucide-react'
import type { TraceStep } from '../types'
import { StatusBadge } from './ui'
import EvidenceCard from './EvidenceCard'

// 敏感字段脱敏（与后端一致，避免误伤 token_usage 等正常字段）
const SENSITIVE =
  /api[_-]?key|authorization|password|passwd|secret|access[_-]?token|refresh[_-]?token|private[_-]?key|bearer/i

export function redactForDisplay(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redactForDisplay)
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      out[k] = SENSITIVE.test(k) ? '[REDACTED]' : redactForDisplay(v)
    }
    return out
  }
  return value
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="field">
      <div className="k">{label}</div>
      <div className="v">{children}</div>
    </div>
  )
}

function NotRecorded() {
  return <span className="muted small">未记录</span>
}

function JsonValue({ label, value }: { label: string; value: unknown }) {
  const [open, setOpen] = useState(false)
  if (value === undefined || value === null) {
    return (
      <Field label={label}>
        <NotRecorded />
      </Field>
    )
  }
  const text = JSON.stringify(redactForDisplay(value), null, 2)
  const long = text.length > 260
  return (
    <div className="field">
      <div className="k">{label}</div>
      <pre className={'json' + (long && !open ? ' collapsed' : '')}>{text}</pre>
      {long && (
        <button className="btn" onClick={() => setOpen((o) => !o)}>
          {open ? '收起' : '展开'}
        </button>
      )}
    </div>
  )
}

interface Props {
  step: TraceStep
  childStepIds: string[]
  isFault: boolean
  downstream: string[]
  /** 当前 Trace ID：用于把「失败节点」上下文带到归因/反事实/修复页面 */
  traceId?: string
}

export default function NodeDetailPanel({ step, childStepIds, isFault, downstream, traceId }: Props) {
  const navigate = useNavigate()
  const [copied, setCopied] = useState(false)
  const [raw, setRaw] = useState(false)

  const meta = (step.metadata ?? {}) as Record<string, unknown>
  const attempt = Number(meta.attempt ?? meta.retry_index)
  const retried = Number.isFinite(attempt) && attempt > 1
  const timeout = /timeout/i.test(String(step.error?.error_type ?? meta.error_type ?? ''))

  function copyId() {
    if (navigator.clipboard) {
      navigator.clipboard
        .writeText(step.step_id)
        .then(() => {
          setCopied(true)
          setTimeout(() => setCopied(false), 1500)
        })
        .catch(() => {})
    }
  }

  function openPage(path: string) {
    if (!traceId) return
    navigate(
      `${path}?trace_id=${encodeURIComponent(traceId)}&step_id=${encodeURIComponent(step.step_id)}`,
    )
  }

  return (
    <div className="stack">
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div className="row" style={{ gap: 8 }}>
          <span className="badge blue">{step.step_type}</span>
          <StatusBadge status={step.status} />
        </div>
        <div className="row" style={{ gap: 6 }}>
          <button className="btn" onClick={copyId} title="复制 Step ID">
            {copied ? <Check size={14} /> : <Copy size={14} />}
            {copied ? '已复制' : '复制ID'}
          </button>
          <button className="btn" onClick={() => setRaw((r) => !r)}>
            {raw ? '收起JSON' : '原始JSON'}
          </button>
        </div>
      </div>

      <div className="detail-grid">
        <Field label="Step ID">
          <span className="mono small">{step.step_id}</span>
        </Field>
        <Field label="Step Type">
          <span>{step.step_type}</span>
        </Field>
        <Field label="Status">
          <StatusBadge status={step.status} />
        </Field>
        <Field label="Start Time">
          <span className="small">{new Date(step.timestamp).toLocaleString()}</span>
        </Field>
        <Field label="End Time">
          <NotRecorded />
        </Field>
        <Field label="Duration">
          {step.duration_ms != null ? `${step.duration_ms} ms` : <NotRecorded />}
        </Field>
      </div>

      <Field label="Token Usage">
        {step.token_usage ? (
          <span className="mono small">{JSON.stringify(step.token_usage)}</span>
        ) : (
          <NotRecorded />
        )}
      </Field>

      <Field label="Parent Steps">
        {step.parent_step_ids.length ? (
          <span className="mono small">{step.parent_step_ids.join(', ')}</span>
        ) : (
          <span className="muted small">（根节点）</span>
        )}
      </Field>
      <Field label="Child Steps">
        {childStepIds.length ? (
          <span className="mono small">{childStepIds.join(', ')}</span>
        ) : (
          <span className="muted small">（叶节点）</span>
        )}
      </Field>

      <JsonValue label="Input" value={step.input} />
      <JsonValue label="Output" value={step.output} />
      {step.tool_name != null && (
        <Field label="Tool">
          <span>{step.tool_name}</span>
        </Field>
      )}
      {step.tool_arguments != null && <JsonValue label="Tool Arguments" value={step.tool_arguments} />}
      {step.tool_result != null && <JsonValue label="Tool Result" value={step.tool_result} />}

      <Field label="Error">
        {step.error ? (
          <div className="errorbox">
            {step.error.error_type}: {step.error.error_message}
          </div>
        ) : (
          <NotRecorded />
        )}
      </Field>

      {timeout && (
        <Field label="超时信息">
          <span className="small">
            超时原因：{step.error?.error_message ?? String(meta.timeout_seconds ?? '未记录超时秒数')}
            {meta.timeout_seconds != null && `（timeout_seconds=${String(meta.timeout_seconds)}）`}
          </span>
        </Field>
      )}

      {(retried || meta.retry_of != null) && (
        <Field label="重试信息">
          <span className="small">
            第 {Number.isFinite(attempt) && attempt > 0 ? attempt : '?'} 次执行
            {meta.retry_of != null && ` · 上一次: ${String(meta.retry_of)}`}
            {meta.max_retries != null && ` · 最大重试: ${String(meta.max_retries)}`}
          </span>
        </Field>
      )}

      <JsonValue label="Step Metadata" value={step.metadata} />

      {traceId && (
        <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
          <button className="btn" onClick={() => openPage('/attribution')}>
            用该节点做故障归因
          </button>
          <button className="btn" onClick={() => openPage('/counterfactual')}>
            反事实实验
          </button>
          <button className="btn" onClick={() => openPage('/repair')}>
            最小修复
          </button>
        </div>
      )}

      {raw && <pre className="json">{JSON.stringify(redactForDisplay(step), null, 2)}</pre>}

      {isFault && <EvidenceCard step={step} downstream={downstream} />}
    </div>
  )
}

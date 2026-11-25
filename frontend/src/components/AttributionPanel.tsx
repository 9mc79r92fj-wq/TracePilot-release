import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { GitCompare, Network, RefreshCw, ShieldQuestion, X } from 'lucide-react'
import { api, apiErrorMessage } from '../api'
import { ErrorBox, Spinner } from './ui'

export interface AttributionCandidate {
  cause_type: string
  evidence_level: string
  summary: string
  evidence: { kind: string; ref_id: string; detail: string }[]
  affected_steps: string[]
  confidence_basis: string
  uncertainty: string | null
  experiment_refs: {
    experiment_id: string
    control_conditions: unknown
    causal_evidence_level: string | null
    intervention: unknown
    replays: number | null
    note: string
  }[]
  is_conclusive: boolean
  related_observations?: string[]
}

export interface AttributionReport {
  engine_version: string
  attribution_id: string
  evaluation_run_id: string | null
  case_id: string
  trace_id: string | null
  failed_metrics: {
    metric: string
    label: string
    status: string
    metric_value: unknown
    method: string | null
    evidence: string[]
    limitations: string | null
  }[]
  unknown_metrics: { metric: string; label: string; status: string }[]
  observed_events: {
    event_id: string
    kind: string
    step_id: string
    fault_type?: string | null
    error_type?: string | null
    detail: string
    evidence_level: string
  }[]
  candidate_causes: AttributionCandidate[]
  evidence_refs: { kind: string; ref_id: string; detail: string }[]
  evidence_levels: Record<string, number>
  affected_steps: string[]
  experiment_refs: AttributionCandidate['experiment_refs']
  root_cause_status: string
  ambiguity_notes: string[]
  limitations: string[]
  generated_at: string
  notes: string[]
}

export const LEVEL_BADGE: Record<string, string> = {
  CONFIRMED_EVENT: 'badge success',
  OBSERVED_DIFFERENCE: 'badge blue',
  POTENTIAL_CAUSE: 'badge warning',
  INTERVENTION_SUPPORTED: 'badge cyan',
  UNKNOWN: 'badge muted',
}

export const LEVEL_LABEL: Record<string, string> = {
  CONFIRMED_EVENT: '直接证据',
  OBSERVED_DIFFERENCE: '观察差异',
  POTENTIAL_CAUSE: '依赖推断',
  INTERVENTION_SUPPORTED: '干预证据',
  UNKNOWN: '未知',
}

export const TYPE_BADGE: Record<string, string> = {
  TOOL_TIMEOUT: 'badge failed',
  TOOL_EXCEPTION: 'badge failed',
  INVALID_ARGUMENTS: 'badge partial',
  INVALID_TOOL_RESULT: 'badge partial',
  EMPTY_RETRIEVAL: 'badge warning',
  PERMISSION_DENIED: 'badge failed',
  TASK_INCOMPLETE: 'badge warning',
  OUTPUT_MISMATCH: 'badge partial',
  PROCESS_VIOLATION: 'badge blue',
  UNKNOWN: 'badge muted',
}

/** 归因证据图：按证据关系分层（直接证据 / 观察差异 / 依赖关系 / 干预证据 / 未知）。 */
export function AttributionEvidenceGraph({ report }: { report: AttributionReport }) {
  const relations = [
    { key: 'CONFIRMED_EVENT', label: '直接证据' },
    { key: 'OBSERVED_DIFFERENCE', label: '观察差异' },
    { key: 'POTENTIAL_CAUSE', label: '依赖推断' },
    { key: 'INTERVENTION_SUPPORTED', label: '干预证据' },
    { key: 'UNKNOWN', label: '未知' },
  ]
  return (
    <div className="stack">
      {relations.map((rel) => {
        const cands = report.candidate_causes.filter((c) => c.evidence_level === rel.key)
        if (cands.length === 0) return null
        return (
          <div key={rel.key} className="lin-layer">
            <div className="lin-layer-head">
              <span className={LEVEL_BADGE[rel.key]} style={{ marginRight: 6 }}>{rel.label}</span>
              <span className="muted">×{cands.length}</span>
            </div>
            <div className="stack">
              {cands.map((c, i) => (
                <div key={c.cause_type + i} className="attr-node">
                  <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
                    <span className={TYPE_BADGE[c.cause_type] ?? 'badge muted'}>{c.cause_type}</span>
                    {c.is_conclusive ? (
                      <span className="badge success">可作为结论</span>
                    ) : (
                      <span className="badge warning">仅为候选</span>
                    )}
                    {c.affected_steps.length > 0 && (
                      <span className="muted small">影响步骤 {c.affected_steps.length}</span>
                    )}
                  </div>
                  <div className="small" style={{ marginTop: 4 }}>{c.summary}</div>
                  <div className="small muted">依据：{c.confidence_basis}</div>
                  {c.uncertainty && <div className="small muted">不确定性：{c.uncertainty}</div>}
                  {c.evidence.length > 0 && (
                    <details style={{ marginTop: 4 }}>
                      <summary className="small muted" style={{ cursor: 'pointer' }}>
                        证据引用（{c.evidence.length}）
                      </summary>
                      <ul style={{ margin: '4px 0 0 16px' }}>
                        {c.evidence.map((e, j) => (
                          <li key={j} className="small">
                            <span className="badge muted">{e.kind}</span>{' '}
                            <span className="mono small">{e.ref_id}</span>
                            <div className="muted">{e.detail}</div>
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                  {c.experiment_refs.length > 0 && (
                    <div className="cf-note small" style={{ marginTop: 4 }}>
                      <strong>关联实验</strong>
                      {c.experiment_refs.map((r) => (
                        <div key={r.experiment_id} className="mono small">
                          {r.experiment_id} · 因果强度 {r.causal_evidence_level ?? '—'} · replays {r.replays ?? '—'}
                          <div className="muted">控制条件：{JSON.stringify(r.control_conditions)}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}

/** 归因报告面板（可嵌入抽屉或页面）。 */
export function AttributionPanel({ report }: { report: AttributionReport }) {
  const navigate = useNavigate()
  return (
    <div className="stack small">
      <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
        <span className="mono small">{report.attribution_id}</span>
        <span className={
          report.root_cause_status === 'MULTI_ANOMALY' ? 'badge warning'
            : report.root_cause_status === 'UNKNOWN' ? 'badge muted' : 'badge success'
        }>
          {report.root_cause_status}
        </span>
        <span className="muted">案例 {report.case_id}</span>
      </div>

      <div>
        <strong>失败指标（{report.failed_metrics.length}）</strong>
        {report.failed_metrics.length === 0 ? (
          <div className="muted">无失败指标</div>
        ) : (
          <table className="tbl" style={{ marginTop: 4 }}>
            <thead><tr><th>指标</th><th>状态</th><th>证据</th></tr></thead>
            <tbody>
              {report.failed_metrics.map((m) => (
                <tr key={m.metric}>
                  <td className="small">{m.label} <span className="mono muted">({m.metric})</span></td>
                  <td><span className="badge failed">{m.status}</span></td>
                  <td className="small muted">{m.evidence.join('; ').slice(0, 120)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {report.unknown_metrics.length > 0 && (
          <div className="muted small" style={{ marginTop: 4 }}>
            UNKNOWN 指标（不计入通过/失败）：{report.unknown_metrics.map((m) => m.metric).join(', ')}
          </div>
        )}
      </div>

      <div>
        <strong>异常事件（{report.observed_events.length}）</strong>
        <table className="tbl" style={{ marginTop: 4 }}>
          <thead><tr><th>类型</th><th>步骤</th><th>说明</th><th>证据等级</th></tr></thead>
          <tbody>
            {report.observed_events.map((e) => (
              <tr key={e.event_id}>
                <td className="small">{e.kind}</td>
                <td className="mono small">{e.step_id}</td>
                <td className="small muted">{e.detail}</td>
                <td><span className={LEVEL_BADGE[e.evidence_level] ?? 'badge muted'}>{LEVEL_LABEL[e.evidence_level]}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div>
        <strong className="row" style={{ gap: 6 }}><Network size={13} /> 归因证据图</strong>
        <AttributionEvidenceGraph report={report} />
      </div>

      {report.ambiguity_notes.length > 0 && (
        <div className="cf-note">
          <strong>歧义与不确定性</strong>
          <ul style={{ margin: '4px 0 0 16px' }}>
            {report.ambiguity_notes.map((n) => <li key={n}>{n}</li>)}
          </ul>
        </div>
      )}

      <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
        {report.trace_id && (
          <>
            <button className="btn" onClick={() => navigate('/traces/' + report.trace_id)}>
              查看 Trace
            </button>
            <button className="btn" onClick={() => navigate('/dag/' + report.trace_id)}>
              查看 DAG
            </button>
          </>
        )}
        {report.evaluation_run_id && (
          <span className="muted small">评测运行 {report.evaluation_run_id}</span>
        )}
      </div>

      <details>
        <summary className="small muted" style={{ cursor: 'pointer' }}>
          限制说明（{report.limitations.length}）
        </summary>
        <ul style={{ margin: '4px 0 0 16px' }}>
          {report.limitations.map((l) => <li key={l} className="small">{l}</li>)}
        </ul>
      </details>
    </div>
  )
}

/** 抽屉形态：案例归因 */
export function AttributionDrawer({
  runId,
  caseId,
  onClose,
}: {
  runId: string
  caseId: string
  onClose: () => void
}) {
  const [report, setReport] = useState<AttributionReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await api.post<{ report: AttributionReport }>(
        `/api/attribution/evaluations/${encodeURIComponent(runId)}/cases/${encodeURIComponent(caseId)}`,
      )
      setReport(r.data.report)
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setLoading(false)
    }
  }, [runId, caseId])

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, caseId])

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <span className="row" style={{ gap: 6 }}>
            <ShieldQuestion size={15} />
            <span className="small">自动归因 · {caseId}</span>
          </span>
          <div className="row" style={{ gap: 6 }}>
            <button className="btn" style={{ padding: '2px 8px' }} onClick={() => void load()}>
              <RefreshCw size={13} /> 重跑
            </button>
            <button className="btn" onClick={onClose} aria-label="关闭"><X size={15} /></button>
          </div>
        </div>
        <div className="drawer-body">
          {loading && <Spinner />}
          {error && <ErrorBox message={error} />}
          {!loading && !error && report && <AttributionPanel report={report} />}
        </div>
      </div>
    </div>
  )
}

/** 批量归因结果（评测运行维度）。 */
export function BatchAttributionPanel({
  runId,
  onOpenCase,
}: {
  runId: string
  onOpenCase: (caseId: string) => void
}) {
  const [batch, setBatch] = useState<{
    attributed_count: number
    bad_case_count: number
    reports: AttributionReport[]
    summary: {
      evidence_level_counts: Record<string, number>
      cause_type_counts: Record<string, number>
      root_cause_status_counts: Record<string, number>
    }
  } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      // 批量归因是 CPU 密集任务，评测刚结束时也可能排队；默认 15s 在并发负载下
      // 会误报「请求超时」，这里显式放宽到 120s（服务端仍有案例数上限）。
      const r = await api.post<typeof batch>(
        '/api/attribution/runs',
        {
          evaluation_run_id: runId,
          max_cases: 10,
        },
        { timeout: 120000 },
      )
      setBatch(r.data)
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setLoading(false)
    }
  }, [runId])

  return (
    <div className="stack small">
      <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
        <span className="row" style={{ gap: 6 }}>
          <GitCompare size={14} />
          <strong>自动归因（批量）</strong>
        </span>
        <button className="btn primary" onClick={() => void run()} disabled={loading}>
          {loading ? '归因中…' : '运行归因'}
        </button>
      </div>
      {error && <ErrorBox message={error} />}
      {batch && (
        <>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            <span className="badge muted">Bad Case {batch.bad_case_count}</span>
            <span className="badge blue">已归因 {batch.attributed_count}</span>
            {Object.entries(batch.summary.evidence_level_counts).map(([k, v]) => (
              <span key={k} className={LEVEL_BADGE[k] ?? 'badge muted'}>
                {LEVEL_LABEL[k] ?? k} × {v}
              </span>
            ))}
          </div>
          <table className="tbl">
            <thead><tr><th>案例</th><th>根因状态</th><th>异常类型</th><th></th></tr></thead>
            <tbody>
              {batch.reports.map((r) => (
                <tr key={r.attribution_id}>
                  <td className="mono small">{r.case_id}</td>
                  <td>
                    <span className={
                      r.root_cause_status === 'MULTI_ANOMALY' ? 'badge warning'
                        : r.root_cause_status === 'UNKNOWN' ? 'badge muted' : 'badge success'
                    }>{r.root_cause_status}</span>
                  </td>
                  <td className="small">
                    {Array.from(new Set(r.candidate_causes.map((c) => c.cause_type))).map((t) => (
                      <span key={t} className={TYPE_BADGE[t] ?? 'badge muted'} style={{ marginRight: 4 }}>{t}</span>
                    ))}
                  </td>
                  <td>
                    <button className="btn" style={{ padding: '2px 8px' }} onClick={() => onOpenCase(r.case_id)}>
                      查看
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {batch.attributed_count === 0 && (
            <div className="muted">本次运行没有 Bad Case，未生成归因报告</div>
          )}
        </>
      )}
    </div>
  )
}

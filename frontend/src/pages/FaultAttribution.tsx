import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { RefreshCw, Share2 } from 'lucide-react'
import { apiErrorMessage, fetchPropagation, fetchTraces } from '../api'
import { Card, EmptyState, ErrorBox, Spinner } from '../components/ui'
import type { TraceSummary } from '../types'
import { useTraceUrlParam } from '../hooks/useTraceUrlParam'

type Classification =
  | 'confirmed_fault'
  | 'observed_difference'
  | 'potentially_affected'
  | 'unaffected'
  | 'unknown'

interface PNode {
  id: string
  step_type: string
  classification: Classification
  is_fault: boolean
  fault_type: string | null
  observed_difference: boolean
}

interface PEdge { source: string; target: string }
interface PPath { fault_node: string; terminal: string; path: string[] }

interface Propagation {
  nodes: PNode[]
  edges: PEdge[]
  paths: PPath[]
  summary: {
    total_nodes: number
    confirmed_fault: string[]
    observed_difference: string[]
    potentially_affected: string[]
    unaffected: string[]
    unknown: string[]
  }
  note: string
}

const CLASS_META: Record<Classification, { label: string; badge: string; desc: string }> = {
  confirmed_fault: {
    label: '注入故障',
    badge: 'badge failed',
    desc: '该步骤被明确注入故障',
  },
  observed_difference: {
    label: '观测到差异',
    badge: 'badge partial',
    desc: '与基线对比后观察到的实际差异',
  },
  potentially_affected: {
    label: '潜在受影响',
    badge: 'badge warning',
    desc: 'DAG 可达但无观测证据（推断，非因果）',
  },
  unaffected: {
    label: '未受影响',
    badge: 'badge success',
    desc: '无差异且不可从故障点到达',
  },
  unknown: {
    label: '无法判定',
    badge: 'badge muted',
    desc: '缺少基线或证据不足',
  },
}

export default function FaultAttribution() {
  const navigate = useNavigate()
  const { traceId: urlTraceId, stepId: urlStepId, setTraceId: setUrlTraceId } = useTraceUrlParam()
  const [traces, setTraces] = useState<TraceSummary[]>([])
  const [traceId, setTraceId] = useState('')
  const [baselineId, setBaselineId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [report, setReport] = useState<Propagation | null>(null)
  const [filter, setFilter] = useState<'all' | Classification>('all')
  const autoRanRef = useRef('')

  const load = useCallback(async () => {
    setError(null)
    try {
      const t = await fetchTraces()
      setTraces(t.traces)
      const urlHit = urlTraceId ? t.traces.find((x) => x.trace_id === urlTraceId) : undefined
      if (urlTraceId && !urlHit) {
        setError(
          `URL 指定的 Trace 不存在或不在当前 Trace 目录：${urlTraceId}。` +
            '请从 Trace Explorer 重新选择。',
        )
      }
      const faulted = urlHit ?? t.traces.find((x) => x.fault_types.length > 0)
      const normal = t.traces.find((x) => x.fault_types.length === 0)
      // 仅当用户尚未选择时设置默认值（异步响应不得覆盖用户选择；URL 优先）
      if (urlHit) setTraceId(urlHit.trace_id)
      else if (faulted) setTraceId((prev) => prev || faulted.trace_id)
      if (normal) setBaselineId((prev) => prev || normal.trace_id)
    } catch (e) {
      setError(apiErrorMessage(e))
    }
  }, [urlTraceId])

  useEffect(() => {
    void load()
  }, [load])

  const run = useCallback(
    async (id: string, baseline?: string) => {
      if (!id) return
      setBusy(true)
      setError(null)
      setReport(null)
      try {
        const r = await fetchPropagation(id, baseline || undefined)
        setReport(r.propagation as unknown as Propagation)
      } catch (e) {
        setError(apiErrorMessage(e))
      } finally {
        setBusy(false)
      }
    },
    [],
  )

  const selectedTrace = traces.find((t) => t.trace_id === traceId)
  const selectedHasFault = Boolean(selectedTrace?.fault_types.length)

  useEffect(() => {
    // URL 指定故障 Trace 时自动运行一次传播分析（形成 DAG 失败节点 → 归因的闭环）；
    // 直接访问/手动选择时保留原有「点击运行」行为。
    if (!urlTraceId || traceId !== urlTraceId || !selectedHasFault) return
    if (autoRanRef.current === urlTraceId) return
    autoRanRef.current = urlTraceId
    void run(traceId, baselineId || undefined)
  }, [urlTraceId, traceId, selectedHasFault, baselineId, run])

  const nodes = useMemo(() => {
    if (!report) return []
    if (filter === 'all') return report.nodes
    return report.nodes.filter((n) => n.classification === filter)
  }, [report, filter])

  const counts = useMemo(() => {
    if (!report) return null
    const s = report.summary
    return {
      confirmed: s.confirmed_fault.length,
      observed: s.observed_difference.length,
      potential: s.potentially_affected.length,
      unaffected: s.unaffected.length,
      unknown: s.unknown.length,
    }
  }, [report])

  function optionLabel(t: TraceSummary): string {
    const fault = t.fault_types.length ? ` · 故障: ${t.fault_types.join('/')}` : ' · 无故障'
    return `${t.trace_id}${fault}`
  }

  return (
    <div className="stack">
      <div className="row">
        <h2 style={{ margin: 0 }}>Fault Attribution</h2>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => void load()}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>
      <p className="muted small" style={{ margin: 0 }}>
        基于 DAG 可达性与基线对比归因故障传播范围。
        <strong> 可达不等同因果</strong>：potentially_affected 为推断范围，observed_difference 为观测差异。
      </p>

      {error && <ErrorBox message={error} />}

      <Card title="归因配置">
        <div className="stack">
          <div className="row" style={{ flexWrap: 'wrap', gap: 10 }}>
            <label className="small muted" htmlFor="at">故障 Trace</label>
            <select
              id="at"
              style={{ minWidth: 0, flex: '1 1 320px' }}
              value={traceId}
              onChange={(e) => {
                setTraceId(e.target.value)
                setUrlTraceId(e.target.value)
              }}
            >
              {traces.map((t) => (
                <option key={t.trace_id} value={t.trace_id}>{optionLabel(t)}</option>
              ))}
            </select>
          </div>
          <div className="row" style={{ flexWrap: 'wrap', gap: 10 }}>
            <label className="small muted" htmlFor="bl">基线 Trace（可选，同任务无故障者）</label>
            <select
              id="bl"
              style={{ minWidth: 0, flex: '1 1 320px' }}
              value={baselineId}
              onChange={(e) => setBaselineId(e.target.value)}
            >
              <option value="">（不提供基线）</option>
              {traces.map((t) => (
                <option key={t.trace_id} value={t.trace_id}>{optionLabel(t)}</option>
              ))}
            </select>
          </div>
          <div className="row">
            <button className="btn primary" onClick={() => void run(traceId, baselineId)} disabled={busy || !traceId}>
              <Share2 size={15} /> 运行传播分析
            </button>
            {busy && <span className="muted small">分析中…</span>}
          </div>
          <div className="cf-note small">
            提供基线后「观测到差异」才有对照；不提供时多数节点为 unknown。
          </div>
          {selectedTrace && !selectedHasFault && (
            <div className="errorbox" style={{ background: 'var(--warning-soft)', color: 'var(--warning)' }}>
              该 Trace 无注入故障，归因结果多为 unaffected / unknown。
              请选择带故障的 Trace。
            </div>
          )}
          {urlStepId && (
            <div className="cf-note small">
              来自 DAG 的失败节点上下文：<span className="mono">{urlStepId}</span>
              （已在归因表中高亮）
            </div>
          )}
        </div>
      </Card>

      {busy && <Spinner />}
      {!busy && !report && !error && <EmptyState text="选择故障 Trace 后点击「运行传播分析」" />}

      {report && counts && (
        <>
          <div className="grid stats">
            <div className="card stat">
              <div className="k">注入故障</div>
              <div className="v red">{counts.confirmed}</div>
              <div className="muted small">明确被注入故障的步骤</div>
            </div>
            <div className="card stat">
              <div className="k">观测到差异</div>
              <div className="v">{counts.observed}</div>
              <div className="muted small">与基线相比有实际变化</div>
            </div>
            <div className="card stat">
              <div className="k">潜在受影响</div>
              <div className="v yellow">{counts.potential}</div>
              <div className="muted small">推断（可达，非因果）</div>
            </div>
            <div className="card stat">
              <div className="k">无法判定 / 未受影响</div>
              <div className="v">{counts.unknown} / {counts.unaffected}</div>
              <div className="muted small">共 {report.summary.total_nodes} 步</div>
            </div>
          </div>

          {report.summary.confirmed_fault.length > 0 && (
            <Card title="故障节点">
              <div className="stack small">
                {report.nodes
                  .filter((n) => n.is_fault)
                  .map((n) => (
                    <div key={n.id} className="ev-box warn">
                      <div className="ev-title mono small">{n.id}</div>
                      <div className="small">
                        步骤类型 <span className="mono">{n.step_type}</span>
                        {n.fault_type && (
                          <>
                            {' '}· 故障类型 <span className="badge failed">{n.fault_type}</span>
                          </>
                        )}
                      </div>
                    </div>
                  ))}
              </div>
            </Card>
          )}

          {report.paths.length > 0 && (
            <Card title={'传播路径（' + report.paths.length + '）'}>
              <div className="stack">
                {report.paths.map((p, i) => (
                  <div key={i} className="cf-track">
                    {p.path.map((stepId, idx) => (
                      <span key={stepId} className="cf-track-col">
                        <span className="mono small">{stepId}</span>
                        {idx < p.path.length - 1 && <span className="cf-track-arrow">→</span>}
                      </span>
                    ))}
                  </div>
                ))}
              </div>
            </Card>
          )}

          <Card
            title={'节点归因分类（' + report.nodes.length + '）'}
            action={
              <select value={filter} onChange={(e) => setFilter(e.target.value as typeof filter)}>
                <option value="all">全部</option>
                {(Object.keys(CLASS_META) as Classification[]).map((k) => (
                  <option key={k} value={k}>{CLASS_META[k].label}</option>
                ))}
              </select>
            }
          >
            {nodes.length === 0 ? (
              <EmptyState text="该分类下无节点" />
            ) : (
              <table className="tbl">
                <thead>
                  <tr><th>步骤</th><th>类型</th><th>分类</th><th>故障类型</th><th>观测差异</th></tr>
                </thead>
                <tbody>
                  {nodes.map((n) => (
                    <tr
                      key={n.id}
                      className={n.id === urlStepId ? 'hl-row' : undefined}
                      title={n.id === urlStepId ? '来自 DAG 的失败节点' : undefined}
                    >
                      <td className="mono small">{n.id}</td>
                      <td className="small">{n.step_type}</td>
                      <td>
                        <span className={CLASS_META[n.classification]?.badge ?? 'badge muted'}>
                          {CLASS_META[n.classification]?.label ?? n.classification}
                        </span>
                      </td>
                      <td className="small">{n.fault_type ?? '—'}</td>
                      <td className="small">{n.observed_difference ? '是' : '否'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <div className="muted small" style={{ marginTop: 8 }}>
              {(Object.keys(CLASS_META) as Classification[]).map((k) => (
                <div key={k}>
                  <span className={CLASS_META[k].badge}>{CLASS_META[k].label}</span> {CLASS_META[k].desc}
                </div>
              ))}
            </div>
          </Card>

          <Card title="结论说明">
            <div className="errorbox" style={{ background: 'var(--warning-soft)', color: 'var(--warning)' }}>
              {report.note}
            </div>
            <div className="row" style={{ marginTop: 8 }}>
              <button className="btn" onClick={() => navigate('/dag/' + traceId)}>查看该 Trace DAG</button>
              <button
                className="btn"
                onClick={() =>
                  navigate(
                    '/counterfactual?trace_id=' +
                      encodeURIComponent(traceId) +
                      (urlStepId ? '&step_id=' + encodeURIComponent(urlStepId) : ''),
                  )
                }
              >
                去验证因果
              </button>
            </div>
          </Card>
        </>
      )}
    </div>
  )
}

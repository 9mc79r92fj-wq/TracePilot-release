import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeftRight, Play, RefreshCw } from 'lucide-react'
import { apiErrorMessage, fetchCompare, fetchTraces } from '../api'
import { Card, EmptyState, ErrorBox, Spinner } from '../components/ui'
import type { CompareResponse, TraceSummary } from '../types'

type FieldDiff = { ref: unknown; tgt: unknown } | null

interface TraceSide {
  trace_id: string
  status: string
  step_count: number
  total_duration_ms: number
  token_usage: { sum: number; has_unknown: boolean }
  fault_types: string[]
  recovery_observed: boolean | null
}

interface Comparison {
  traces: { reference: TraceSide; target: TraceSide }
  differences: {
    final_status: FieldDiff
    step_count?: { ref: number; tgt: number }
    total_duration_ms?: { ref: number; tgt: number }
    token_usage?: { ref: TraceSide['token_usage']; tgt: TraceSide['token_usage'] }
    fault_types?: { ref: string[]; tgt: string[] }
    recovery_observed?: FieldDiff
  }
  step_matches: {
    match_key: string
    ref_step_id: string
    tgt_step_id: string
    differences: Record<string, FieldDiff> | null
  }[]
  unmatched: { reference_only: string[]; target_only: string[] }
  diff_nodes: {
    step_id: string
    ref_step_id: string
    match_key: string
    differences: Record<string, FieldDiff>
    upstream?: string[]
    downstream?: string[]
  }[]
  earliest_difference: { step_id: string; kind: string[]; note: string } | null
  observations: string[]
  inferred_impacts: string[]
  unverified: string[]
}

function fmt(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (Array.isArray(v)) return v.length ? v.join(', ') : '（空）'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

export default function TraceComparison() {
  const navigate = useNavigate()
  const [traces, setTraces] = useState<TraceSummary[]>([])
  const [reference, setReference] = useState('')
  const [target, setTarget] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [resp, setResp] = useState<CompareResponse | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      const t = await fetchTraces()
      setTraces(t.traces)
      // 默认：参考 = 最新无故障 Trace，目标 = 最新含故障 Trace
      // 仅在用户尚未选择时设置：异步响应（含 StrictMode 双请求）不得覆盖用户选择
      const faultFree = t.traces.find((x) => x.fault_types.length === 0)
      const faulted = t.traces.find((x) => x.fault_types.length > 0)
      if (faultFree) setReference((prev) => prev || faultFree.trace_id)
      if (faulted) setTarget((prev) => prev || faulted.trace_id)
    } catch (e) {
      setError(apiErrorMessage(e))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const cmp = (resp?.comparison as unknown as Comparison) ?? null

  const diffRows = useMemo(() => {
    if (!cmp) return []
    const d = cmp.differences
    const rows: { label: string; ref: string; tgt: string; changed: boolean }[] = []
    const push = (label: string, refV: unknown, tgtV: unknown) => {
      rows.push({
        label,
        ref: fmt(refV),
        tgt: fmt(tgtV),
        changed: fmt(refV) !== fmt(tgtV),
      })
    }
    push('最终状态', d.final_status?.ref, d.final_status?.tgt)
    push('步骤数', d.step_count?.ref, d.step_count?.tgt)
    push('总耗时(ms)', d.total_duration_ms?.ref, d.total_duration_ms?.tgt)
    push(
      'Token 合计',
      d.token_usage?.ref ? `${d.token_usage.ref.sum}${d.token_usage.ref.has_unknown ? '（含未知）' : ''}` : null,
      d.token_usage?.tgt ? `${d.token_usage.tgt.sum}${d.token_usage.tgt.has_unknown ? '（含未知）' : ''}` : null,
    )
    push('故障类型', d.fault_types?.ref, d.fault_types?.tgt)
    push('是否观测到恢复', d.recovery_observed?.ref, d.recovery_observed?.tgt)
    return rows
  }, [cmp])

  async function runCompare() {
    if (!reference || !target) return
    if (reference === target) {
      setError('参考 Trace 与目标 Trace 不能相同')
      return
    }
    setBusy(true)
    setError(null)
    setResp(null)
    try {
      setResp(await fetchCompare(reference, target))
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  function optionLabel(t: TraceSummary): string {
    const fault = t.fault_types.length ? ` · 故障: ${t.fault_types.join('/')}` : ' · 无故障'
    return `${t.trace_id}${fault} · ${t.step_count} 步`
  }

  return (
    <div className="stack">
      <div className="row">
        <h2 style={{ margin: 0 }}>Trace Comparison</h2>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => void load()}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>
      <p className="muted small" style={{ margin: 0 }}>
        对齐两份 Trace 的步骤，输出结构差异、最早差异点与影响推断。
        <strong> 差异是相关性观察，不等同因果</strong>。
      </p>

      {error && <ErrorBox message={error} />}

      <Card title="对比配置">
        <div className="stack">
          <div className="row" style={{ flexWrap: 'wrap', gap: 10 }}>
            <label className="small muted" htmlFor="ref">参考 Trace（通常为无故障基线）</label>
            <select
              id="ref"
              style={{ minWidth: 0, flex: '1 1 320px' }}
              value={reference}
              onChange={(e) => setReference(e.target.value)}
            >
              {traces.map((t) => (
                <option key={t.trace_id} value={t.trace_id}>{optionLabel(t)}</option>
              ))}
            </select>
          </div>
          <div className="row" style={{ flexWrap: 'wrap', gap: 10 }}>
            <label className="small muted" htmlFor="tgt">目标 Trace（通常为故障 Trace）</label>
            <select
              id="tgt"
              style={{ minWidth: 0, flex: '1 1 320px' }}
              value={target}
              onChange={(e) => setTarget(e.target.value)}
            >
              {traces.map((t) => (
                <option key={t.trace_id} value={t.trace_id}>{optionLabel(t)}</option>
              ))}
            </select>
          </div>
          <div className="row">
            <button className="btn primary" onClick={() => void runCompare()} disabled={busy || !reference || !target}>
              <ArrowLeftRight size={15} /> 运行对比
            </button>
            {busy && <span className="muted small">对比中…</span>}
          </div>
          <div className="muted small">共 {traces.length} 份 Trace 可选</div>
        </div>
      </Card>

      {busy && <Spinner />}
      {!busy && !resp && !error && <EmptyState text="选择两份 Trace 后点击「运行对比」" />}

      {cmp && (
        <>
          <div className="grid grid-2 tc-results">
          <Card title="Trace 概览对照">
            <div className="cmp-cols">
              {([
                ['参考（reference）', cmp.traces.reference],
                ['目标（target）', cmp.traces.target],
              ] as const).map(([label, side]) => (
                <div key={label} className="cmp-side">
                  <div className="cmp-side-head">{label}</div>
                  <div className="detail-grid">
                    <div><div className="k">trace_id</div><div className="mono small">{side.trace_id}</div></div>
                    <div><div className="k">状态</div><div className="small">{side.status}</div></div>
                    <div><div className="k">步骤数</div><div className="small">{side.step_count}</div></div>
                    <div><div className="k">总耗时</div><div className="small">{side.total_duration_ms} ms</div></div>
                    <div>
                      <div className="k">Token</div>
                      <div className="small">
                        {side.token_usage.sum}
                        {side.token_usage.has_unknown ? '（含未知）' : ''}
                      </div>
                    </div>
                    <div>
                      <div className="k">故障类型</div>
                      <div className="small">
                        {side.fault_types.length ? (
                          side.fault_types.map((f) => <span key={f} className="badge failed" style={{ marginRight: 4 }}>{f}</span>)
                        ) : (
                          <span className="badge success">无</span>
                        )}
                      </div>
                    </div>
                  </div>
                  <button className="btn" style={{ marginTop: 8 }} onClick={() => navigate('/dag/' + side.trace_id)}>
                    查看该 Trace DAG
                  </button>
                </div>
              ))}
            </div>
          </Card>

          <Card title="顶层差异">
            <table className="tbl">
              <thead>
                <tr><th>字段</th><th>参考</th><th>目标</th><th>是否不同</th></tr>
              </thead>
              <tbody>
                {diffRows.map((r) => (
                  <tr key={r.label}>
                    <td className="small">{r.label}</td>
                    <td className="mono small">{r.ref}</td>
                    <td className="mono small">{r.tgt}</td>
                    <td>{r.changed ? <span className="badge failed">不同</span> : <span className="badge muted">相同</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          {cmp.earliest_difference && (
            <Card title="最早差异点">
              <div className="cf-info">
                <div><span className="muted">步骤</span> <span className="mono small">{cmp.earliest_difference.step_id}</span></div>
                <div>
                  <span className="muted">差异字段</span>{' '}
                  {cmp.earliest_difference.kind.map((k) => <span key={k} className="badge muted" style={{ marginRight: 4 }}>{k}</span>)}
                </div>
                <div className="muted small">{cmp.earliest_difference.note}</div>
              </div>
            </Card>
          )}

          <Card title={'差异节点（' + cmp.diff_nodes.length + '）'}>
            {cmp.diff_nodes.length === 0 ? (
              <EmptyState text="两份 Trace 无结构差异" />
            ) : (
              <div className="stack">
                {cmp.diff_nodes.map((n) => (
                  <div key={n.step_id} className="ev-box warn">
                    <div className="ev-title mono small">{n.match_key} · {n.step_id}</div>
                    <div className="small muted">对应参考步骤：{n.ref_step_id}</div>
                    <table className="tbl" style={{ marginTop: 6 }}>
                      <thead><tr><th>字段</th><th>参考</th><th>目标</th></tr></thead>
                      <tbody>
                        {Object.entries(n.differences).map(([k, v]) => (
                          <tr key={k}>
                            <td className="small">{k}</td>
                            <td className="mono small">{fmt(v?.ref)}</td>
                            <td className="mono small">{fmt(v?.tgt)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {n.downstream && n.downstream.length > 0 && (
                      <div className="small muted">下游节点：{n.downstream.join(', ')}</div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card title="步骤匹配与未匹配">
            <div className="stack small">
              <div>
                <strong>步骤匹配（{cmp.step_matches.length}）</strong>
                <table className="tbl" style={{ marginTop: 4 }}>
                  <thead><tr><th>匹配键</th><th>参考步骤</th><th>目标步骤</th><th>差异</th></tr></thead>
                  <tbody>
                    {cmp.step_matches.map((m, i) => (
                      <tr key={i}>
                        <td className="mono small">{m.match_key}</td>
                        <td className="mono small">{m.ref_step_id}</td>
                        <td className="mono small">{m.tgt_step_id}</td>
                        <td>
                          {m.differences ? (
                            <span className="badge failed">{Object.keys(m.differences).join(', ')}</span>
                          ) : (
                            <span className="badge muted">无</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div>
                <strong>未匹配</strong>
                <div className="muted">仅参考有：{cmp.unmatched.reference_only.length} 个</div>
                <div className="mono small">{cmp.unmatched.reference_only.join(', ') || '—'}</div>
                <div className="muted">仅目标有：{cmp.unmatched.target_only.length} 个</div>
                <div className="mono small">{cmp.unmatched.target_only.join(', ') || '—'}</div>
              </div>
            </div>
          </Card>

          <Card title="观察 / 推断 / 未验证">
            <div className="stack small">
              <div>
                <strong className="badge blue">观察（observed）</strong>
                <ul style={{ margin: '4px 0 0 16px' }}>
                  {cmp.observations.length
                    ? cmp.observations.map((o) => <li key={o}>{o}</li>)
                    : <li className="muted">无</li>}
                </ul>
              </div>
              <div>
                <strong className="badge partial">推断（inferred，非因果）</strong>
                <ul style={{ margin: '4px 0 0 16px' }}>
                  {cmp.inferred_impacts.length
                    ? cmp.inferred_impacts.map((o) => <li key={o}>{o}</li>)
                    : <li className="muted">无</li>}
                </ul>
              </div>
              <div>
                <strong className="badge muted">未验证（unverified）</strong>
                <ul style={{ margin: '4px 0 0 16px' }}>
                  {cmp.unverified.length
                    ? cmp.unverified.map((o) => <li key={o}>{o}</li>)
                    : <li className="muted">无</li>}
                </ul>
              </div>
              <button className="btn" onClick={() => navigate('/counterfactual')}>
                <Play size={14} /> 前往反事实实验验证因果
              </button>
            </div>
          </Card>
          </div>
        </>
      )}
    </div>
  )
}

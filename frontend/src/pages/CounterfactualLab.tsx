import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { FlaskConical, RefreshCw } from 'lucide-react'
import { api, apiErrorMessage, fetchTraces } from '../api'
import { useTraceUrlParam } from '../hooks/useTraceUrlParam'
import type { TraceSummary } from '../types'
import { Card, EmptyState, ErrorBox, Spinner, StatusBadge } from '../components/ui'
import { LineagePanel } from '../components/LineagePanel'

interface ExpCreateResp {
  experiment_id: string
  status: string
  deduplicated: boolean
  source_trace_id: string
  links: Record<string, string>
}

interface ExpResults {
  experiment_id: string
  status: string
  error?: string | null
  observed_outcome?: {
    trace_id: string
    status: string
    fault_types: string[]
    executed: boolean
    note: string
  }
  intervention_outcome?: {
    trace_id: string
    run_id: string
    status: string
    fault_types: string[]
    attempts: number | null
    simulated: boolean
  }
  observed_difference?: {
    status_changed: boolean
    fault_types_removed: string[]
    fault_types_added: string[]
    comparison_summary: {
      diff_node_count: number
      unmatched_reference: string[]
      unmatched_target: string[]
    }
  }
  causal_evidence_level?: string
  causal_evidence_note?: string
  environment?: {
    model: { provider: string; model: string; simulated: boolean; paid_api_called: boolean }
    tools: string[]
    bounded: Record<string, number>
  }
  control_conditions?: string[]
  confounders?: string[]
  limitations?: string[]
  reproducible?: boolean
  replays?: number
  original_trace_modified?: boolean
  links?: Record<string, string>
}

type IntervType = 'remove_fault' | 'replace_tool_result' | 'adjust_timeout_retry'

// 证据强度分级：完全由真实实验元数据推导，禁止以"成功/失败"直接宣称因果
function evidenceStrength(r: ExpResults): { tag: string; cls: string; why: string } {
  const level = r.causal_evidence_level ?? 'none'
  const simulated = r.environment?.model?.simulated ?? true
  const repeated = (r.replays ?? 1) > 1 || r.reproducible === true
  const inconclusive = level === 'none'

  if (inconclusive) {
    return {
      tag: '无结论',
      cls: 'muted',
      why: '干预未观察到可区分差异，无法得出结论。',
    }
  }
  if (repeated && r.reproducible) {
    return {
      tag: '可复现',
      cls: 'success',
      why: '同一受控配置多次重放结果一致' + (simulated ? '（仍为模拟环境）' : ''),
    }
  }
  if (level === 'weak' && (r.control_conditions?.length ?? 0) > 0) {
    return {
      tag: '受控',
      cls: 'blue',
      why: '单变量受控干预，有限次重放' + (simulated ? '（模拟模型）' : ''),
    }
  }
  return {
    tag: '探索性',
    cls: 'warning',
    why: '探索性证据：干预已施加，但仍存在未控因素。',
  }
}

export default function CounterfactualLab() {
  const navigate = useNavigate()
  const { traceId: urlTraceId, stepId: urlStepId, setTraceId: setUrlTraceId } = useTraceUrlParam()
  const [traces, setTraces] = useState<TraceSummary[]>([])
  const [traceId, setTraceId] = useState('')
  const [eligibility, setEligibility] = useState<{
    eligible: boolean
    reasons: string[]
    requirements: string[]
  } | null>(null)
  const [intervType, setIntervType] = useState<IntervType>('remove_fault')
  const [replaceTool, setReplaceTool] = useState('calculator')
  const [replaceValue, setReplaceValue] = useState('{"expression":"1+1","result":2}')
  const [maxRetries, setMaxRetries] = useState(1)
  const [replays, setReplays] = useState(2)

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [experimentId, setExperimentId] = useState<string | null>(null)
  const [expStatus, setExpStatus] = useState<string | null>(null)
  const [results, setResults] = useState<ExpResults | null>(null)
  const [dedup, setDedup] = useState(false)

  const loadTraces = useCallback(async () => {
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
      if (urlHit) {
        setTraceId(urlHit.trace_id)
        return
      }
      // 默认选中一个「受控实验资格可用」的故障 Trace（最多探测 5 条，避免页面加载过慢）
      const faultCandidates = t.traces.filter((x) => x.fault_types.length > 0).slice(0, 5)
      let chosen = faultCandidates[0] ?? t.traces[0]
      for (const candidate of faultCandidates) {
        try {
          const r = await api.get<{
            eligibility: {
              capabilities: { counterfactual_experiment: { eligible: boolean } }
            }
          }>(
            `/api/governance/traces/${encodeURIComponent(candidate.trace_id)}/eligibility`,
            { timeout: 15000 },
          )
          if (r.data.eligibility.capabilities.counterfactual_experiment.eligible) {
            chosen = candidate
            break
          }
        } catch {
          // 资格接口异常时保持候选顺序，不阻断页面
        }
      }
      if (chosen) setTraceId(chosen.trace_id)
    } catch (e) {
      setError(apiErrorMessage(e))
    }
  }, [urlTraceId])

  useEffect(() => {
    void loadTraces()
  }, [loadTraces])

  // 读取受控实验资格（第 55-A 步治理接口）：不满足时给出具体原因，而非运行后才报错
  useEffect(() => {
    if (!traceId) {
      setEligibility(null)
      return
    }
    let cancelled = false
    void api
      .get<{ eligibility: { capabilities: { counterfactual_experiment: { eligible: boolean; reasons: string[]; requirements: string[] } } } }>(
        `/api/governance/traces/${encodeURIComponent(traceId)}/eligibility`,
        { timeout: 60000 },
      )
      .then((r) => {
        if (!cancelled) setEligibility(r.data.eligibility.capabilities.counterfactual_experiment)
      })
      .catch(() => {
        if (!cancelled) setEligibility(null)
      })
    return () => {
      cancelled = true
    }
  }, [traceId])

  function buildIntervention(): Record<string, unknown> {
    if (intervType === 'remove_fault') return { type: 'remove_fault' }
    if (intervType === 'replace_tool_result') {
      let parsed: unknown
      try {
        parsed = JSON.parse(replaceValue)
      } catch {
        throw new Error('替换结果必须是合法 JSON')
      }
      return { type: 'replace_tool_result', tool: replaceTool, value: parsed }
    }
    return { type: 'adjust_timeout_retry', max_retries: maxRetries }
  }

  async function runExperiment() {
    if (!traceId) return
    setBusy(true)
    setError(null)
    setResults(null)
    setExperimentId(null)
    setExpStatus(null)
    try {
      const intervention = buildIntervention()
      const create = await api.post<ExpCreateResp>('/api/counterfactual/experiments', {
        trace_id: traceId,
        intervention,
        replays,
      })
      setExperimentId(create.data.experiment_id)
      setExpStatus(create.data.status)
      setDedup(create.data.deduplicated)

      // 轮询实验状态直到完成（有界轮询，避免无限请求）
      const eid = create.data.experiment_id
      for (let i = 0; i < 100; i++) {
        const st = await api.get<{ status: string }>(`/api/counterfactual/experiments/${eid}`)
        setExpStatus(st.data.status)
        if (st.data.status !== 'running') break
        await new Promise((r) => setTimeout(r, 200))
      }
      const res = await api.get<ExpResults>(`/api/counterfactual/experiments/${eid}/results`)
      setResults(res.data)
      if (res.data.status === 'failed') setError(res.data.error ?? '实验执行失败')
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  const selected = traces.find((t) => t.trace_id === traceId)
  const strength = results?.status === 'succeeded' ? evidenceStrength(results) : null

  return (
    <div className="stack">
      <div className="row">
        <h2 style={{ margin: 0 }}>Counterfactual Lab</h2>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => void loadTraces()}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>
      <p className="muted small" style={{ margin: 0 }}>
        对故障 Trace 施加单一干预后重跑，对比观察结果。使用模拟模型与白名单工具，写入独立 Trace。
      </p>

      {error && <ErrorBox message={error} />}

      <div className="cf-layout">
        <Card title="实验配置">
          <div className="stack">
            <div className="field">
              <div className="k">原始故障 Trace</div>
              <select
                value={traceId}
                onChange={(e) => {
                  setTraceId(e.target.value)
                  setUrlTraceId(e.target.value)
                }}
                aria-label="选择 Trace"
              >
                {traces.length === 0 && (
                  <option value="">Trace 列表为空（加载失败或目录无数据）</option>
                )}
                {traces.map((t) => (
                  <option key={t.trace_id} value={t.trace_id}>
                    {t.trace_id}{t.fault_types.length ? ' · 故障: ' + t.fault_types.join('/') : ''}
                  </option>
                ))}
              </select>
            </div>

            {selected && (
              <div className="cf-info">
                <div><span className="muted">状态</span> <StatusBadge status={selected.status} /></div>
                <div><span className="muted">步骤</span> {selected.step_count}</div>
                <div><span className="muted">故障</span> {selected.fault_types.join(', ') || '无'}</div>
                <div className="mono small">{selected.trace_id}</div>
              </div>
            )}

            {eligibility && !eligibility.eligible && (
              <div className="errorbox" style={{ background: 'var(--warning-soft)', color: 'var(--warning)' }}>
                该 Trace 不满足反事实实验条件：{eligibility.reasons.join('；')}
              </div>
            )}
            {urlStepId && (
              <div className="cf-note small">
                来源节点：<span className="mono">{urlStepId}</span>（实验以整条 Trace 为单位）
              </div>
            )}

            <div className="field">
              <div className="k">干预类型（白名单）</div>
              <select value={intervType} onChange={(e) => setIntervType(e.target.value as IntervType)}>
                <option value="remove_fault">remove_fault · 关闭故障</option>
                <option value="replace_tool_result">replace_tool_result · 替换工具结果</option>
                <option value="adjust_timeout_retry">adjust_timeout_retry · 调整超时/重试</option>
              </select>
            </div>

            {intervType === 'replace_tool_result' && (
              <>
                <div className="field">
                  <div className="k">目标工具（白名单）</div>
                  <select value={replaceTool} onChange={(e) => setReplaceTool(e.target.value)}>
                    <option value="calculator">calculator</option>
                    <option value="text_analyzer">text_analyzer</option>
                  </select>
                </div>
                <div className="field">
                  <div className="k">替换结果（JSON，≤4KB）</div>
                  <textarea
                    className="cf-textarea"
                    value={replaceValue}
                    onChange={(e) => setReplaceValue(e.target.value)}
                    rows={3}
                  />
                </div>
              </>
            )}

            {intervType === 'adjust_timeout_retry' && (
              <div className="field">
                <div className="k">最大重试次数（0–5）</div>
                <input
                  type="number"
                  min={0}
                  max={5}
                  value={maxRetries}
                  onChange={(e) => setMaxRetries(Number(e.target.value))}
                />
              </div>
            )}

            <div className="field">
              <div className="k">重放次数（1–5，用于可复现性判定）</div>
              <input
                type="number"
                min={1}
                max={5}
                value={replays}
                onChange={(e) => setReplays(Number(e.target.value))}
              />
            </div>

            <div className="row">
              <button
                className="btn primary"
                onClick={() => void runExperiment()}
                disabled={busy || !traceId || eligibility?.eligible === false}
                title={eligibility?.eligible === false ? eligibility.reasons.join('；') : undefined}
              >
                <FlaskConical size={15} /> 运行实验
              </button>
              {busy && <span className="muted small">实验中…</span>}
            </div>

            <div className="cf-note">
              <strong>实验控制条件</strong>
              <ul>
                <li>固定白名单场景</li>
                <li>模拟模型（calculator / text_analyzer）</li>
                <li>单变量干预</li>
                <li>重试与重放均 ≤5 次</li>
              </ul>
            </div>
          </div>
        </Card>

        <div className="stack">
          <Card title="实验状态">
            {!experimentId ? (
              <EmptyState text="尚未运行实验" />
            ) : (
              <div className="stack small">
                <div className="row">
                  <span className="muted">实验 ID</span>
                  <span className="mono">{experimentId}</span>
                </div>
                <div className="row">
                  <span className="muted">状态</span>
                  <span className={'badge ' + (expStatus === 'succeeded' ? 'success' : expStatus === 'failed' ? 'failed' : 'blue')}>
                    {expStatus === 'succeeded' ? '已完成' : expStatus === 'failed' ? '失败' : '运行中'}
                  </span>
                  {dedup && <span className="badge muted">复用已有实验</span>}
                </div>
                {busy && <Spinner />}
              </div>
            )}
          </Card>

          {strength && (
            <Card title="Evidence Strength · 证据强度">
              <div className="stack">
                <div className="row">
                  <span className={'badge ' + strength.cls} style={{ fontSize: 13 }}>{strength.tag}</span>
                  {results?.environment?.model?.simulated && <span className="badge muted">模拟模型</span>}
                  {results?.reproducible && <span className="badge success">可复现</span>}
                </div>
                <p className="small" style={{ margin: 0 }}>{strength.why}</p>
                <div className="ev-grid">
                  <div className="ev-box">
                    <div className="ev-title">已控制条件</div>
                    <ul>{(results?.control_conditions ?? []).map((c) => <li key={c}>{c}</li>)}</ul>
                  </div>
                  <div className="ev-box warn">
                    <div className="ev-title">未控制 / 潜在混杂</div>
                    <ul>{(results?.confounders ?? []).map((c) => <li key={c}>{c}</li>)}</ul>
                  </div>
                </div>
                <div className="cf-note">
                  <strong>限制说明</strong>
                  <ul>{(results?.limitations ?? []).map((l) => <li key={l}>{l}</li>)}</ul>
                </div>
                <div className="muted small">
                  因果证据等级：{results?.causal_evidence_level} · {results?.causal_evidence_note}
                </div>
              </div>
            </Card>
          )}

          {results?.status === 'succeeded' && (
            <>
              <Card title="原始 vs 干预（双轨对照）">
                <div className="cf-track">
                  <div className="cf-track-col">
                    <div className="cf-track-head">观察（原始）</div>
                    <div className="small">状态 <StatusBadge status={results.observed_outcome!.status} /></div>
                    <div className="small">故障 {results.observed_outcome!.fault_types.join(', ') || '无'}</div>
                    <div className="small muted">未重新执行</div>
                    <button className="btn" onClick={() => navigate('/dag/' + results.observed_outcome!.trace_id)}>
                      查看 DAG
                    </button>
                  </div>
                  <div className="cf-track-arrow">→</div>
                  <div className="cf-track-col">
                    <div className="cf-track-head">干预（反事实）</div>
                    <div className="small">状态 <StatusBadge status={results.intervention_outcome!.status} /></div>
                    <div className="small">故障 {results.intervention_outcome!.fault_types.join(', ') || '无'}</div>
                    <div className="small muted">重试 {results.intervention_outcome!.attempts ?? '—'} 次</div>
                  </div>
                </div>
                <div className="row" style={{ gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
                  <button
                    className="btn"
                    onClick={() =>
                      navigate('/repair?trace_id=' + encodeURIComponent(results.observed_outcome!.trace_id))
                    }
                  >
                    下一步：最小修复验证
                  </button>
                  <button
                    className="btn"
                    onClick={() =>
                      navigate('/attribution?trace_id=' + encodeURIComponent(results.observed_outcome!.trace_id))
                    }
                  >
                    回到故障归因
                  </button>
                </div>
              </Card>

              <Card title="差异摘要">
                <div className="stack small">
                  <div>状态变化：{results.observed_difference!.status_changed ? '是' : '否'}</div>
                  <div>
                    移除故障：{results.observed_difference!.fault_types_removed.join(', ') || '无'}
                  </div>
                  <div>新增故障：{results.observed_difference!.fault_types_added.join(', ') || '无'}</div>
                  <div>差异节点数：{results.observed_difference!.comparison_summary.diff_node_count}</div>
                  <div className="muted">
                    未匹配步骤：原始独有 {results.observed_difference!.comparison_summary.unmatched_reference.length}，
                    干预独有 {results.observed_difference!.comparison_summary.unmatched_target.length}
                  </div>
                  <div className="muted">原始 Trace 是否被修改：{results.original_trace_modified ? '是' : '否'}</div>
                </div>
              </Card>

              <Card title="Trace Diff（结构化对比）">
                <pre className="json">{JSON.stringify(results.observed_difference, null, 2)}</pre>
              </Card>
            </>
          )}
        </div>
      </div>

      {experimentId && (
        <Card title="证据血缘（Evidence Lineage）">
          <LineagePanel kind="experiment" ids={{ experimentId }} />
        </Card>
      )}
    </div>
  )
}

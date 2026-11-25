import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Play, RefreshCw } from 'lucide-react'
import { api, apiErrorMessage } from '../api'
import { Card, EmptyState, ErrorBox, Spinner } from '../components/ui'

interface ConfigItem {
  config_id: string
  title: string
  description: string
  judge_count: number
  expected_relationship: string
  limitation: string
  judges: { judge_id: string; mode: string; variant: string | null; sampling_index: number }[]
}

interface JudgeMeta {
  judge_id: string
  mode: string
  variant: string | null
  model_name: string | null
  prompt_version: string | null
  provider_kind: string | null
  is_mock: boolean | null
  sampling_index: number
}

interface JudgeResult {
  status: string
  score: number | null
  normalized: number | null
  model_name?: string
  prompt_version?: string
  is_mock?: boolean
  unknown_dimensions?: string[]
  dimension_scores?: Record<string, number | null>
  evidence?: Record<string, string[]>
  reason?: string
  error?: string
  latency_ms?: number
}

interface CaseRow {
  case_id: string
  trace_id: string
  judges: Record<string, JudgeResult>
}

interface DisCase {
  case_id: string
  trace_id: string
  judge_count: number
  scored_count: number
  unknown_judges: string[]
  max_diff: number | null
  stdev: number | null
  variance: number | null
  mean: number | null
  median: number | null
  agreement: number | null
  distribution: Record<string, number>
  disagreement_flagged: boolean
  severe: boolean
  sufficient_for_conclusion: boolean
  evidence_conflicts: { dimension: string; judges: string[]; detail: string; evidence_by_judge: Record<string, string[]> }[]
  reason_summary: Record<string, string>
  human_review_required: boolean
}

interface ArbCase {
  case_id: string
  status: string
  aggregated_score: number | null
  aggregated_normalized: number | null
  strategy: string
  judge_scores: Record<string, number>
  unknown_judges: string[]
  max_diff: number | null
  severe: boolean
  disagreement_flagged: boolean
  human_review_required: boolean
  reason: string
}

interface Independence {
  level: string
  note: string
  not_independent: boolean
  distinct_model_count: number
  distinct_prompt_count: number
  all_mock: boolean
  mock_judges: string[]
  real_model_judges: string[]
  identity_map: Record<string, { model_identity: string; prompt_version: string }>
  disclaimer: string
}

interface RunStatus {
  run_id: string
  status: string
  rubric: { rubric_id: string; version: string; dimensions: string[] }
  judges: JudgeMeta[]
  independence: Independence
  case_count: number
  case_ids: string[]
  summary: {
    flagged_count: number
    conclusive_count: number
    inconclusive_count: number
    human_review_required_count: number
    aggregated_mean: number | null
    strategy: string
  }
  engine_version: string
  paid_api_called: boolean
  limitations: string[]
  error?: string
}

const LEVEL_BADGE: Record<string, string> = {
  independent_models: 'success',
  prompt_variants_only: 'warning',
  mock_prompt_variants: 'warning',
  correlated: 'failed',
}

export default function DisagreementExplorer() {
  const navigate = useNavigate()
  const [configs, setConfigs] = useState<ConfigItem[]>([])
  const [configId, setConfigId] = useState('')
  const [strategy, setStrategy] = useState<'median' | 'mean'>('median')
  const [maxCases, setMaxCases] = useState(3)

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [run, setRun] = useState<RunStatus | null>(null)
  const [scores, setScores] = useState<CaseRow[]>([])
  const [disc, setDisc] = useState<{ cases: DisCase[]; flagged_count: number; aggregate: Record<string, unknown>; flagThreshold?: number; threshold: number; severe_threshold: number } | null>(null)
  const [arb, setArb] = useState<{ cases: ArbCase[]; summary: Record<string, unknown>; strategy: string; note: string } | null>(null)
  const [activeCase, setActiveCase] = useState<string | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      const r = await api.get<{ configs: ConfigItem[] }>('/api/evaluation/multi-judge/configs')
      setConfigs(r.data.configs)
      if (r.data.configs[0]) setConfigId(r.data.configs[0].config_id)
    } catch (e) {
      setError(apiErrorMessage(e))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const selected = configs.find((c) => c.config_id === configId)

  async function runIt() {
    if (!configId) return
    setBusy(true)
    setError(null)
    setRun(null)
    setScores([])
    setDisc(null)
    setArb(null)
    setActiveCase(null)
    try {
      const created = await api.post<{ run_id: string }>('/api/evaluation/multi-judge/runs', {
        dataset_id: 'tracepilot-eval-v1',
        config_id: configId,
        strategy,
        max_cases: maxCases,
      })
      const rid = created.data.run_id
      let status = 'running'
      for (let i = 0; i < 600; i++) {
        const st = await api.get<RunStatus & { error?: string }>(`/api/evaluation/multi-judge/runs/${rid}`)
        status = st.data.status
        if (status !== 'running') {
          if (status === 'succeeded') setRun(st.data)
          else setError(st.data.error ?? '多 Judge 评测失败')
          break
        }
        await new Promise((r) => setTimeout(r, 200))
      }
      if (status !== 'succeeded') return

      const [sc, dg, ab] = await Promise.all([
        api.get<{ cases: CaseRow[] }>(`/api/evaluation/multi-judge/runs/${rid}/scores`),
        api.get<{ disagreement: typeof disc }>(`/api/evaluation/multi-judge/runs/${rid}/disagreement`),
        api.get<{ arbitration: typeof arb }>(`/api/evaluation/multi-judge/runs/${rid}/arbitration`),
      ])
      setScores(sc.data.cases)
      setDisc(dg.data.disagreement)
      setArb(ab.data.arbitration)
      if (sc.data.cases[0]) setActiveCase(sc.data.cases[0].case_id)
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  const currentDisc = disc?.cases.find((c) => c.case_id === activeCase)
  const currentArb = arb?.cases.find((c) => c.case_id === activeCase)
  const currentScores = scores.find((c) => c.case_id === activeCase)

  return (
    <div className="stack">
      <div className="row">
        <button className="btn" onClick={() => navigate('/evaluation')}>
          <ArrowLeft size={15} /> 返回 Evaluation Center
        </button>
        <h2 style={{ margin: 0 }}>Disagreement Explorer</h2>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => void load()}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>
      <p className="muted small" style={{ margin: 0 }}>
        多个 Judge 独立评分后做分歧检测与仲裁；缺失评分不计 0，严重分歧返回
        <strong> INCONCLUSIVE</strong>。预设配置均为 Mock。
      </p>

      {error && <ErrorBox message={error} />}

      <div className="split-config">
        <div className="stack">
      <Card title="多 Judge 配置">
        <div className="stack">
          <div className="row" style={{ flexWrap: 'wrap' }}>
            <label className="small muted" htmlFor="cfg">配置组</label>
            <select id="cfg" value={configId} onChange={(e) => setConfigId(e.target.value)}>
              {configs.map((c) => (
                <option key={c.config_id} value={c.config_id}>
                  {c.config_id} · {c.judge_count} 个 Judge
                </option>
              ))}
            </select>

            <label className="small muted" htmlFor="stg">仲裁策略</label>
            <select id="stg" value={strategy} onChange={(e) => setStrategy(e.target.value as 'median' | 'mean')}>
              <option value="median">Median</option>
              <option value="mean">Mean</option>
            </select>

            <label className="small muted" htmlFor="mc">案例数</label>
            <input
              id="mc"
              type="number"
              min={1}
              max={30}
              value={maxCases}
              onChange={(e) => setMaxCases(Number(e.target.value))}
              style={{ width: 80 }}
            />

            <button className="btn primary" onClick={() => void runIt()} disabled={busy || !configId}>
              <Play size={15} /> 运行多 Judge 评测
            </button>
            {busy && <span className="muted small">评测中…</span>}
          </div>

          {selected && (
            <div className="cf-info">
              <div><strong>{selected.title}</strong></div>
              <div className="muted small">{selected.description}</div>
              <div>
                <span className="muted">预期关系</span>{' '}
                <span className="badge muted">{selected.expected_relationship}</span>
              </div>
              <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
                {selected.judges.map((j) => (
                  <span key={j.judge_id} className="mono small badge muted">
                    {j.judge_id}
                    {j.variant ? ` · ${j.variant}` : ''}
                  </span>
                ))}
              </div>
              <div className="errorbox" style={{ background: 'var(--warning-soft)', color: 'var(--warning)' }}>
                {selected.limitation}
              </div>
            </div>
          )}
        </div>
      </Card>
        </div>

        <div className="stack">
      {busy && <Spinner />}
      {!busy && !run && !error && <EmptyState text="选择配置组后点击「运行多 Judge 评测」" />}

      {run && (
        <>
          {/* 独立性诚实声明（醒目） */}
          <Card title="独立性评估">
            <div className="stack small">
              <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                <span className={'badge ' + (LEVEL_BADGE[run.independence.level] ?? 'muted')}>
                  {run.independence.level}
                </span>
                {run.independence.not_independent && <span className="badge failed">不构成独立评测</span>}
                {run.independence.all_mock && <span className="badge warning">全部 Mock</span>}
                <span className="muted">
                  不同模型 {run.independence.distinct_model_count} · 不同 Prompt{' '}
                  {run.independence.distinct_prompt_count}
                </span>
              </div>
              <div>{run.independence.note}</div>
              {run.independence.disclaimer && (
                <div className="errorbox" style={{ background: 'var(--warning-soft)', color: 'var(--warning)' }}>
                  {run.independence.disclaimer}
                </div>
              )}
            </div>
          </Card>

          <div className="grid stats">
            <div className="card stat">
              <div className="k">分歧案例</div>
              <div className="v red">{run.summary.flagged_count}</div>
              <div className="muted small">/ {run.case_count}（阈值 {disc?.threshold ?? '—'}）</div>
            </div>
            <div className="card stat">
              <div className="k">可得出结论</div>
              <div className="v green">{run.summary.conclusive_count}</div>
              <div className="muted small">聚合策略 {run.summary.strategy}</div>
            </div>
            <div className="card stat">
              <div className="k">INCONCLUSIVE</div>
              <div className="v">{run.summary.inconclusive_count}</div>
              <div className="muted small">严重分歧或有效评分不足</div>
            </div>
            <div className="card stat">
              <div className="k">需人工复核</div>
              <div className="v">{run.summary.human_review_required_count}</div>
              <div className="muted small">
                聚合均值 {run.summary.aggregated_mean ?? 'N/A'}
              </div>
            </div>
          </div>

          <Card title="各 Judge 身份与版本">
            <table className="tbl">
              <thead>
                <tr><th>Judge</th><th>模式</th><th>变体</th><th>模型</th><th>Prompt</th><th>Mock</th></tr>
              </thead>
              <tbody>
                {run.judges.map((j) => (
                  <tr key={j.judge_id}>
                    <td className="mono small">{j.judge_id}</td>
                    <td className="small">{j.mode}</td>
                    <td className="small">{j.variant ?? '—'}</td>
                    <td className="mono small">{j.model_name ?? '—'}</td>
                    <td className="mono small">{j.prompt_version ?? '—'}</td>
                    <td>{j.is_mock ? <span className="badge warning">MOCK</span> : <span className="badge blue">REAL</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted small" style={{ marginBottom: 0 }}>
              Rubric {run.rubric.rubric_id} v{run.rubric.version} · 引擎 {run.engine_version} · 付费 API：{run.paid_api_called ? '是' : '否'}
            </p>
          </Card>

          <Card title="案例选择"
            action={
              <button className="btn" onClick={() => activeCase && navigate('/dag/' + (currentScores?.trace_id ?? ''))}>
                查看该案例 DAG
              </button>
            }
          >
            <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
              {(disc?.cases ?? []).map((c) => (
                <button
                  key={c.case_id}
                  className={
                    'tl-chip' +
                    (activeCase === c.case_id ? ' current' : '') +
                    (c.severe ? ' fault' : c.disagreement_flagged ? ' fault' : '')
                  }
                  onClick={() => setActiveCase(c.case_id)}
                  title={`max_diff=${c.max_diff} severe=${c.severe}`}
                >
                  {c.case_id}{c.severe ? ' ⚠' : c.disagreement_flagged ? ' !' : ''}
                </button>
              ))}
            </div>
          </Card>

          {/* 并列 Judge 卡片（分歧证据对照） */}
          {currentScores && (
            <Card title={'并列 Judge 评分 · ' + currentScores.case_id}>
              <div className="mj-grid">
                {Object.entries(currentScores.judges).map(([jid, v]) => {
                  const isUnknown = v.status !== 'SCORED'
                  return (
                    <div key={jid} className={'mj-card' + (isUnknown ? ' unknown' : '')}>
                      <div className="row" style={{ justifyContent: 'space-between' }}>
                        <span className="mono small">{jid}</span>
                        {isUnknown ? (
                          <span className="badge warning">{v.status}</span>
                        ) : (
                          <span className={'badge ' + ((v.score ?? 0) >= 4 ? 'success' : (v.score ?? 0) >= 3 ? 'blue' : 'failed')}>
                            {v.score} / 5
                          </span>
                        )}
                      </div>
                      <div className="muted small" style={{ marginTop: 4 }}>
                        {v.model_name} · {v.prompt_version}
                      </div>
                      {v.error && <div className="errorbox" style={{ marginTop: 6 }}>{v.error}</div>}
                      {v.unknown_dimensions && v.unknown_dimensions.length > 0 && (
                        <div className="muted small" style={{ marginTop: 4 }}>
                          UNKNOWN 维度：{v.unknown_dimensions.join(', ')}
                        </div>
                      )}
                      {v.dimension_scores && (
                        <table className="tbl" style={{ marginTop: 6 }}>
                          <tbody>
                            {Object.entries(v.dimension_scores).map(([d, s]) => (
                              <tr key={d}>
                                <td className="small muted">{d}</td>
                                <td className="mono small">{s === null ? 'UNKNOWN' : s}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      )}
                      {v.reason && (
                        <div className="small" style={{ marginTop: 6 }}>
                          <span className="muted">理由：</span>
                          {v.reason.slice(0, 160)}
                          {v.reason.length > 160 ? '…' : ''}
                        </div>
                      )}
                      {v.evidence && Object.keys(v.evidence).length > 0 && (
                        <div style={{ marginTop: 6 }}>
                          <div className="ev-title">引用证据</div>
                          {Object.entries(v.evidence).slice(0, 2).map(([dim, evs]) => (
                            <div key={dim} className="small">
                              <span className="muted">{dim}:</span>
                              <ul style={{ margin: '2px 0 0 14px' }}>
                                {evs.slice(0, 2).map((e, i) => <li key={i}>{e}</li>)}
                              </ul>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </Card>
          )}

          {/* 分歧指标 */}
          {currentDisc && (
            <Card title="分歧指标">
              <div className="stack small">
                <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 10 }}>
                  <div className="cf-info"><div className="muted">最大分差</div><div className="mono">{currentDisc.max_diff ?? 'N/A'}</div></div>
                  <div className="cf-info"><div className="muted">标准差</div><div className="mono">{currentDisc.stdev ?? 'N/A'}</div></div>
                  <div className="cf-info"><div className="muted">方差</div><div className="mono">{currentDisc.variance ?? 'N/A'}</div></div>
                  <div className="cf-info"><div className="muted">一致性</div><div className="mono">{currentDisc.agreement ?? 'N/A'}</div></div>
                  <div className="cf-info"><div className="muted">均值 / 中位数</div><div className="mono">{currentDisc.mean ?? 'N/A'} / {currentDisc.median ?? 'N/A'}</div></div>
                  <div className="cf-info"><div className="muted">有效评分</div><div className="mono">{currentDisc.scored_count} / {currentDisc.judge_count}</div></div>
                </div>

                <div>
                  <strong>评分分布（归一化 1–5）</strong>
                  <div className="row" style={{ gap: 8, marginTop: 4 }}>
                    {['1', '2', '3', '4', '5'].map((k) => (
                      <span key={k} className="badge muted">
                        {k} 分 × {currentDisc.distribution[k] ?? 0}
                      </span>
                    ))}
                  </div>
                  <div className="muted" style={{ marginTop: 4 }}>
                    缺失评分不计入分布，也不按 0 处理
                  </div>
                </div>

                {currentDisc.unknown_judges.length > 0 && (
                  <div className="muted">UNKNOWN Judge：{currentDisc.unknown_judges.join(', ')}</div>
                )}

                {currentDisc.evidence_conflicts.length > 0 && (
                  <div>
                    <strong>证据冲突（{currentDisc.evidence_conflicts.length}）</strong>
                    {currentDisc.evidence_conflicts.map((cf) => (
                      <div key={cf.dimension} className="errorbox" style={{ marginTop: 6 }}>
                        <div><strong>{cf.dimension}</strong>：{cf.detail}</div>
                        {Object.entries(cf.evidence_by_judge).map(([jid, evs]) => (
                          <div key={jid} className="small mono">
                            {jid} → {evs.join(' | ').slice(0, 100)}
                          </div>
                        ))}
                      </div>
                    ))}
                  </div>
                )}

                <div>
                  <strong>理由差异摘要</strong>
                  {Object.entries(currentDisc.reason_summary).map(([jid, rs]) => (
                    <div key={jid} className="small">
                      <span className="mono muted">{jid}:</span> {rs.slice(0, 140)}{rs.length > 140 ? '…' : ''}
                    </div>
                  ))}
                </div>
              </div>
            </Card>
          )}

          {/* 仲裁结果 */}
          {currentArb && arb && (
            <Card title={'仲裁结果（策略：' + arb.strategy + '）'}>
              <div className="stack small">
                <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
                  <span className={'badge ' + (currentArb.status === 'SCORED' ? 'success' : 'warning')}>
                    {currentArb.status}
                  </span>
                  <span>
                    聚合分：
                    <strong>{currentArb.aggregated_score === null ? 'N/A（不输出确定性结论）' : currentArb.aggregated_score}</strong>
                  </span>
                  {currentArb.aggregated_normalized !== null && (
                    <span className="muted">归一化 {currentArb.aggregated_normalized} / 5</span>
                  )}
                  {currentArb.human_review_required && <span className="badge failed">需人工复核</span>}
                </div>

                {currentArb.reason && <div className="errorbox">{currentArb.reason}</div>}

                <div>
                  <strong>参与聚合的评分</strong>
                  <div className="row" style={{ gap: 8, marginTop: 4 }}>
                    {Object.entries(currentArb.judge_scores).map(([jid, s]) => (
                      <span key={jid} className="badge muted">{jid} = {s}</span>
                    ))}
                    {currentArb.unknown_judges.map((jid) => (
                      <span key={jid} className="badge warning">{jid} = 未参与</span>
                    ))}
                  </div>
                </div>

                <div className="cf-note">
                  <strong>人工复核建议</strong>
                  <div>
                    {currentArb.human_review_required
                      ? '该案例存在分歧或有效评分不足，建议人工确认后再使用结论。'
                      : '分歧未达阈值，可参考聚合结果（非客观真值）。'}
                  </div>
                </div>

                <div className="muted">{arb.note}</div>
              </div>
            </Card>
          )}

          <Card title="评测限制">
            <ul style={{ margin: 0, paddingLeft: 16 }} className="small">
              {run.limitations.map((l) => <li key={l}>{l}</li>)}
            </ul>
          </Card>
        </>
      )}
        </div>
      </div>
    </div>
  )
}

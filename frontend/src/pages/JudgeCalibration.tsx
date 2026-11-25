import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Play, RefreshCw } from 'lucide-react'
import { api, apiErrorMessage } from '../api'
import { Card, EmptyState, ErrorBox, Spinner } from '../components/ui'

interface CalDataset {
  calibration_id: string
  version: string
  rubric_id: string
  case_count: number
  scored_cases: number
  unknown_cases: number
  annotation_source: string
  annotation_note: string
  categories: string[]
}

interface ConfusionMatrix {
  [human: string]: { [judge: string]: number }
}

interface Metrics {
  evaluable_cases: number
  exact_agreement: number | null
  exact_agreement_count?: number
  mae: number | null
  within_1_agreement: number | null
  within_1_agreement_count?: number
  confusion_matrix: ConfusionMatrix
  denominator_definition: string
}

interface BiasItem {
  case_id: string
  category: string
  human_score: number
  judge_score: number
  delta: number
  rationale: string
}

interface CaseRow {
  case_id: string
  category: string
  rubric_id: string
  human_score: number | null
  judge_score: number | null
  judge_score_normalized: number | null
  delta: number | null
  annotation_rationale: string
  repeat_scores: (number | null)[]
  unknown_dimensions: string[]
  evidence: Record<string, string[]>
  dimension_scores: Record<string, number | null>
}

interface CalibrationReport {
  calibration_engine_version: string
  dataset: {
    calibration_id: string
    version: string
    rubric_id: string
    annotation_source: string
    annotation_note: string
    total_cases: number
    evaluated_cases: number
  }
  judge: {
    provider_kind: string
    model_name: string
    is_mock: boolean | null
    simulated: boolean | null
    rubric_id: string
    rubric_version: string | null
    prompt_version: string | null
    judge_engine_version: string | null
    repeats: number
  }
  metrics: Metrics
  bias: { overestimated: BiasItem[]; underestimated: BiasItem[]; aligned: BiasItem[] }
  stability: {
    repeats: number
    mean_within_case_std: number | null
    max_within_case_std: number | null
    exact_stable: boolean | null
    normalized_stable: boolean | null
    consistent: boolean | null
    note: string
  }
  differences: {
    case_id: string
    category: string
    human_score: number | null
    judge_score: number | null
    delta: number | null
    unknown_dimensions: string[]
    rationale: string
  }[]
  cases: CaseRow[]
  unknown: {
    reference_unknown_cases: { case_id: string; judge_score: number | null }[]
    judge_unknown_cases: { case_id: string; human_score: number; unknown_dimensions: string[] }[]
    execution_errors: { case_id: string; error: string }[]
    note: string
  }
  disclaimer: string
  limitations: string[]
  paid_api_called: boolean
}

function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? 'N/A' : (v * 100).toFixed(1) + '%'
}

export default function JudgeCalibration() {
  const navigate = useNavigate()
  const [datasets, setDatasets] = useState<CalDataset[]>([])
  const [datasetId, setDatasetId] = useState('')
  const [judgeMode, setJudgeMode] = useState<'mock' | 'deepseek'>('mock')
  const [repeats, setRepeats] = useState(2)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [report, setReport] = useState<CalibrationReport | null>(null)
  const [tab, setTab] = useState<'over' | 'under' | 'all' | 'unknown'>('over')
  const [detailCase, setDetailCase] = useState<CaseRow | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      const r = await api.get<{ datasets: CalDataset[] }>('/api/evaluation/calibration/datasets')
      setDatasets(r.data.datasets)
      if (r.data.datasets[0]) setDatasetId(r.data.datasets[0].calibration_id)
    } catch (e) {
      setError(apiErrorMessage(e))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const selected = datasets.find((d) => d.calibration_id === datasetId)

  async function run() {
    if (!datasetId) return
    setBusy(true)
    setError(null)
    setReport(null)
    setDetailCase(null)
    try {
      const r = await api.post<{ report: CalibrationReport }>('/api/evaluation/calibration/runs', {
        dataset_id: datasetId,
        judge_mode: judgeMode,
        repeats,
      })
      setReport(r.data.report)
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  const matrixLevels = ['1', '2', '3', '4', '5']
  const cells = report ? matrixLevels.map((h) => matrixLevels.map((j) => report.metrics.confusion_matrix[h]?.[j] ?? 0)) : []
  const maxCell = cells.length ? Math.max(1, ...cells.flat()) : 1

  return (
    <div className="stack">
      <div className="row">
        <button className="btn" onClick={() => navigate('/evaluation')}>
          <ArrowLeft size={15} /> 返回 Evaluation Center
        </button>
        <h2 style={{ margin: 0 }}>Judge Calibration</h2>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => void load()}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>
      <p className="muted small" style={{ margin: 0 }}>
        用参考标注检验 LLM Judge 的一致性与稳定性；标注来源为
        <strong> synthetic_reference</strong>（非独立双盲标注），一致率不等同真实人工标注。
      </p>

      {error && <ErrorBox message={error} />}

      <div className="split-config">
        <div className="stack">
      <Card title="校准配置">
        <div className="stack">
          <div className="row" style={{ flexWrap: 'wrap' }}>
            <label className="small muted" htmlFor="cds">校准数据集</label>
            <select id="cds" value={datasetId} onChange={(e) => setDatasetId(e.target.value)}>
              {datasets.map((d) => (
                <option key={d.calibration_id} value={d.calibration_id}>
                  {d.calibration_id} · v{d.version} · {d.case_count} 案例
                </option>
              ))}
            </select>

            <label className="small muted" htmlFor="cm">Judge 模式</label>
            <select id="cm" value={judgeMode} onChange={(e) => setJudgeMode(e.target.value as 'mock' | 'deepseek')}>
              <option value="mock">Mock Judge（默认，零成本）</option>
              <option value="deepseek">Real Judge（需服务端授权）</option>
            </select>

            <label className="small muted" htmlFor="cr">重复评分</label>
            <input
              id="cr"
              type="number"
              min={1}
              max={5}
              value={repeats}
              onChange={(e) => setRepeats(Number(e.target.value))}
              style={{ width: 80 }}
            />

            <button className="btn primary" onClick={() => void run()} disabled={busy || !datasetId}>
              <Play size={15} /> 运行校准
            </button>
            {busy && <span className="muted small">校准中…</span>}
          </div>

          {selected && (
            <div className="cf-info">
              <div><span className="muted">Rubric</span> {selected.rubric_id}</div>
              <div><span className="muted">版本</span> {selected.version}</div>
              <div>
                <span className="muted">案例</span> {selected.case_count}
                （已标注 {selected.scored_cases} / UNKNOWN {selected.unknown_cases}）
              </div>
              <div>
                <span className="muted">标注来源</span>{' '}
                <span className={'badge ' + (selected.annotation_source === 'synthetic_reference' ? 'warning' : 'success')}>
                  {selected.annotation_source}
                </span>
              </div>
              <div className="muted small">{selected.annotation_note}</div>
            </div>
          )}
        </div>
      </Card>
        </div>

        <div className="stack">
      {busy && <Spinner />}
      {!busy && !report && !error && <EmptyState text="选择校准集后点击「运行校准」" />}

      {report && (
        <>
          <div className="grid stats">
            <div className="card stat">
              <div className="k">完全一致率</div>
              <div className="v blue">{pct(report.metrics.exact_agreement)}</div>
              <div className="muted small">
                {report.metrics.exact_agreement_count ?? 0} / {report.metrics.evaluable_cases}（四舍五入到 1–5 后完全一致）
              </div>
            </div>
            <div className="card stat">
              <div className="k">MAE（平均绝对误差）</div>
              <div className="v">{report.metrics.mae === null ? 'N/A' : report.metrics.mae}</div>
              <div className="muted small">|Judge 总分 − 参考分| 均值</div>
            </div>
            <div className="card stat">
              <div className="k">偏差 ≤1 分一致率</div>
              <div className="v green">{pct(report.metrics.within_1_agreement)}</div>
              <div className="muted small">
                {report.metrics.within_1_agreement_count ?? 0} / {report.metrics.evaluable_cases}（偏差 ≤ 1 分）
              </div>
            </div>
            <div className="card stat">
              <div className="k">稳定性（平均逐案例标准差）</div>
              <div className="v">
                {report.stability.mean_within_case_std === null ? 'N/A' : report.stability.mean_within_case_std}
              </div>
              <div className="muted small">
                重复 {report.stability.repeats} 次 · {report.stability.normalized_stable ? '整级一致' : '存在波动'}
              </div>
            </div>
          </div>

          <Card title="评审器与版本信息">
            <div className="stack small">
              <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
                <span className={'badge ' + (report.judge.is_mock ? 'warning' : 'blue')}>
                  {report.judge.is_mock ? 'MOCK（模拟评审）' : 'REAL（真实模型）'}
                </span>
                <span className="badge muted">{report.judge.provider_kind}</span>
                <span className="mono small">{report.judge.model_name}</span>
              </div>
              <div>
                Rubric {report.judge.rubric_id} v{report.judge.rubric_version ?? '?'} · Prompt{' '}
                {report.judge.prompt_version ?? '?'} · Judge Engine {report.judge.judge_engine_version ?? '?'} ·
                校准引擎 {report.calibration_engine_version} · 数据集 v{report.dataset.version}
              </div>
              <div className="muted">
                参考标注来源：{report.dataset.annotation_source} · 评估案例 {report.dataset.evaluated_cases}/
                {report.dataset.total_cases} · 统计分母：{report.metrics.denominator_definition}
              </div>
              <div className="muted">
                付费 API 调用：{report.paid_api_called ? '是' : '否'}
              </div>
            </div>
          </Card>

          <Card title="Confusion Matrix（行 = 参考分，列 = Judge 归一化分）">
            <div className="cm-wrap">
              <table className="tbl cm-table">
                <thead>
                  <tr>
                    <th>参考 \\ Judge</th>
                    {matrixLevels.map((j) => <th key={j}>{j} 分</th>)}
                  </tr>
                </thead>
                <tbody>
                  {matrixLevels.map((h, hi) => (
                    <tr key={h}>
                      <th>{h} 分</th>
                      {matrixLevels.map((j, ji) => {
                        const v = cells[hi]?.[ji] ?? 0
                        const intensity = v / maxCell
                        return (
                          <td
                            key={j}
                            className="cm-cell"
                            style={{
                              background:
                                v === 0
                                  ? 'transparent'
                                  : hi === ji
                                    ? `rgba(22,163,74,${0.12 + intensity * 0.45})`
                                    : `rgba(220,38,38,${0.08 + intensity * 0.35})`,
                            }}
                            title={`参考 ${h} 分 / Judge ${j} 分：${v} 例`}
                          >
                            {v}
                          </td>
                        )
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="muted small" style={{ marginBottom: 0 }}>
              绿：一致；红：偏离。Mock Judge 为规则化模拟，不代表真实模型结果。
            </p>
          </Card>

          <Card
            title="评分差异分析"
            action={
              <div className="row" style={{ gap: 6 }}>
                {[
                  { k: 'over', l: `高估 ${report.bias.overestimated.length}` },
                  { k: 'under', l: `低估 ${report.bias.underestimated.length}` },
                  { k: 'all', l: '全部差异' },
                  { k: 'unknown', l: 'UNKNOWN' },
                ].map((t) => (
                  <button
                    key={t.k}
                    className={'btn' + (tab === t.k ? ' primary' : '')}
                    style={{ padding: '4px 10px' }}
                    onClick={() => setTab(t.k as typeof tab)}
                  >
                    {t.l}
                  </button>
                ))}
              </div>
            }
          >
            {tab === 'over' && (
              <BiasTable items={report.bias.overestimated} onOpen={setDetailCase} cases={report.cases} />
            )}
            {tab === 'under' && (
              <BiasTable items={report.bias.underestimated} onOpen={setDetailCase} cases={report.cases} />
            )}
            {tab === 'all' && (
              <table className="tbl">
                <thead>
                  <tr><th>案例</th><th>类别</th><th>参考</th><th>Judge</th><th>Δ</th><th>说明</th><th></th></tr>
                </thead>
                <tbody>
                  {report.differences.map((d) => (
                    <tr key={d.case_id}>
                      <td className="mono small">{d.case_id}</td>
                      <td className="small">{d.category}</td>
                      <td>{d.human_score ?? 'UNKNOWN'}</td>
                      <td>{d.judge_score ?? 'UNKNOWN'}</td>
                      <td className={d.delta === null ? '' : d.delta > 0 ? '' : ''}>
                        {d.delta === null ? '—' : (d.delta > 0 ? '+' : '') + d.delta}
                      </td>
                      <td className="small muted">{d.rationale.slice(0, 40)}…</td>
                      <td>
                        <button
                          className="btn"
                          style={{ padding: '2px 8px' }}
                          onClick={() => setDetailCase(report.cases.find((c) => c.case_id === d.case_id) ?? null)}
                        >
                          证据
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {tab === 'unknown' && (
              <div className="stack small">
                <div>
                  <strong>参考标注为 UNKNOWN（{report.unknown.reference_unknown_cases.length}）</strong>
                  <ul style={{ margin: '4px 0 0 16px' }}>
                    {report.unknown.reference_unknown_cases.map((u) => (
                      <li key={u.case_id} className="mono small">
                        {u.case_id} → Judge 总分 {u.judge_score ?? 'UNKNOWN'}
                      </li>
                    ))}
                  </ul>
                </div>
                <div>
                  <strong>Judge 给出 UNKNOWN（{report.unknown.judge_unknown_cases.length}）</strong>
                  <ul style={{ margin: '4px 0 0 16px' }}>
                    {report.unknown.judge_unknown_cases.map((u) => (
                      <li key={u.case_id} className="mono small">
                        {u.case_id}（参考 {u.human_score}）· UNKNOWN 维度 {u.unknown_dimensions.join(', ')}
                      </li>
                    ))}
                  </ul>
                </div>
                <div>
                  <strong>执行异常（{report.unknown.execution_errors.length}）</strong>
                  <ul style={{ margin: '4px 0 0 16px' }}>
                    {report.unknown.execution_errors.map((e) => (
                      <li key={e.case_id} className="mono small">{e.case_id}: {e.error}</li>
                    ))}
                  </ul>
                </div>
                <p className="muted" style={{ margin: 0 }}>{report.unknown.note}</p>
              </div>
            )}
          </Card>

          <Card title="校准限制与声明">
            <div className="stack small">
              <div className="errorbox" style={{ background: 'var(--warning-soft)', color: 'var(--warning)' }}>
                {report.disclaimer}
              </div>
              <ul style={{ margin: 0, paddingLeft: 16 }}>
                {report.limitations.map((l) => <li key={l}>{l}</li>)}
              </ul>
            </div>
          </Card>
        </>
      )}
        </div>
      </div>

      {detailCase && (
        <div className="drawer-backdrop" onClick={() => setDetailCase(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <span className="mono small">{detailCase.case_id}</span>
              <button className="btn" onClick={() => setDetailCase(null)} aria-label="关闭">✕</button>
            </div>
            <div className="drawer-body stack small">
              <div className="row" style={{ flexWrap: 'wrap', gap: 8 }}>
                <span className="badge muted">{detailCase.category}</span>
                <span className="badge muted">Rubric {detailCase.rubric_id}</span>
                <span className="badge blue">参考 {detailCase.human_score ?? 'UNKNOWN'}</span>
                <span className="badge warning">Judge {detailCase.judge_score ?? 'UNKNOWN'}</span>
                {detailCase.delta !== null && (
                  <span className="badge muted">Δ {detailCase.delta > 0 ? '+' : ''}{detailCase.delta}</span>
                )}
              </div>
              <div><strong>标注理由</strong><div>{detailCase.annotation_rationale}</div></div>
              {detailCase.repeat_scores.length > 1 && (
                <div>
                  <strong>重复评分</strong>
                  <div className="mono">{JSON.stringify(detailCase.repeat_scores)}</div>
                </div>
              )}
              {detailCase.unknown_dimensions.length > 0 && (
                <div><strong>UNKNOWN 维度</strong><div>{detailCase.unknown_dimensions.join(', ')}</div></div>
              )}
              <div><strong>逐维度得分</strong>
                <table className="tbl">
                  <tbody>
                    {Object.entries(detailCase.dimension_scores).map(([k, v]) => (
                      <tr key={k}>
                        <td className="small muted">{k}</td>
                        <td className="mono">{v === null ? 'UNKNOWN' : v}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div><strong>Judge 引用证据</strong>
                <div className="stack">
                  {Object.entries(detailCase.evidence).map(([dim, evs]) => (
                    <div key={dim} className="ev-box">
                      <div className="ev-title">{dim}</div>
                      <ul style={{ margin: '2px 0 0 16px' }}>
                        {evs.map((e, i) => <li key={i}>{e}</li>)}
                      </ul>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function BiasTable({
  items,
  cases,
  onOpen,
}: {
  items: BiasItem[]
  cases: CaseRow[]
  onOpen: (c: CaseRow | null) => void
}) {
  if (items.length === 0) return <EmptyState text="该方向无案例" />
  return (
    <table className="tbl">
      <thead>
        <tr><th>案例</th><th>类别</th><th>参考</th><th>Judge</th><th>Δ</th><th>标注理由</th><th></th></tr>
      </thead>
      <tbody>
        {items.map((x) => (
          <tr key={x.case_id}>
            <td className="mono small">{x.case_id}</td>
            <td className="small">{x.category}</td>
            <td>{x.human_score}</td>
            <td>{x.judge_score}</td>
            <td><strong>{x.delta > 0 ? '+' : ''}{x.delta}</strong></td>
            <td className="small muted">{x.rationale.slice(0, 44)}…</td>
            <td>
              <button
                className="btn"
                style={{ padding: '2px 8px' }}
                onClick={() => onOpen(cases.find((c) => c.case_id === x.case_id) ?? null)}
              >
                证据
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

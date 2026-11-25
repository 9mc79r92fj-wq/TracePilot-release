import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChevronRight, Play, RefreshCw, X } from 'lucide-react'
import { api, apiErrorMessage } from '../api'
import { Card, EmptyState, ErrorBox, Spinner, StatusBadge } from '../components/ui'
import { LineageDrawer } from '../components/LineagePanel'
import {
  AttributionDrawer,
  BatchAttributionPanel,
} from '../components/AttributionPanel'
import { FailurePatternExplorer } from '../components/FailurePatternExplorer'
import { RootCauseMetricsPanel } from '../components/RootCauseMetricsPanel'

interface DatasetMeta {
  dataset_id: string
  dataset_version: string
  case_count: number
  data_source: string
  data_source_note?: string
}

interface Metric {
  metric_name: string
  metric_value: unknown
  evaluation_method: string
  evidence: string[]
  confidence_or_limitations: string
  status: string
}

interface JudgeDimensionScore {
  dimension: string
  status: string
  score: number | null
  evidence: string[]
  reason: string
  unknown_reason: string | null
}

interface JudgeBlock {
  rubric_id: string
  rubric_version: string
  prompt_version: string
  judge_engine_version: string
  provider_kind: string
  model_name: string
  is_mock: boolean
  simulated: boolean
  scores: JudgeDimensionScore[]
  overall: number | null
  unknown_dimensions: string[]
  limitations: string[]
  input_truncated: boolean
  latency_ms: number
  token_usage: { total_tokens: number } | null
  error?: string
  status?: string
}

interface JudgeBreakdown {
  enabled: boolean
  provider_kind: string
  model_name: string
  is_mock: boolean | null
  simulated: boolean | null
  rubric_id: string | null
  rubric_version: string | null
  prompt_version: string | null
  judge_engine_version: string | null
  judged_cases: number
  failed_cases: number
  denominator_definition: string
  average_overall: number | null
  dimensions: Record<string, { scored: number; unknown: number; avg: number | null }>
  note: string
}

interface CaseResult {
  case_id: string
  scenario_type: string
  data_source: string
  trace_id: string
  overall_status: string
  metrics: Record<string, Metric>
  is_bad_case: boolean
  judge?: JudgeBlock
}

interface DimensionStat {
  pass: number
  fail: number
  unknown: number
  not_applicable: number
  applicable_denominator: number
  pass_rate: number | null
}

interface Report {
  evaluation_run_id: string
  dataset_id: string
  dataset_version: string
  dataset_data_source: string
  total_cases: number
  executed_cases: number
  passed_cases: number
  failed_cases: number
  unknown_cases: number
  not_applicable_cases: number
  execution_error_cases: number
  not_executed_cases: number
  metrics: {
    task_success_rate: {
      value: number | null
      numerator: number
      denominator: number
      denominator_definition: string
    }
    tool_correctness_rate: { value: number | null; numerator: number; denominator: number }
    recovery_rate: { value: number | null; numerator: number; denominator: number }
    safety_pass_rate: { value: number | null; numerator: number; denominator: number }
    dimension_breakdown: Record<string, DimensionStat>
    judge_breakdown?: JudgeBreakdown | null
    average_duration_ms: number | null
    total_tokens: number | null
    token_note: string
  }
  bad_cases: { case_id: string; trace_id: string; failed_dimensions: string[]; unknown_dimensions: string[] }[]
  execution_errors: { case_id: string; error: string }[]
  case_results: CaseResult[]
  trace_ids: string[]
  environment: { model: { provider: string; simulated: boolean; paid_api_called: boolean }; tools: string[] }
  limitations: string[]
}

const DIMS: { key: string; label: string }[] = [
  { key: 'task_success', label: 'Task Success' },
  { key: 'tool_correctness', label: 'Tool Correctness' },
  { key: 'process_quality', label: 'Process Quality' },
  { key: 'recovery', label: 'Recovery' },
  { key: 'efficiency', label: 'Efficiency' },
  { key: 'safety', label: 'Safety' },
]

function pct(v: number | null): string {
  if (v === null || v === undefined) return 'N/A'
  return (v * 100).toFixed(1) + '%'
}

function MetricStatus({ status }: { status: string }) {
  const cls =
    status === 'PASS' ? 'success' : status === 'FAIL' ? 'failed' : status === 'UNKNOWN' ? 'warning' : 'muted'
  return <span className={'badge ' + cls}>{status}</span>
}

export default function EvaluationCenter() {
  const navigate = useNavigate()
  const [datasets, setDatasets] = useState<DatasetMeta[]>([])
  const [runOptions, setRunOptions] = useState<
    { run_id: string; status: string; dataset_id?: string }[]
  >([])
  const [dataset, setDataset] = useState<DatasetMeta | null>(null)
  const [maxCases, setMaxCases] = useState<number | ''>('')
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState<string | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [report, setReport] = useState<Report | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Drill-down 状态
  const [filter, setFilter] = useState<string>('all')
  const [drawerCase, setDrawerCase] = useState<CaseResult | null>(null)
  const [lineageCase, setLineageCase] = useState<CaseResult | null>(null)
  const [attributionCase, setAttributionCase] = useState<CaseResult | null>(null)
  const [drawerDim, setDrawerDim] = useState<string | null>(null)

  // LLM Judge 配置（默认 none：不启用）
  const [judgeMode, setJudgeMode] = useState<'none' | 'mock'>('none')
  const [rubrics, setRubrics] = useState<{ rubric_id: string; version: string; title: string }[]>([])
  const [judgeRubric, setJudgeRubric] = useState('answer_quality')
  const [judgeInfo, setJudgeInfo] = useState<{ real_judge_enabled: boolean } | null>(null)

  const loadDatasets = useCallback(async () => {
    setError(null)
    try {
      const r = await api.get<{ datasets: DatasetMeta[] }>('/api/evaluation/datasets')
      setDatasets(r.data.datasets)
      // 只在用户尚未选择数据集时设置默认值：异步响应（含 StrictMode 双请求）
      // 不得覆盖用户已经做出的选择。
      setDataset((prev) => prev ?? r.data.datasets[0] ?? null)
    } catch (e) {
      setError(apiErrorMessage(e))
    }
  }, [])

  const loadRuns = useCallback(async () => {
    try {
      const r = await api.get<{ runs: { run_id: string; status: string; dataset_id?: string }[] }>(
        '/api/evaluation/runs',
      )
      setRunOptions(r.data.runs.slice(0, 20))
    } catch {
      setRunOptions([])
    }
  }, [])

  useEffect(() => {
    void loadDatasets()
    void loadRuns()
  }, [loadDatasets, loadRuns])

  useEffect(() => {
    // 加载可用 Rubric 与 Judge 可用性（均为只读元信息）
    void (async () => {
      try {
        const [rb, info] = await Promise.all([
          api.get<{ rubrics: { rubric_id: string; version: string; title: string }[] }>(
            '/api/evaluation/judge/rubrics',
          ),
          api.get<{ real_judge_enabled: boolean }>('/api/evaluation/judge/info'),
        ])
        setRubrics(rb.data.rubrics)
        setJudgeInfo(info.data)
        if (rb.data.rubrics[0]) setJudgeRubric(rb.data.rubrics[0].rubric_id)
      } catch {
        // Judge 元信息不可用时不阻塞评测主流程
      }
    })()
  }, [])

  async function runEvaluation() {
    if (!dataset) return
    setBusy(true)
    setError(null)
    setReport(null)
    setRunId(null)
    setDrawerCase(null)
    setDrawerDim(null)
    try {
      const payload: Record<string, unknown> = { dataset_id: dataset.dataset_id }
      if (maxCases !== '' && Number(maxCases) > 0) payload.max_cases = Number(maxCases)
      if (judgeMode !== 'none') {
        payload.judge_mode = judgeMode
        payload.judge_rubric_id = judgeRubric
      }
      const created = await api.post<{ run_id: string; status: string }>('/api/evaluation/runs', payload)
      const id = created.data.run_id
      setRunId(id)

      for (let i = 0; i < 600; i++) {
        const st = await api.get<{ status: string; error?: string; report: Report | null }>(
          `/api/evaluation/runs/${id}`,
        )
        setProgress(st.data.status)
        if (st.data.status === 'succeeded') {
          setReport(st.data.report)
          break
        }
        if (st.data.status === 'failed') {
          setError(st.data.error ?? '评测执行失败')
          break
        }
        await new Promise((r) => setTimeout(r, 250))
      }
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  const filteredCases = useMemo(() => {
    if (!report) return []
    if (filter === 'all') return report.case_results
    if (filter === 'bad') return report.case_results.filter((c) => c.is_bad_case)
    if (filter === 'unknown') return report.case_results.filter((c) => c.overall_status === 'UNKNOWN')
    if (filter === 'pass') return report.case_results.filter((c) => c.overall_status === 'PASS')
    return report.case_results.filter((c) => c.metrics[filter]?.status === 'FAIL')
  }, [report, filter])

  const drillCasesByDim = useMemo(() => {
    if (!report || !drawerDim) return []
    return report.case_results.filter((c) => c.metrics[drawerDim]?.status === 'FAIL')
  }, [report, drawerDim])

  return (
    <div className="stack">
      <div className="row">
        <h2 style={{ margin: 0 }}>Evaluation Center</h2>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => void loadDatasets()}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>
      <p className="muted small" style={{ margin: 0 }}>
        对 Agent 执行轨迹做六维度确定性评测。案例为合成数据，使用模拟模型与本地白名单工具，不调用付费 API。
      </p>

      <div className="grid grid-2">
      <Card title="Judge Calibration（评测器校准）">
        <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 10 }}>
          <span className="muted small">
            用参考标注检验 LLM Judge 的评分一致性（Exact / MAE / Within-1 / 混淆矩阵 / 稳定性）；标注来源为 synthetic_reference。
          </span>
          <button className="btn primary" onClick={() => navigate('/calibration')}>
            进入 Judge Calibration
          </button>
        </div>
      </Card>

      <Card title="Multi-Judge Arbitration（多评测器仲裁）">
        <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 10 }}>
          <span className="muted small">
            多个 Judge 独立评分后做分歧检测与 Median/Mean 仲裁；严重分歧返回 INCONCLUSIVE 并建议人工复核。全部为 Mock。
          </span>
          <button className="btn primary" onClick={() => navigate('/multi-judge')}>
            进入 Disagreement Explorer
          </button>
        </div>
      </Card>
      </div>

      {error && <ErrorBox message={error} />}

      <Card title="评测配置">
        <div className="stack">
          <div className="row" style={{ flexWrap: 'wrap' }}>
            <label className="small muted" htmlFor="ds">评测数据集</label>
            <select
              id="ds"
              value={dataset?.dataset_id ?? ''}
              onChange={(e) => setDataset(datasets.find((d) => d.dataset_id === e.target.value) ?? null)}
            >
              {datasets.map((d) => (
                <option key={d.dataset_id} value={d.dataset_id}>
                  {d.dataset_id} · v{d.dataset_version} · {d.case_count} 案例
                </option>
              ))}
            </select>

            <label className="small muted" htmlFor="mc">案例上限</label>
            <input
              id="mc"
              type="number"
              min={1}
              max={50}
              placeholder="全部"
              value={maxCases}
              onChange={(e) => setMaxCases(e.target.value === '' ? '' : Number(e.target.value))}
              style={{ width: 90 }}
            />

            <label className="small muted" htmlFor="jm">LLM Judge</label>
            <select
              id="jm"
              value={judgeMode}
              onChange={(e) => setJudgeMode(e.target.value as 'none' | 'mock')}
            >
              <option value="none">不启用（仅确定性验证器）</option>
              <option value="mock">Mock Judge（规则化模拟，零成本）</option>
            </select>

            {judgeMode === 'mock' && rubrics.length > 0 && (
              <>
                <label className="small muted" htmlFor="jr">Rubric</label>
                <select id="jr" value={judgeRubric} onChange={(e) => setJudgeRubric(e.target.value)}>
                  {rubrics.map((r) => (
                    <option key={r.rubric_id} value={r.rubric_id}>
                      {r.rubric_id} · v{r.version}
                    </option>
                  ))}
                </select>
                <span className="badge warning">模拟评审</span>
              </>
            )}
            {judgeInfo?.real_judge_enabled && (
              <span className="badge muted">真实 Judge 已授权（界面不开放）</span>
            )}

            <button className="btn primary" onClick={() => void runEvaluation()} disabled={busy || !dataset}>
              <Play size={15} /> 运行评测
            </button>
            {busy && <span className="muted small">执行中… {progress ?? ''}</span>}
            {runId && <span className="mono small muted">{runId}</span>}
          </div>

          {dataset && (
            <div className="cf-info">
              <div><span className="muted">数据集</span> {dataset.dataset_id}</div>
              <div><span className="muted">版本</span> {dataset.dataset_version}</div>
              <div><span className="muted">案例数</span> {dataset.case_count}</div>
              <div>
                <span className="muted">数据来源</span> <span className="badge muted">{dataset.data_source}</span>
              </div>
              {dataset.data_source_note && <div className="muted small">{dataset.data_source_note}</div>}
            </div>
          )}
        </div>
      </Card>

      {busy && <Spinner />}

      {/* 仅在评测报告就绪后分析：否则后端会因运行中而拒绝，且不会自动重试 */}
      {runId && report && (
        <Card title="失败模式挖掘（Failure Pattern Mining）">
          <FailurePatternExplorer runId={runId} runOptions={runOptions} />
        </Card>
      )}

      {runId && (
        <Card title="自动归因（Automated Bad Case Attribution）">
          <BatchAttributionPanel runId={runId} onOpenCase={(cid) => setDrawerCase(
            report?.case_results.find((x) => x.case_id === cid) ?? null,
          )} />
        </Card>
      )}

      {runId && (
        <Card title="根因准确率评估（Root-Cause Accuracy Metrics）">
          <RootCauseMetricsPanel runId={runId} />
        </Card>
      )}

      {report && (
        <>
          <div className="grid stats">
            <div className="card stat">
              <div className="k">任务成功率</div>
              <div className="v blue">{pct(report.metrics.task_success_rate.value)}</div>
              <div className="muted small">
                {report.metrics.task_success_rate.numerator} / {report.metrics.task_success_rate.denominator} 已执行
              </div>
            </div>
            <div className="card stat">
              <div className="k">工具正确率</div>
              <div className="v">{pct(report.metrics.tool_correctness_rate.value)}</div>
              <div className="muted small">
                {report.metrics.tool_correctness_rate.numerator} / {report.metrics.tool_correctness_rate.denominator}
              </div>
            </div>
            <div className="card stat">
              <div className="k">故障恢复率</div>
              <div className="v">{pct(report.metrics.recovery_rate.value)}</div>
              <div className="muted small">
                {report.metrics.recovery_rate.numerator} / {report.metrics.recovery_rate.denominator}
              </div>
            </div>
            <div className="card stat">
              <div className="k">平均耗时</div>
              <div className="v">
                {report.metrics.average_duration_ms === null ? 'N/A' : `${report.metrics.average_duration_ms.toFixed(1)}ms`}
              </div>
              <div className="muted small">墙钟时间均值</div>
            </div>
          </div>

          <Card title="运行概览">
            <div className="row" style={{ gap: 18, flexWrap: 'wrap' }}>
              <span className="small">总案例 <strong>{report.total_cases}</strong></span>
              <span className="small">已执行 <strong>{report.executed_cases}</strong></span>
              <span className="badge success">通过 {report.passed_cases}</span>
              <span className="badge failed">失败 {report.failed_cases}</span>
              <span className="badge warning">未知 {report.unknown_cases}</span>
              <span className="badge muted">不适用 {report.not_applicable_cases}</span>
              <span className="badge muted">执行异常 {report.execution_error_cases}</span>
              <span className="badge muted">未执行 {report.not_executed_cases}</span>
              <span className="muted small">{report.metrics.token_note}（总计 {report.metrics.total_tokens ?? 'N/A'}）</span>
            </div>
          </Card>

          <Card title="六维评测指标（点击 FAIL 数可下钻到案例）">
            <table className="tbl">
              <thead>
                <tr>
                  <th>维度</th><th>PASS</th><th>FAIL</th><th>UNKNOWN</th><th>N/A</th><th>可判定分母</th><th>通过率</th>
                </tr>
              </thead>
              <tbody>
                {DIMS.map((d) => {
                  const st = report.metrics.dimension_breakdown[d.key]
                  if (!st) return null
                  return (
                    <tr key={d.key}>
                      <td className="small">{d.label}</td>
                      <td>{st.pass}</td>
                      <td>
                        {st.fail > 0 ? (
                          <button
                            className="btn"
                            style={{ padding: '2px 8px' }}
                            onClick={() => setDrawerDim(d.key)}
                          >
                            {st.fail}
                          </button>
                        ) : 0}
                      </td>
                      <td>{st.unknown}</td>
                      <td>{st.not_applicable}</td>
                      <td className="muted small">{st.applicable_denominator}</td>
                      <td>{pct(st.pass_rate)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </Card>

          <Card
            title="案例结果 / Bad Case"
            action={
              <div className="row" style={{ gap: 6 }}>
                {[
                  { k: 'all', l: '全部' },
                  { k: 'bad', l: 'Bad Case' },
                  { k: 'unknown', l: 'UNKNOWN' },
                  { k: 'pass', l: 'PASS' },
                ].map((f) => (
                  <button
                    key={f.k}
                    className={'btn' + (filter === f.k ? ' primary' : '')}
                    style={{ padding: '4px 10px' }}
                    onClick={() => setFilter(f.k)}
                  >
                    {f.l}
                  </button>
                ))}
              </div>
            }
          >
            {filteredCases.length === 0 ? (
              <EmptyState text="该筛选条件下无案例" />
            ) : (
              <table className="tbl">
                <thead>
                  <tr><th>案例</th><th>场景</th><th>整体</th><th>失败维度</th><th>Trace</th><th></th></tr>
                </thead>
                <tbody>
                  {filteredCases.map((c) => (
                    <tr key={c.case_id}>
                      <td className="mono small">{c.case_id}</td>
                      <td className="small">{c.scenario_type}</td>
                      <td><StatusBadge status={c.overall_status === 'PASS' ? 'succeeded' : c.overall_status === 'FAIL' ? 'failed' : 'partial'} /></td>
                      <td className="small">
                        {Object.entries(c.metrics).filter(([, m]) => m.status === 'FAIL').map(([k]) => DIMS.find((d) => d.key === k)?.label ?? k).join(', ') || '—'}
                      </td>
                      <td className="mono small">{c.trace_id.slice(0, 22)}…</td>
                      <td>
                        <button className="btn" style={{ padding: '2px 8px' }} onClick={() => setDrawerCase(c)}>
                          证据 <ChevronRight size={13} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>

          {report.metrics.judge_breakdown?.enabled && (
            <Card
              title={
                'LLM Judge 评审（Rubric ' +
                (report.metrics.judge_breakdown.rubric_id ?? '-') +
                ' v' +
                (report.metrics.judge_breakdown.rubric_version ?? '-') +
                '）'
              }
            >
              <div className="stack small">
                <div className="row" style={{ flexWrap: 'wrap', gap: 10 }}>
                  <span className={'badge ' + (report.metrics.judge_breakdown.is_mock ? 'warning' : 'blue')}>
                    {report.metrics.judge_breakdown.is_mock ? 'MOCK（模拟评审）' : 'REAL（真实模型）'}
                  </span>
                  <span className="badge muted">{report.metrics.judge_breakdown.provider_kind}</span>
                  <span className="mono small">{report.metrics.judge_breakdown.model_name}</span>
                  <span className="muted">
                    Prompt {report.metrics.judge_breakdown.prompt_version} · Engine{' '}
                    {report.metrics.judge_breakdown.judge_engine_version}
                  </span>
                </div>

                <div className="row" style={{ gap: 18, flexWrap: 'wrap' }}>
                  <span>已评审案例 <strong>{report.metrics.judge_breakdown.judged_cases}</strong></span>
                  <span>失败案例 <strong>{report.metrics.judge_breakdown.failed_cases}</strong></span>
                  <span>
                    平均总分{' '}
                    <strong>
                      {report.metrics.judge_breakdown.average_overall === null
                        ? 'N/A'
                        : report.metrics.judge_breakdown.average_overall}
                    </strong>{' '}
                    <span className="muted">/ 5</span>
                  </span>
                </div>

                <table className="tbl">
                  <thead>
                    <tr><th>Judge 维度</th><th>已评分</th><th>UNKNOWN</th><th>均分（1–5）</th></tr>
                  </thead>
                  <tbody>
                    {Object.entries(report.metrics.judge_breakdown.dimensions).map(([dim, st]) => (
                      <tr key={dim}>
                        <td className="small">{dim}</td>
                        <td>{st.scored}</td>
                        <td>{st.unknown}</td>
                        <td>{st.avg === null ? 'N/A' : st.avg}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>

                <p className="muted" style={{ margin: 0 }}>{report.metrics.judge_breakdown.note}</p>
              </div>
            </Card>
          )}

          <Card title="评测环境与限制">
            <div className="stack small">
              <div>
                模型：{report.environment.model.provider} · 模拟：{report.environment.model.simulated ? '是' : '否'} ·
                付费 API：{report.environment.model.paid_api_called ? '是' : '否'}
              </div>
              <div>工具白名单：{report.environment.tools.join(', ')}</div>
              <div className="muted">数据来源：{report.dataset_data_source}（非真实用户数据）</div>
              <div>
                <strong>限制说明</strong>
                <ul style={{ margin: '4px 0 0 16px' }}>
                  {report.limitations.map((l) => <li key={l}>{l}</li>)}
                </ul>
              </div>
              {report.execution_errors.length > 0 && (
                <div>
                  <strong>执行异常</strong>
                  <ul style={{ margin: '4px 0 0 16px' }}>
                    {report.execution_errors.map((e) => <li key={e.case_id}>{e.case_id}: {e.error}</li>)}
                  </ul>
                </div>
              )}
            </div>
          </Card>
        </>
      )}

      {!busy && !report && !error && <EmptyState text="选择数据集后点击「运行评测」" />}

      {/* 维度下钻抽屉 */}
      {drawerDim && report && (
        <div className="drawer-backdrop" onClick={() => setDrawerDim(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <span>维度下钻 · {DIMS.find((d) => d.key === drawerDim)?.label}</span>
              <button className="btn" onClick={() => setDrawerDim(null)} aria-label="关闭"><X size={15} /></button>
            </div>
            <div className="drawer-body">
              <p className="muted small">该维度 FAIL 的案例（{drillCasesByDim.length}）</p>
              {drillCasesByDim.length === 0 ? (
                <EmptyState text="该维度无失败案例" />
              ) : (
                <table className="tbl">
                  <thead><tr><th>案例</th><th>证据</th><th></th></tr></thead>
                  <tbody>
                    {drillCasesByDim.map((c) => (
                      <tr key={c.case_id}>
                        <td className="mono small">{c.case_id}</td>
                        <td className="small muted">{c.metrics[drawerDim].evidence.join('; ').slice(0, 60)}</td>
                        <td>
                          <button className="btn" style={{ padding: '2px 8px' }} onClick={() => { setDrawerCase(c); setDrawerDim(null) }}>
                            打开
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </div>
      )}

      {/* 案例证据抽屉 */}
      {drawerCase && (
        <div className="drawer-backdrop" onClick={() => setDrawerCase(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <span className="mono small">{drawerCase.case_id}</span>
              <button className="btn" onClick={() => setDrawerCase(null)} aria-label="关闭"><X size={15} /></button>
            </div>
            <div className="drawer-body stack small">
              <div className="row">
                <span className="badge muted">{drawerCase.scenario_type}</span>
                <span className="badge muted">{drawerCase.data_source}</span>
                <span className="mono small">{drawerCase.trace_id}</span>
              </div>

              <div className="row">
                <button className="btn" onClick={() => navigate('/traces/' + drawerCase.trace_id)}>
                  查看 Trace
                </button>
                <button className="btn" onClick={() => navigate('/dag/' + drawerCase.trace_id)}>
                  查看 DAG
                </button>
                <button
                  className="btn"
                  onClick={() => {
                    setLineageCase(drawerCase)
                    setDrawerCase(null)
                  }}
                >
                  查看证据血缘
                </button>
                {drawerCase.is_bad_case && (
                  <button
                    className="btn primary"
                    onClick={() => {
                      setAttributionCase(drawerCase)
                      setDrawerCase(null)
                    }}
                  >
                    自动归因
                  </button>
                )}
              </div>

              {/* LLM Judge 明细（与确定性指标分区展示，不可互相替代） */}
              {drawerCase.judge && (
                <div className="cf-note">
                  <strong>LLM Judge 评分</strong>
                  {drawerCase.judge.error ? (
                    <div className="errorbox" style={{ marginTop: 6 }}>
                      Judge 执行失败：{drawerCase.judge.error}
                    </div>
                  ) : (
                    <>
                      <div className="row" style={{ gap: 8, marginTop: 6, flexWrap: 'wrap' }}>
                        <span className={'badge ' + (drawerCase.judge.is_mock ? 'warning' : 'blue')}>
                          {drawerCase.judge.is_mock ? 'MOCK 模拟' : 'REAL 真实'}
                        </span>
                        <span className="mono small">{drawerCase.judge.model_name}</span>
                        <span className="badge muted">Rubric {drawerCase.judge.rubric_id} v{drawerCase.judge.rubric_version}</span>
                        <span className="badge muted">Prompt {drawerCase.judge.prompt_version}</span>
                        <span className="muted small">
                          总分 {drawerCase.judge.overall === null ? 'N/A' : drawerCase.judge.overall} / 5
                        </span>
                        <span className="muted small">延迟 {drawerCase.judge.latency_ms}ms</span>
                      </div>
                      {drawerCase.judge.input_truncated && (
                        <div className="muted small" style={{ marginTop: 4 }}>
                          输入过长已截断，评分仅基于截断后的材料
                        </div>
                      )}
                      <div className="stack" style={{ marginTop: 8 }}>
                        {drawerCase.judge.scores.map((s) => (
                          <div
                            key={s.dimension}
                            className="ev-box"
                            style={{
                              borderLeftColor:
                                s.status === 'UNKNOWN'
                                  ? 'var(--warning)'
                                  : (s.score ?? 0) >= 4
                                    ? 'var(--success)'
                                    : 'var(--danger)',
                            }}
                          >
                            <div className="row" style={{ justifyContent: 'space-between' }}>
                              <span className="ev-title">{s.dimension}</span>
                              <span className={'badge ' + (s.status === 'UNKNOWN' ? 'warning' : 'muted')}>
                                {s.status === 'UNKNOWN' ? 'UNKNOWN' : `${s.score} / 5`}
                              </span>
                            </div>
                            {s.reason && <div style={{ marginTop: 4 }}>{s.reason}</div>}
                            {s.unknown_reason && (
                              <div className="muted" style={{ marginTop: 4 }}>
                                UNKNOWN 原因：{s.unknown_reason}
                              </div>
                            )}
                            {s.evidence.length > 0 && (
                              <div style={{ marginTop: 4 }}>
                                <span className="muted">证据：</span>
                                <ul style={{ margin: '2px 0 0 16px' }}>
                                  {s.evidence.map((e, i) => <li key={i}>{e}</li>)}
                                </ul>
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                      <div className="muted small" style={{ marginTop: 6 }}>
                        <div><strong>评测限制</strong></div>
                        <ul style={{ margin: '2px 0 0 16px' }}>
                          {drawerCase.judge.limitations.map((l) => <li key={l}>{l}</li>)}
                        </ul>
                      </div>
                    </>
                  )}
                </div>
              )}

              {DIMS.map((d) => {
                const m = drawerCase.metrics[d.key]
                if (!m) return null
                return (
                  <div key={d.key} className="ev-box" style={{ borderLeftColor: m.status === 'FAIL' ? 'var(--danger)' : m.status === 'PASS' ? 'var(--success)' : 'var(--border)' }}>
                    <div className="row" style={{ justifyContent: 'space-between' }}>
                      <span className="ev-title">{d.label}</span>
                      <MetricStatus status={m.status} />
                    </div>
                    <div style={{ marginTop: 4 }}>
                      <div><span className="muted">方法：</span>{m.evaluation_method}</div>
                      <div><span className="muted">值：</span><span className="mono">{JSON.stringify(m.metric_value)}</span></div>
                      <div><span className="muted">证据：</span>
                        <ul style={{ margin: '2px 0 0 16px' }}>
                          {m.evidence.map((e, i) => <li key={i}>{e}</li>)}
                        </ul>
                      </div>
                      <div className="muted">限制：{m.confidence_or_limitations}</div>
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      )}

      {lineageCase && runId && (
        <LineageDrawer
          kind="evaluation_case"
          ids={{ runId, caseId: lineageCase.case_id }}
          onClose={() => setLineageCase(null)}
        />
      )}

      {attributionCase && runId && (
        <AttributionDrawer
          runId={runId}
          caseId={attributionCase.case_id}
          onClose={() => setAttributionCase(null)}
        />
      )}
    </div>
  )
}

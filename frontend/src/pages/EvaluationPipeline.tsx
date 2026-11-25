import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { AlertTriangle, Ban, ListChecks, Play, RefreshCw } from 'lucide-react'
import { api, apiErrorMessage } from '../api'
import { Card, EmptyState, ErrorBox, Spinner } from '../components/ui'

/**
 * 第 52 步：Automated Evaluation Pipeline 前端（Pipeline Run Inspector）。
 *
 * 布局：左侧流水线配置 / 中部可交互阶段流程图 / 右侧阶段详情与运行日志 /
 * 底部最终报告与证据入口；支持 PC、平板、手机。
 * 所有数据来自后端真实流水线记录；未完成的流水线不显示伪造结论。
 */

interface StageMeta {
  stage_id: string
  stage_name: string
  required: boolean
  depends_on: string[]
}

interface PipelineMeta {
  stages: StageMeta[]
  stage_statuses: string[]
  pipeline_statuses: string[]
  execution_modes: string[]
  judge_modes: string[]
  limits: {
    max_cases: number
    max_concurrent_pipelines: number
    max_queue_length: number
    max_runtime_seconds: number
    max_retries: number
    max_label_length: number
  }
  real_api_note: string
  mock_note: string
}

interface DatasetMeta {
  dataset_id: string
  dataset_version: string
  case_count: number
  data_source: string
}

interface RubricMeta {
  rubric_id: string
  version: string
  title: string
}

interface VersionDescriptor {
  evaluation_version_id: string
  evaluation_run_id: string
  dataset_id: string | null
  dataset_version: string | null
  evaluator_version: string | null
}

interface StageState {
  stage_id: string
  stage_name: string
  required: boolean
  status: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  input_refs: string[]
  output_refs: string[]
  error_details: { type?: string; message?: string; attempt?: number } | null
  skip_reason: string | null
  attempts: number
  max_retries: number
}

interface PipelineRefs {
  evaluation_run_id: string | null
  baseline_run_id: string | null
  agent_run_ids: string[]
  trace_ids: string[]
  bad_case_ids: string[]
  attribution_report_ids: string[]
  failure_pattern_report_id: string | null
  comparison_id: string | null
  regression_report_id: string | null
  quality_gate_report_id: string | null
}

interface Repro {
  pipeline_run_id: string
  dataset_version: string | null
  agent_version: string | null
  prompt_version: string | null
  evaluator_version: string | null
  rubric_version: string | null
  execution_mode: string
  random_seed: number
  config_fingerprint: string
  created_at: string
  notes?: Record<string, string>
}

interface GateSummary {
  gate_status: string | null
  exit_code: number | null
  gate_report_id: string | null
  reason?: string
  compared?: boolean
  regression_gate_status?: string | null
  triggered_rules?: string[]
}

interface EvaluationSummary {
  evaluation_run_id: string
  dataset_id: string
  dataset_version: string
  total_cases: number
  executed_cases: number
  passed_cases: number
  failed_cases: number
  unknown_cases: number
  metrics: Record<string, { value: number | null; numerator: number | null; denominator: number | null }>
  bad_case_count: number
  bad_cases: { case_id: string; trace_id: string; failed_dimensions?: string[] }[]
  cases: { case_id: string; overall_status: string; trace_id: string; is_bad_case: boolean }[]
}

interface PipelineReport {
  pipeline_run_id: string
  status: string
  config: Record<string, unknown>
  reproducibility: Repro
  stage_summary: {
    total: number
    counts: Record<string, number>
    succeeded: string[]
    failed: string[]
    skipped: string[]
    blocked: string[]
    cancelled: string[]
  }
  stages: StageState[]
  refs: PipelineRefs
  evaluation: EvaluationSummary | null
  attribution: { attributed_count?: number; bad_case_count?: number; reports?: { attribution_id: string; case_id: string; root_cause_status: string }[] } | null
  failure_patterns: { pattern_report_id: string; pattern_count: number; patterns?: { fingerprint_id: string; fingerprint_label: string; case_count: number }[] } | null
  version_comparison: {
    comparison_id: string
    comparability: { verdict?: string }
    case_counts?: Record<string, number>
    regressed_cases?: string[]
    metric_deltas?: { metric: string; baseline: number | null; candidate: number | null; delta: number | null; status: string }[]
  } | null
  regression: { regression_report_id: string; risk_level: string; gate_status: string; triggered_rules: string[] } | null
  quality_gate: GateSummary & { gate_reason?: string; checks?: { check_id: string; status: string; message: string }[] } | null
  evidence_refs: { kind: string; ref_id: string; detail?: string }[]
  error: { type?: string; stage_id?: string; message?: string } | null
  limitations: string[]
  paid_api_called: boolean
  generated_at: string
}

interface PipelineRecord {
  pipeline_run_id: string
  status: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  config: Record<string, unknown>
  reproducibility: Repro
  stages: StageState[]
  refs: PipelineRefs
  gate_summary: GateSummary | null
  final_report: PipelineReport | null
  final_report_paths: Record<string, string> | null
  error: { type?: string; stage_id?: string; message?: string } | null
  cancel_requested?: boolean
  paid_api_called: boolean
  limitations: string[]
}

interface PipelineSummary {
  pipeline_run_id: string
  status: string
  created_at: string
  duration_ms: number | null
  dataset_id: string | null
  dataset_version: string | null
  execution_mode: string | null
  label: string | null
  succeeded_stages: number
  skipped_stages: number
  failed_stages: number
  quality_gate_status: string | null
}

const TERMINAL = ['SUCCEEDED', 'FAILED', 'BLOCKED', 'CANCELLED']

function statusTone(status: string): string {
  switch (status) {
    case 'SUCCEEDED':
      return 'success'
    case 'FAILED':
      return 'failed'
    case 'BLOCKED':
      return 'failed'
    case 'CANCELLED':
      return 'muted'
    case 'SKIPPED':
      return 'partial'
    case 'RUNNING':
      return 'blue'
    case 'PENDING':
      return 'muted'
    default:
      return 'muted'
  }
}

function gateTone(status: string | null | undefined): string {
  switch (status) {
    case 'PASS':
      return 'success'
    case 'WARN':
      return 'partial'
    case 'BLOCK':
      return 'failed'
    case 'INCONCLUSIVE':
      return 'muted'
    default:
      return 'muted'
  }
}

function riskTone(level: string): string {
  switch (level) {
    case 'LOW':
      return 'success'
    case 'MEDIUM':
      return 'partial'
    case 'HIGH':
    case 'CRITICAL':
      return 'failed'
    default:
      return 'muted'
  }
}

function fmtDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(2)} s`
}

function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(1)}%`
}

export default function EvaluationPipeline() {
  const navigate = useNavigate()
  const [meta, setMeta] = useState<PipelineMeta | null>(null)
  const [datasets, setDatasets] = useState<DatasetMeta[]>([])
  const [rubrics, setRubrics] = useState<RubricMeta[]>([])
  const [runs, setRuns] = useState<VersionDescriptor[]>([])
  const [history, setHistory] = useState<PipelineSummary[]>([])
  const [pipeline, setPipeline] = useState<PipelineRecord | null>(null)
  const [selectedStage, setSelectedStage] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 表单
  const [datasetId, setDatasetId] = useState('')
  const [maxCases, setMaxCases] = useState('')
  const [executionMode, setExecutionMode] = useState<'mock' | 'real_api'>('mock')
  const [confirmReal, setConfirmReal] = useState(false)
  const [judgeMode, setJudgeMode] = useState<'none' | 'mock'>('none')
  const [rubricId, setRubricId] = useState('answer_quality')
  const [baselineRun, setBaselineRun] = useState('')
  const [enableAttribution, setEnableAttribution] = useState(true)
  const [enablePatterns, setEnablePatterns] = useState(true)
  const [enableComparison, setEnableComparison] = useState(true)
  const [enableRegression, setEnableRegression] = useState(true)
  const [enableGate, setEnableGate] = useState(true)
  const [seed, setSeed] = useState('42')
  const [runtime, setRuntime] = useState('')
  const [retries, setRetries] = useState('0')
  const [label, setLabel] = useState('')

  const pollRef = useRef<number | null>(null)

  const loadHistory = useCallback(async () => {
    try {
      const r = await api.get<{ count: number; pipelines: PipelineSummary[] }>(
        '/api/pipelines',
        { params: { limit: 20 } },
      )
      setHistory(r.data.pipelines)
    } catch {
      setHistory([])
    }
  }, [])

  const loadAll = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [metaResp, dsResp, rubricResp, runResp] = await Promise.all([
        api.get<PipelineMeta>('/api/pipelines/meta'),
        api.get<{ datasets: DatasetMeta[] }>('/api/evaluation/datasets'),
        api.get<{ rubrics: RubricMeta[] }>('/api/evaluation/judge/rubrics'),
        api.get<{ count: number; runs: VersionDescriptor[] }>('/api/version-comparison/runs', {
          params: { limit: 30 },
        }),
      ])
      setMeta(metaResp.data)
      setDatasets(dsResp.data.datasets)
      setRubrics(rubricResp.data.rubrics)
      setRuns(runResp.data.runs)
      if (dsResp.data.datasets[0]) {
        setDatasetId((prev) => prev || dsResp.data.datasets[0].dataset_id)
      }
      if (rubricResp.data.rubrics[0]) {
        setRubricId((prev) => prev || rubricResp.data.rubrics[0].rubric_id)
      }
      await loadHistory()
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setLoading(false)
    }
  }, [loadHistory])

  useEffect(() => {
    void loadAll()
  }, [loadAll])

  const refreshPipeline = useCallback(async (id: string) => {
    try {
      const r = await api.get<PipelineRecord>(`/api/pipelines/${id}`)
      setPipeline(r.data)
      setSelectedStage((prev) => prev ?? r.data.stages[0]?.stage_id ?? null)
      return r.data
    } catch (e) {
      setError(apiErrorMessage(e))
      return null
    }
  }, [])

  // 轮询运行中的流水线（页面刷新后仍可继续查询）
  useEffect(() => {
    if (!pipeline) return
    if (TERMINAL.includes(pipeline.status)) {
      if (pollRef.current) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
      return
    }
    if (pollRef.current) return
    pollRef.current = window.setInterval(() => {
      void refreshPipeline(pipeline.pipeline_run_id).then((record) => {
        if (record && TERMINAL.includes(record.status)) {
          if (pollRef.current) {
            window.clearInterval(pollRef.current)
            pollRef.current = null
          }
          void loadHistory()
        }
      })
    }, 1000)
    return () => {
      if (pollRef.current) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pipeline?.pipeline_run_id, pipeline?.status])

  async function createPipeline() {
    if (!datasetId) {
      setError('请选择评测数据集')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const payload: Record<string, unknown> = {
        dataset_id: datasetId,
        execution_mode: executionMode,
        confirm_real: executionMode === 'real_api' ? confirmReal : false,
        judge_mode: judgeMode,
        judge_rubric_id: rubricId,
        baseline_run_id: baselineRun || null,
        enable_attribution: enableAttribution,
        enable_failure_patterns: enablePatterns,
        enable_version_comparison: enableComparison,
        enable_regression: enableRegression,
        enable_quality_gate: enableGate,
        random_seed: Number(seed) || 0,
        max_retries: Number(retries) || 0,
      }
      if (maxCases.trim()) payload.max_cases = Number(maxCases)
      if (runtime.trim()) payload.max_runtime_seconds = Number(runtime)
      if (label.trim()) payload.label = label.trim()
      const r = await api.post<PipelineRecord>('/api/pipelines', payload)
      setPipeline(r.data)
      setSelectedStage(r.data.stages[0]?.stage_id ?? null)
      await loadHistory()
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  async function startPipeline() {
    if (!pipeline) return
    setBusy(true)
    setError(null)
    try {
      const r = await api.post<PipelineRecord>(`/api/pipelines/${pipeline.pipeline_run_id}/start`)
      setPipeline(r.data)
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  async function cancelPipeline() {
    if (!pipeline) return
    setBusy(true)
    setError(null)
    try {
      const r = await api.post<PipelineRecord>(`/api/pipelines/${pipeline.pipeline_run_id}/cancel`)
      setPipeline(r.data)
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  const stage = useMemo(
    () => pipeline?.stages.find((s) => s.stage_id === selectedStage) ?? null,
    [pipeline, selectedStage],
  )
  const terminalCount = useMemo(
    () => (pipeline ? pipeline.stages.filter((s) => TERMINAL.includes(s.status)).length : 0),
    [pipeline],
  )
  const report = pipeline?.final_report ?? null
  const canStart = pipeline?.status === 'PENDING' && !busy
  const canCancel = pipeline ? ['PENDING', 'RUNNING'].includes(pipeline.status) : false

  return (
    <div className="stack">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <h2 style={{ margin: 0 }}>Evaluation Pipeline · 自动化评测流水线</h2>
          <div className="muted small">
            数据集验证 → Agent 执行 → Trace 采集 → 评测 → 可选归因 / 失败模式 / 版本对比 / 回归 / 门禁 → 报告。
          </div>
        </div>
        <div className="row">
          <span className="badge blue">运行详情</span>
          <span className="badge muted">Mock 默认 · 真实 API 需双重授权</span>
        </div>
      </div>

      {error && <ErrorBox message={error} />}
      {loading && <Spinner />}

      {!loading && meta && (
        <>
          <div className="pl-layout">
            {/* 左：流水线配置 */}
            <Card title="流水线配置" className="pl-config">
              <div className="stack">
                <label className="pl-field">
                  <span>评测数据集</span>
                  <select
                    id="pl-ds"
                    value={datasetId}
                    onChange={(e) => setDatasetId(e.target.value)}
                  >
                    {datasets.map((d) => (
                      <option key={d.dataset_id} value={d.dataset_id}>
                        {d.dataset_id} · v{d.dataset_version} · {d.case_count} 案例
                      </option>
                    ))}
                  </select>
                </label>
                <div className="row pl-row2">
                  <label className="pl-field">
                    <span>案例上限</span>
                    <input
                      id="pl-mc"
                      type="number"
                      min={1}
                      max={meta.limits.max_cases}
                      placeholder={`1..${meta.limits.max_cases}`}
                      value={maxCases}
                      onChange={(e) => setMaxCases(e.target.value)}
                    />
                  </label>
                  <label className="pl-field">
                    <span>随机种子</span>
                    <input
                      id="pl-seed"
                      type="number"
                      min={0}
                      value={seed}
                      onChange={(e) => setSeed(e.target.value)}
                    />
                  </label>
                </div>
                <label className="pl-field">
                  <span>执行模式</span>
                  <select
                    id="pl-mode"
                    value={executionMode}
                    onChange={(e) => setExecutionMode(e.target.value as 'mock' | 'real_api')}
                  >
                    <option value="mock">Mock（受控确定性执行器，零成本）</option>
                    <option value="real_api">real_api（需服务端开启真实 API）</option>
                  </select>
                </label>
                {executionMode === 'real_api' && (
                  <label className="pl-check pl-warn">
                    <input
                      type="checkbox"
                      checked={confirmReal}
                      onChange={(e) => setConfirmReal(e.target.checked)}
                    />
                    我确认这会产生真实付费 API 调用（未获服务端授权时会被拒绝）
                  </label>
                )}
                <div className="row pl-row2">
                  <label className="pl-field">
                    <span>Judge 模式</span>
                    <select
                      id="pl-judge"
                      value={judgeMode}
                      onChange={(e) => setJudgeMode(e.target.value as 'none' | 'mock')}
                    >
                      <option value="none">不启用（仅确定性验证器）</option>
                      <option value="mock">Mock（规则化模拟，零成本）</option>
                    </select>
                  </label>
                  <label className="pl-field">
                    <span>Rubric</span>
                    <select
                      id="pl-rubric"
                      value={rubricId}
                      onChange={(e) => setRubricId(e.target.value)}
                      disabled={judgeMode === 'none'}
                    >
                      {rubrics.map((r) => (
                        <option key={r.rubric_id} value={r.rubric_id}>
                          {r.rubric_id} v{r.version}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                <label className="pl-field">
                  <span>基线版本（可选，用于版本对比/回归/门禁）</span>
                  <select
                    id="pl-baseline"
                    value={baselineRun}
                    onChange={(e) => setBaselineRun(e.target.value)}
                  >
                    <option value="">无基线（跳过对比/回归/门禁）</option>
                    {runs.map((r) => (
                      <option key={r.evaluation_run_id} value={r.evaluation_run_id}>
                        {r.evaluation_run_id.slice(0, 20)} · {r.dataset_id}@{r.dataset_version}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="pl-field">
                  <span>可选评测阶段</span>
                  <div className="pl-checks">
                    <label className="pl-check">
                      <input type="checkbox" checked={enableAttribution}
                        onChange={(e) => setEnableAttribution(e.target.checked)} />
                      Bad Case 归因
                    </label>
                    <label className="pl-check">
                      <input type="checkbox" checked={enablePatterns}
                        onChange={(e) => setEnablePatterns(e.target.checked)} />
                      失败模式挖掘
                    </label>
                    <label className="pl-check">
                      <input type="checkbox" checked={enableComparison}
                        onChange={(e) => setEnableComparison(e.target.checked)} />
                      版本对比
                    </label>
                    <label className="pl-check">
                      <input type="checkbox" checked={enableRegression}
                        onChange={(e) => setEnableRegression(e.target.checked)} />
                      回归检测
                    </label>
                    <label className="pl-check">
                      <input type="checkbox" checked={enableGate}
                        onChange={(e) => setEnableGate(e.target.checked)} />
                      质量门禁
                    </label>
                  </div>
                </div>
                <div className="row pl-row2">
                  <label className="pl-field">
                    <span>运行上限（秒）</span>
                    <input
                      id="pl-runtime"
                      type="number"
                      min={1}
                      max={meta.limits.max_runtime_seconds}
                      placeholder={`默认 120`}
                      value={runtime}
                      onChange={(e) => setRuntime(e.target.value)}
                    />
                  </label>
                  <label className="pl-field">
                    <span>最大重试</span>
                    <input
                      id="pl-retries"
                      type="number"
                      min={0}
                      max={meta.limits.max_retries}
                      value={retries}
                      onChange={(e) => setRetries(e.target.value)}
                    />
                  </label>
                </div>
                <label className="pl-field">
                  <span>标签（可选）</span>
                  <input
                    id="pl-label"
                    type="text"
                    maxLength={meta.limits.max_label_length}
                    placeholder="例如：发布前回归"
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                  />
                </label>
                <div className="row pl-actions">
                  <button className="btn primary" onClick={() => void createPipeline()} disabled={busy}>
                    <ListChecks size={15} /> 创建流水线
                  </button>
                  <button className="btn" onClick={() => void startPipeline()} disabled={!canStart}>
                    <Play size={15} /> 启动流水线
                  </button>
                  <button className="btn" onClick={() => void cancelPipeline()} disabled={!canCancel || busy}>
                    <Ban size={15} /> 取消
                  </button>
                </div>
                <div className="muted small">
                  限制：案例 ≤ {meta.limits.max_cases}、并发 ≤ {meta.limits.max_concurrent_pipelines}、
                  队列 ≤ {meta.limits.max_queue_length}、运行 ≤ {meta.limits.max_runtime_seconds}s、
                  重试 ≤ {meta.limits.max_retries}
                </div>
                <div className="muted small">{meta.mock_note}</div>
              </div>
            </Card>

            {/* 中：阶段流程图 + 进度 */}
            <Card
              title="阶段流程图"
              className="pl-center"
              action={
                pipeline ? (
                  <span className="row" style={{ gap: 6 }}>
                    <span className={`badge ${statusTone(pipeline.status)}`}>{pipeline.status}</span>
                    <span className="muted small">
                      {terminalCount}/{pipeline.stages.length} 阶段结束
                    </span>
                  </span>
                ) : null
              }
            >
              {!pipeline ? (
                <EmptyState text="在左侧配置并创建流水线后，这里显示可交互的阶段流程图。" />
              ) : (
                <>
                  <div className="pl-progress">
                    <div className="pl-progress-track">
                      <span
                        style={{
                          width: `${(terminalCount / Math.max(pipeline.stages.length, 1)) * 100}%`,
                        }}
                      />
                    </div>
                    <div className="muted small">
                      Pipeline {pipeline.pipeline_run_id} · 创建 {pipeline.created_at} ·
                      耗时 {fmtDuration(pipeline.duration_ms)}
                    </div>
                  </div>
                  <div className="pl-flow">
                    {pipeline.stages.map((s, index) => (
                      <div key={s.stage_id} className="pl-flow-item">
                        <button
                          type="button"
                          className={
                            'pl-stage' +
                            (selectedStage === s.stage_id ? ' active' : '') +
                            ' ' + statusTone(s.status)
                          }
                          onClick={() => setSelectedStage(s.stage_id)}
                          title={s.skip_reason || s.error_details?.message || s.stage_name}
                        >
                          <span className="pl-stage-name">{s.stage_name}</span>
                          <span className={`badge ${statusTone(s.status)}`}>{s.status}</span>
                          <span className="muted small">
                            {s.required ? '必选' : '可选'} · {fmtDuration(s.duration_ms)}
                          </span>
                        </button>
                        {index < pipeline.stages.length - 1 && (
                          <span className="pl-arrow" aria-hidden>
                            ↓
                          </span>
                        )}
                      </div>
                    ))}
                  </div>
                  {pipeline.error && (
                    <div className="pl-error">
                      <AlertTriangle size={15} /> 失败阶段：
                      {pipeline.error.stage_id ?? '—'} · {pipeline.error.type ?? ''} ·{' '}
                      {pipeline.error.message ?? ''}
                    </div>
                  )}
                  <div className="row mt">
                    <button className="btn" onClick={() => void refreshPipeline(pipeline.pipeline_run_id)}>
                      <RefreshCw size={14} /> 刷新状态
                    </button>
                  </div>
                </>
              )}
            </Card>

            {/* 右：阶段详情与运行日志 */}
            <Card title="阶段详情与日志" className="pl-detail-card">
              {!pipeline ? (
                <EmptyState text="选择阶段后查看详情。" />
              ) : !stage ? (
                <EmptyState text="点击中间任一阶段查看详情。" />
              ) : (
                <div className="pl-stage-detail">
                  <div className="row">
                    <strong>{stage.stage_name}</strong>
                    <span className={`badge ${statusTone(stage.status)}`}>{stage.status}</span>
                    <span className="muted small">{stage.stage_id}</span>
                  </div>
                  <dl className="pl-kv">
                    <dt>开始</dt>
                    <dd>{stage.started_at ?? '—'}</dd>
                    <dt>结束</dt>
                    <dd>{stage.finished_at ?? '—'}</dd>
                    <dt>耗时</dt>
                    <dd>{fmtDuration(stage.duration_ms)}</dd>
                    <dt>尝试</dt>
                    <dd>
                      {stage.attempts} / 最大重试 {stage.max_retries}
                    </dd>
                    <dt>输入引用</dt>
                    <dd className="mono small">{stage.input_refs.join(' · ') || '—'}</dd>
                    <dt>输出引用</dt>
                    <dd className="mono small">{stage.output_refs.join(' · ') || '—'}</dd>
                  </dl>
                  {stage.status === 'SKIPPED' && (
                    <div className="pl-skip">跳过原因：{stage.skip_reason ?? '未提供'}</div>
                  )}
                  {stage.error_details && (
                    <div className="pl-error">
                      <AlertTriangle size={14} /> {stage.error_details.type ?? 'ERROR'}：
                      {stage.error_details.message}
                    </div>
                  )}
                  <h4 className="pl-h4">运行日志</h4>
                  <ol className="pl-log">
                    {pipeline.stages.map((s) => (
                      <li key={s.stage_id} className={s.stage_id === stage.stage_id ? 'current' : ''}>
                        <span className={`badge ${statusTone(s.status)}`}>{s.status}</span>
                        <span className="mono small">{s.stage_id}</span>
                        <span className="muted small">
                          {s.status === 'SKIPPED' ? s.skip_reason : fmtDuration(s.duration_ms)}
                        </span>
                      </li>
                    ))}
                  </ol>
                </div>
              )}
            </Card>
          </div>

          {/* 底部：最终报告与证据入口 */}
          <div className="pl-bottom">
            {!pipeline ? (
              <Card title="最终报告">
                <EmptyState text="创建并启动流水线后，这里展示最终报告与证据入口。" />
              </Card>
            ) : !report ? (
              <Card title="最终报告">
                <EmptyState text="流水线仍在运行，报告尚未生成。" />
              </Card>
            ) : (
              <>
                <Card
                  title="最终报告"
                  action={
                    <span className="row" style={{ gap: 8 }}>
                      <span className={`badge ${statusTone(report.status)}`}>{report.status}</span>
                      <span className="badge muted">{report.reproducibility.execution_mode}</span>
                      <span className="badge muted">
                        config {report.reproducibility.config_fingerprint}
                      </span>
                    </span>
                  }
                >
                  <div className="pl-report-grid">
                    <div>
                      <h4 className="pl-h4">可复现性</h4>
                      <dl className="pl-kv">
                        <dt>pipeline_run_id</dt>
                        <dd className="mono small">{report.reproducibility.pipeline_run_id}</dd>
                        <dt>dataset_version</dt>
                        <dd>{report.reproducibility.dataset_version ?? '（未提供）'}</dd>
                        <dt>agent_version</dt>
                        <dd>{report.reproducibility.agent_version ?? '未提供'}</dd>
                        <dt>prompt_version</dt>
                        <dd>{report.reproducibility.prompt_version ?? '未提供'}</dd>
                        <dt>evaluator_version</dt>
                        <dd>{report.reproducibility.evaluator_version ?? '—'}</dd>
                        <dt>rubric_version</dt>
                        <dd>{report.reproducibility.rubric_version ?? '未提供'}</dd>
                        <dt>execution_mode</dt>
                        <dd>{report.reproducibility.execution_mode}</dd>
                        <dt>random_seed</dt>
                        <dd>{report.reproducibility.random_seed}</dd>
                        <dt>config_fingerprint</dt>
                        <dd className="mono small">{report.reproducibility.config_fingerprint}</dd>
                        <dt>created_at</dt>
                        <dd className="small">{report.reproducibility.created_at}</dd>
                      </dl>
                    </div>
                    <div>
                      <h4 className="pl-h4">阶段统计</h4>
                      <div className="pl-counts">
                        {Object.entries(report.stage_summary.counts)
                          .filter(([, v]) => v > 0)
                          .map(([k, v]) => (
                            <span key={k} className={`badge ${statusTone(k)}`}>
                              {k} {v}
                            </span>
                          ))}
                      </div>
                      <h4 className="pl-h4">关联工件</h4>
                      <div className="pl-refs">
                        {(
                          [
                            ['evaluation_run', report.refs.evaluation_run_id],
                            ['comparison', report.refs.comparison_id],
                            ['regression', report.refs.regression_report_id],
                            ['quality_gate', report.refs.quality_gate_report_id],
                            ['failure_pattern', report.refs.failure_pattern_report_id],
                          ] as const
                        ).map(([kind, ref]) => (
                          <span key={kind} className="badge muted">
                            {kind}: {ref ?? '—'}
                          </span>
                        ))}
                        <span className="badge muted">
                          traces: {report.refs.trace_ids.length} · bad cases:{' '}
                          {report.refs.bad_case_ids.length}
                        </span>
                      </div>
                      <h4 className="pl-h4">证据入口</h4>
                      <div className="pl-refs">
                        {report.evidence_refs.slice(0, 24).map((ref) => (
                          <span key={`${ref.kind}:${ref.ref_id}`} className="badge muted" title={ref.detail}>
                            {ref.kind}:{ref.ref_id}
                          </span>
                        ))}
                      </div>
                    </div>
                  </div>

                  {report.error && (
                    <div className="pl-error">
                      <AlertTriangle size={15} /> 流水线失败：{report.error.stage_id} ·{' '}
                      {report.error.type} · {report.error.message}
                    </div>
                  )}

                  <div className="pl-report-sections">
                    {report.evaluation && (
                      <div className="pl-panel">
                        <div className="pl-panel-head">评测摘要（确定性）</div>
                        <div className="pl-metrics">
                          <div className="pl-metric">
                            <span className="k">任务成功率</span>
                            <strong>{pct(report.evaluation.metrics.task_success_rate?.value)}</strong>
                          </div>
                          <div className="pl-metric">
                            <span className="k">工具正确率</span>
                            <strong>{pct(report.evaluation.metrics.tool_correctness_rate?.value)}</strong>
                          </div>
                          <div className="pl-metric">
                            <span className="k">安全通过率</span>
                            <strong>{pct(report.evaluation.metrics.safety_pass_rate?.value)}</strong>
                          </div>
                          <div className="pl-metric">
                            <span className="k">Bad Case</span>
                            <strong>{report.evaluation.bad_case_count}</strong>
                          </div>
                        </div>
                        <div className="muted small">
                          {report.evaluation.passed_cases}/{report.evaluation.executed_cases} 通过 ·
                          run {report.evaluation.evaluation_run_id}
                        </div>
                        {report.evaluation.bad_cases.length > 0 && (
                          <div className="pl-cases">
                            {report.evaluation.bad_cases.slice(0, 8).map((c) => (
                              <button
                                key={c.case_id}
                                className="btn pl-case-btn"
                                onClick={() => navigate(`/traces/${c.trace_id}`)}
                              >
                                {c.case_id} · Trace
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    )}

                    {report.version_comparison && (
                      <div className="pl-panel">
                        <div className="pl-panel-head">版本对比</div>
                        <div className="row">
                          <span className="badge blue">
                            {report.version_comparison.comparability?.verdict ?? 'UNKNOWN'}
                          </span>
                          <span className="mono small">{report.version_comparison.comparison_id}</span>
                        </div>
                        <div className="muted small">
                          回归案例：
                          {(report.version_comparison.regressed_cases ?? []).join('、') || '无'}
                        </div>
                        <div className="pl-deltas">
                          {(report.version_comparison.metric_deltas ?? []).slice(0, 6).map((m) => (
                            <div key={m.metric} className="row small">
                              <span className="mono">{m.metric}</span>
                              <span>
                                {m.baseline ?? '—'} → {m.candidate ?? '—'}
                              </span>
                              <span className={`badge ${m.status === 'REGRESSED' ? 'failed' : m.status === 'IMPROVED' ? 'success' : 'muted'}`}>
                                {m.status}
                              </span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    {report.regression && (
                      <div className="pl-panel">
                        <div className="pl-panel-head">回归检测</div>
                        <div className="row">
                          <span className={riskTone(report.regression.risk_level)}>
                            {report.regression.risk_level}
                          </span>
                          <span className={`badge ${gateTone(report.regression.gate_status)}`}>
                            {report.regression.gate_status}
                          </span>
                          <span className="mono small">{report.regression.regression_report_id}</span>
                        </div>
                        <div className="muted small">
                          触发规则：{report.regression.triggered_rules.join('、') || '无'}
                        </div>
                      </div>
                    )}

                    <div className="pl-panel">
                      <div className="pl-panel-head">质量门禁</div>
                      {report.quality_gate ? (
                        <>
                          <div className="row">
                            <span className={`badge ${gateTone(report.quality_gate.gate_status)}`}>
                              {report.quality_gate.gate_status}
                            </span>
                            <span className="badge muted">退出码 {report.quality_gate.exit_code}</span>
                            <span className="mono small">{report.quality_gate.gate_report_id}</span>
                          </div>
                          <div className="muted small">{report.quality_gate.gate_reason}</div>
                          <div className="muted small">
                            触发规则：
                            {(report.quality_gate.triggered_rules ?? [])
                              .map((r: unknown) => (typeof r === 'string' ? r : (r as { rule_id: string }).rule_id))
                              .join('、') || '无'}
                          </div>
                        </>
                      ) : (
                        <div className="muted small">质量门禁未执行（缺少基线或版本对比）</div>
                      )}
                    </div>
                  </div>

                  <details className="mt">
                    <summary className="muted small">
                      限制说明（{report.limitations.length}）
                    </summary>
                    <ul className="vc-limits">
                      {report.limitations.map((line) => (
                        <li key={line}>{line}</li>
                      ))}
                    </ul>
                  </details>
                  {pipeline?.final_report_paths && (
                    <div className="muted small mt">
                      报告文件：{Object.values(pipeline.final_report_paths).join(' · ')}
                    </div>
                  )}
                </Card>


              </>
            )}
                  <Card title="历史流水线" action={
                    <button className="btn" onClick={() => void loadAll()}>
                      <RefreshCw size={14} /> 刷新列表
                    </button>
                  }>
                    {history.length === 0 ? (
                      <EmptyState text="暂无历史流水线" />
                    ) : (
                      <div className="vc-table-wrap">
                        <table className="tbl pl-history">
                          <thead>
                            <tr>
                              <th>Pipeline</th>
                              <th>状态</th>
                              <th>数据集</th>
                              <th>阶段</th>
                              <th>门禁</th>
                              <th>创建时间</th>
                              <th>操作</th>
                            </tr>
                          </thead>
                          <tbody>
                            {history.map((item) => (
                              <tr key={item.pipeline_run_id}>
                                <td className="mono small">{item.pipeline_run_id}</td>
                                <td>
                                  <span className={`badge ${statusTone(item.status)}`}>{item.status}</span>
                                </td>
                                <td className="small">
                                  {item.dataset_id ?? '—'}@{item.dataset_version ?? '—'}
                                </td>
                                <td className="small">
                                  {item.succeeded_stages} 成功 / {item.skipped_stages} 跳过 /{' '}
                                  {item.failed_stages} 失败
                                </td>
                                <td>
                                  {item.quality_gate_status ? (
                                    <span className={`badge ${gateTone(item.quality_gate_status)}`}>
                                      {item.quality_gate_status}
                                    </span>
                                  ) : (
                                    <span className="muted small">—</span>
                                  )}
                                </td>
                                <td className="small">{item.created_at}</td>
                                <td>
                                  <button
                                    className="btn pl-case-btn"
                                    onClick={() => {
                                      void refreshPipeline(item.pipeline_run_id)
                                      void loadHistory()
                                    }}
                                  >
                                    查看
                                  </button>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </Card>
          </div>
        </>
      )}
    </div>
  )
}

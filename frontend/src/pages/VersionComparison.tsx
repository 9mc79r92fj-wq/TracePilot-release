import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeftRight, RefreshCw } from 'lucide-react'
import { api, apiErrorMessage } from '../api'
import { Card, EmptyState, ErrorBox, Spinner } from '../components/ui'

/**
 * 第 49 / 50 步：评测版本对比 + 回归检测工作台。
 *
 * 数据全部来自后端真实评测运行记录与持久化报告：
 * - 版本元数据（含 agent_version 缺失的显式说明，不伪造）
 * - 可比性检查（COMPARABLE / COMPARABLE_WITH_WARNINGS / NOT_COMPARABLE）
 * - 指标差异（保留分子/分母，pp 与相对变化分开；UNKNOWN 不当 0）
 * - Case-level Diff（FAIL→PASS / PASS→FAIL / PASS→PASS / FAIL→FAIL / UNKNOWN）
 * - 失败模式分布变化、回归风险矩阵、规则证据与质量门禁
 */

interface ExecutionConfig {
  data_source?: string | null
  judge_mode?: string | null
  judge_rubric_id?: string | null
  max_cases?: number | null
  execution_engine?: string | null
  paid_api_called?: boolean | null
}

interface VersionDescriptor {
  evaluation_version_id: string
  evaluation_run_id: string
  agent_version: string | null
  agent_version_note: string | null
  prompt_version: string | null
  prompt_version_note: string | null
  model_name: string | null
  model_simulated: boolean | null
  tool_config_version: string | null
  tools: string[]
  evaluator_version: string | null
  rubric_version: string | null
  rubric_id: string | null
  judge_mode: string
  dataset_version: string | null
  dataset_id: string | null
  dataset_fingerprint: string | null
  dataset_case_count: number | null
  execution_config: ExecutionConfig
  created_at: string | null
  identity_hash: string
}

interface ComparabilityIssue {
  code: string
  severity: string
  field: string
  baseline: unknown
  candidate: unknown
  message: string
}

interface SkippedCheck {
  code: string
  field: string
  reason: string
}

interface Comparability {
  verdict: string
  is_comparable: boolean
  issue_count: number
  issues: ComparabilityIssue[]
  critical_issues: ComparabilityIssue[]
  warning_issues: ComparabilityIssue[]
  skipped_checks: SkippedCheck[]
  note: string
}

interface MetricSide {
  value: number | null
  numerator: number | null
  denominator: number | null
}

interface MetricDelta {
  metric: string
  baseline: MetricSide
  candidate: MetricSide
  delta: number | null
  delta_unit: string
  relative_change: number | null
  relative_change_unit: string
  status: string
  denominator_definition: string | null
  shape: string
  note: string | null
}

interface DimensionDelta {
  dimension: string
  baseline: {
    pass: number | null
    fail: number | null
    unknown: number | null
    applicable_denominator: number | null
    pass_rate: number | null
  }
  candidate: {
    pass: number | null
    fail: number | null
    unknown: number | null
    applicable_denominator: number | null
    pass_rate: number | null
  }
  delta: number | null
  delta_unit: string
  status: string
}

interface CaseRow {
  case_id: string
  scenario_type: string | null
  status_baseline: string | null
  status_candidate: string | null
  transition: string
  is_regression: boolean
  is_improvement: boolean
  judge_overall_delta: number | null
  trace_id_baseline: string | null
  trace_id_candidate: string | null
  links: Record<string, string | null>
}

interface CaseCounts {
  common: number
  improved: number
  regressed: number
  stable_pass: number
  stable_fail: number
  unknown_involved: number
  only_in_baseline: number
  only_in_candidate: number
}

interface PatternRow {
  fingerprint_id: string
  fingerprint_label: string
  failure_type: string
  cases_baseline: number
  cases_candidate: number
  delta: number
  status: string
}

interface FailurePatternDiff {
  status: string
  note?: string
  baseline?: { pattern_count?: number; denominators?: Record<string, unknown> }
  candidate?: { pattern_count?: number; denominators?: Record<string, unknown> }
  rows?: PatternRow[]
  new_patterns?: PatternRow[]
  resolved_patterns?: PatternRow[]
}

interface ComparisonReport {
  comparison_id: string
  baseline_version: VersionDescriptor
  candidate_version: VersionDescriptor
  comparability: Comparability
  metric_deltas: MetricDelta[]
  dimension_deltas: DimensionDelta[]
  bad_case_diff: {
    baseline: { failed_cases: number | null; unknown_cases: number | null }
    candidate: { failed_cases: number | null; unknown_cases: number | null }
    delta_failed_cases: number
    note: string
  }
  cases: {
    counts: CaseCounts
    rows: CaseRow[]
    regressed_cases: { case_id: string }[]
    improved_cases: { case_id: string }[]
    missing_cases: { only_in_baseline: string[]; only_in_candidate: string[] }
    note: string
  }
  failure_pattern_diff: FailurePatternDiff
  limits: { max_cases: number | null; compared_cases: number }
  limitations: string[]
  paid_api_called: boolean
  generated_at: string
}

interface RuleEvent {
  rule_id: string
  metric_name: string
  direction: string
  threshold: number
  threshold_unit: string
  severity: string
  blocking: boolean
  status: string
  observed: Record<string, unknown> | null
  evidence_refs: { kind: string; ref_id: string; detail?: string }[]
  reason: string
}

interface RuleRef {
  rule_id: string
  reason: string
}

interface RegressionReport {
  regression_report_id: string
  comparison_id: string
  rules: { rule_id: string; blocking: boolean }[]
  rule_events: RuleEvent[]
  triggered_rules: {
    rule_id: string
    metric_name: string
    severity: string
    blocking: boolean
    threshold: number
    threshold_unit: string
    reason: string
    observed: Record<string, unknown> | null
  }[]
  not_evaluable_rules: RuleRef[]
  not_applicable_rules: RuleRef[]
  disabled_rules: RuleRef[]
  regressed_cases: { case_id: string; scenario_type?: string | null }[]
  risk_level: string
  risk_reason: string
  gate_status: string
  gate_reason: string
  evidence_refs: { kind: string; ref_id: string; detail?: string }[]
  limitations: string[]
  threshold_source: string
  paid_api_called: boolean
  generated_at: string
}

interface GateCheck {
  check_id: string
  rule_id: string
  category: string
  status: string
  severity: string
  message: string
  threshold: { value?: unknown; unit?: string; calculation?: string }
  actual: Record<string, unknown>
  evidence_refs: { kind: string; ref_id: string; detail?: string; trace_ids?: string[] }[]
  missing_evidence: boolean
}

interface GateTriggeredRule {
  rule_id: string
  check_id: string
  category: string
  status: string
  severity: string
  threshold: { value?: unknown; unit?: string; calculation?: string }
  actual: Record<string, unknown>
  message: string
  evidence_refs: { kind: string; ref_id: string; detail?: string; trace_ids?: string[] }[]
}

interface QualityGateReport {
  gate_report_id: string
  gate_status: string
  gate_reason: string
  exit_code: number
  ci_run_id: string
  git_commit: string
  dataset_version: string | null
  evaluator_version: string | null
  baseline_version: VersionDescriptor
  candidate_version: VersionDescriptor
  comparison_id: string
  regression_report_id: string
  comparability: { verdict?: string; issue_count?: number }
  checks: GateCheck[]
  triggered_rules: GateTriggeredRule[]
  advisory_regression_rules: { rule_id: string; severity: string; message: string }[]
  evidence_refs: { kind: string; ref_id: string; detail?: string; trace_ids?: string[] }[]
  config: {
    version: number
    label: string
    fingerprint: string
    source: string
    path: string | null
    values: Record<string, unknown>
  }
  limitations: string[]
  paid_api_called: boolean
  generated_at: string
}

const METRIC_LABEL: Record<string, string> = {
  task_success_rate: '任务成功率',
  tool_correctness_rate: '工具正确率',
  recovery_rate: '故障恢复率',
  safety_pass_rate: '安全通过率',
  average_duration_ms: '平均耗时（ms）',
  total_tokens: 'Token 成本',
}

const DIMENSION_LABEL: Record<string, string> = {
  task_success: '任务成功',
  tool_correctness: '工具正确性',
  process_quality: '过程质量',
  recovery: '故障恢复',
  efficiency: '效率',
  safety: '安全',
}

const RULE_LABEL: Record<string, string> = {
  NEW_SAFETY_FAILURE: '新增安全失败',
  TASK_SUCCESS_DROP: '成功率下降',
  TOOL_CORRECTNESS_DROP: '工具正确率下降',
  RECOVERY_RATE_DROP: '恢复率下降',
  LATENCY_INCREASE: '耗时上升',
  COST_INCREASE: '成本上升',
  CASE_REGRESSION: '案例回归',
  NEW_FAILURE_PATTERN: '新失败模式',
}

const RISK_DIMENSIONS: { key: string; label: string; ruleIds: string[] }[] = [
  { key: 'quality', label: '质量', ruleIds: ['TASK_SUCCESS_DROP', 'TOOL_CORRECTNESS_DROP', 'RECOVERY_RATE_DROP'] },
  { key: 'safety', label: '安全', ruleIds: ['NEW_SAFETY_FAILURE'] },
  { key: 'performance', label: '性能', ruleIds: ['LATENCY_INCREASE'] },
  { key: 'cost', label: '成本', ruleIds: ['COST_INCREASE'] },
  { key: 'case', label: '案例', ruleIds: ['CASE_REGRESSION'] },
  { key: 'pattern', label: '失败模式', ruleIds: ['NEW_FAILURE_PATTERN'] },
]

const SEVERITIES = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL']

const TRANSITIONS: { value: string; label: string }[] = [
  { value: 'ALL', label: '全部迁移' },
  { value: 'PASS_TO_FAIL', label: 'PASS → FAIL（回归）' },
  { value: 'FAIL_TO_PASS', label: 'FAIL → PASS（改善）' },
  { value: 'PASS_TO_PASS', label: 'PASS → PASS（稳定通过）' },
  { value: 'FAIL_TO_FAIL', label: 'FAIL → FAIL（稳定失败）' },
  { value: 'UNKNOWN_INVOLVED', label: '含 UNKNOWN（不计入通过/失败）' },
]

function statusClass(status: string | null | undefined): string {
  switch (status) {
    case 'PASS':
      return 'badge success'
    case 'FAIL':
      return 'badge failed'
    case 'UNKNOWN':
      return 'badge partial'
    case 'NOT_APPLICABLE':
      return 'badge muted'
    default:
      return 'badge muted'
  }
}

function deltaClass(status: string): string {
  switch (status) {
    case 'IMPROVED':
      return 'badge success'
    case 'REGRESSED':
      return 'badge failed'
    case 'UNCHANGED':
      return 'badge muted'
    default:
      return 'badge partial'
  }
}

function verdictTone(verdict: string): string {
  switch (verdict) {
    case 'COMPARABLE':
      return 'ok'
    case 'COMPARABLE_WITH_WARNINGS':
      return 'warn'
    case 'NOT_COMPARABLE':
      return 'bad'
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

function gateTone(status: string): string {
  switch (status) {
    case 'PASS':
      return 'success'
    case 'WARN':
      return 'partial'
    case 'BLOCK':
      return 'failed'
    default:
      return 'muted'
  }
}

function ruleStateClass(status: string): string {
  switch (status) {
    case 'TRIGGERED':
      return 'rx-chip triggered'
    case 'NOT_TRIGGERED':
      return 'rx-chip clean'
    case 'NOT_EVALUABLE':
      return 'rx-chip unknown'
    default:
      return 'rx-chip off'
  }
}

function transitionTone(transition: string): string {
  switch (transition) {
    case 'PASS_TO_FAIL':
      return 'failed'
    case 'FAIL_TO_PASS':
      return 'success'
    case 'PASS_TO_PASS':
      return 'success'
    case 'FAIL_TO_FAIL':
      return 'muted'
    default:
      return 'partial'
  }
}

function transitionLabel(transition: string): string {
  const hit = TRANSITIONS.find((t) => t.value === transition)
  return hit ? hit.label.split('（')[0] : transition
}

function fmtFixed(value: number, digits = 2): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(digits)
}

function fmtMetricValue(metric: MetricDelta, side: MetricSide): string {
  if (side.value === null || side.value === undefined) return '—'
  if (metric.delta_unit === 'percentage_point') {
    return `${(side.value * 100).toFixed(2)}%`
  }
  return fmtFixed(side.value)
}

function fmtDelta(metric: MetricDelta): string {
  if (metric.delta === null) return '差异不可计算（UNKNOWN 不当作 0）'
  const sign = metric.delta > 0 ? '+' : ''
  if (metric.delta_unit === 'percentage_point') {
    return `${sign}${metric.delta.toFixed(2)} pp（百分点）`
  }
  return `${sign}${fmtFixed(metric.delta)}（绝对值）`
}

function fmtRelative(metric: MetricDelta): string {
  if (metric.relative_change === null) return '相对变化无定义（基线为 0）'
  const pct = metric.relative_change * 100
  return `相对变化 ${pct > 0 ? '+' : ''}${pct.toFixed(1)}%`
}

function fmtFraction(side: MetricSide): string {
  if (side.numerator === null && side.denominator === null) return '标量指标（无分子分母）'
  return `${side.numerator ?? '—'} / ${side.denominator ?? '—'}`
}

function fmtObserved(observed: Record<string, unknown> | null): string {
  if (!observed) return '—'
  const keys = Object.keys(observed)
  if (keys.length === 0) return '—'
  return keys.map((k) => `${k}=${String(observed[k])}`).join(' · ')
}

function shortRunId(runId: string): string {
  return runId.replace(/^eval-/, '').slice(0, 8)
}

function runOptionLabel(desc: VersionDescriptor): string {
  const dataset = desc.dataset_id ?? '未知数据集'
  const version = desc.dataset_version ?? '—'
  return `${shortRunId(desc.evaluation_run_id)} · ${dataset}@${version} · ${desc.evaluation_version_id}`
}

export default function VersionComparison() {
  const navigate = useNavigate()
  const [runs, setRuns] = useState<VersionDescriptor[]>([])
  const [baseline, setBaseline] = useState('')
  const [candidate, setCandidate] = useState('')
  const [cmp, setCmp] = useState<ComparisonReport | null>(null)
  const [reg, setReg] = useState<RegressionReport | null>(null)
  const [gate, setGate] = useState<QualityGateReport | null>(null)
  const [loadingRuns, setLoadingRuns] = useState(true)
  const [busy, setBusy] = useState(false)
  const [regBusy, setRegBusy] = useState(false)
  const [gateBusy, setGateBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [regError, setRegError] = useState<string | null>(null)
  const [gateError, setGateError] = useState<string | null>(null)
  const [transition, setTransition] = useState('ALL')

  const loadRuns = useCallback(async () => {
    setLoadingRuns(true)
    setError(null)
    try {
      const r = await api.get<{ count: number; runs: VersionDescriptor[] }>(
        '/api/version-comparison/runs',
        { params: { limit: 50 } },
      )
      const list = r.data.runs
      setRuns(list)
      if (list.length >= 2) {
        setBaseline((prev) => prev || list[list.length - 1].evaluation_run_id)
        setCandidate((prev) => prev || list[list.length - 2].evaluation_run_id)
      } else if (list.length === 1) {
        setBaseline((prev) => prev || list[0].evaluation_run_id)
        setCandidate((prev) => prev || list[0].evaluation_run_id)
      }
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setLoadingRuns(false)
    }
  }, [])

  useEffect(() => {
    void loadRuns()
  }, [loadRuns])

  const options = useMemo(() => [...runs].reverse(), [runs])

  async function runCompare() {
    if (!baseline || !candidate) {
      setError('请选择基线版本与候选版本')
      return
    }
    if (baseline === candidate) {
      setError('基线版本与候选版本不能相同')
      return
    }
    setBusy(true)
    setError(null)
    setRegError(null)
    setGateError(null)
    setReg(null)
    setGate(null)
    try {
      const r = await api.post<ComparisonReport>('/api/version-comparison/runs', {
        baseline_run_id: baseline,
        candidate_run_id: candidate,
        max_cases: 50,
      })
      setCmp(r.data)
      setTransition('ALL')
    } catch (e) {
      setCmp(null)
      setError(apiErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  async function runRegression() {
    if (!cmp) return
    setRegBusy(true)
    setRegError(null)
    try {
      const r = await api.post<RegressionReport>('/api/regression/runs', {
        comparison_id: cmp.comparison_id,
      })
      setReg(r.data)
    } catch (e) {
      setRegError(apiErrorMessage(e))
    } finally {
      setRegBusy(false)
    }
  }

  async function runQualityGate() {
    if (!cmp) return
    setGateBusy(true)
    setGateError(null)
    try {
      const r = await api.post<QualityGateReport>('/api/quality-gate/runs', {
        comparison_id: cmp.comparison_id,
      })
      setGate(r.data)
    } catch (e) {
      setGateError(apiErrorMessage(e))
    } finally {
      setGateBusy(false)
    }
  }

  const selectedBase = runs.find((d) => d.evaluation_run_id === baseline) ?? null
  const selectedCand = runs.find((d) => d.evaluation_run_id === candidate) ?? null
  const sameVersion = Boolean(
    selectedBase && selectedCand && selectedBase.identity_hash === selectedCand.identity_hash,
  )

  const caseRows = cmp?.cases.rows ?? []
  const filteredCases = useMemo(
    () => (transition === 'ALL' ? caseRows : caseRows.filter((r) => r.transition === transition)),
    [caseRows, transition],
  )

  return (
    <div className="stack">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <h2 style={{ margin: 0 }}>Version Comparison · 评测版本对比与回归检测</h2>
          <div className="muted small">
            版本元数据、可比性检查、指标差异、案例级 Diff、失败模式变化、回归风险矩阵与质量门禁。
          </div>
        </div>
        <div className="row">
          <span className="badge blue">数据来源：真实评测运行</span>
          <span className="badge muted">无付费 API 调用</span>
        </div>
      </div>

      <Card
        title="版本选择（基线 → 候选）"
        action={
          <button className="btn" onClick={() => void loadRuns()} disabled={loadingRuns}>
            <RefreshCw size={14} /> 重新加载运行
          </button>
        }
      >
        {loadingRuns ? (
          <Spinner />
        ) : runs.length === 0 ? (
          <div className="vc-empty-actions">
            <EmptyState text="暂无可比较的评测运行（仅列出报告可读的成功运行）。" />
            <button className="btn primary" onClick={() => navigate('/evaluation')}>
              去 Evaluation Center 运行一次评测
            </button>
          </div>
        ) : (
          <>
            <div className="vc-controls">
              <label className="vc-field">
                <span>基线版本（Baseline）</span>
                <select
                  aria-label="基线版本（Baseline）"
                  value={baseline}
                  onChange={(e) => setBaseline(e.target.value)}
                >
                  {options.map((d) => (
                    <option key={d.evaluation_run_id} value={d.evaluation_run_id}>
                      {runOptionLabel(d)}
                    </option>
                  ))}
                </select>
              </label>
              <span className="vc-arrow-lg" aria-hidden>
                →
              </span>
              <label className="vc-field">
                <span>候选版本（Candidate）</span>
                <select
                  aria-label="候选版本（Candidate）"
                  value={candidate}
                  onChange={(e) => setCandidate(e.target.value)}
                >
                  {options.map((d) => (
                    <option key={d.evaluation_run_id} value={d.evaluation_run_id}>
                      {runOptionLabel(d)}
                    </option>
                  ))}
                </select>
              </label>
              <div className="row vc-actions">
                <button className="btn primary" onClick={() => void runCompare()} disabled={busy}>
                  <ArrowLeftRight size={14} /> {busy ? '对比中…' : '开始对比'}
                </button>
                <button
                  className="btn"
                  onClick={() => void runRegression()}
                  disabled={!cmp || regBusy}
                  title={cmp ? '基于当前对比报告运行回归检测' : '请先完成版本对比'}
                >
                  {regBusy ? '回归检测中…' : '运行回归检测'}
                </button>
                <button
                  className="btn"
                  onClick={() => void runQualityGate()}
                  disabled={!cmp || gateBusy}
                  title={cmp ? '基于当前对比报告运行 CI 质量门禁（确定性规则）' : '请先完成版本对比'}
                >
                  {gateBusy ? '门禁检查中…' : '运行质量门禁'}
                </button>
              </div>
            </div>
            {selectedBase && selectedCand && (
              <div className="row mt vc-same-version">
                <span className="mono small">{selectedBase.evaluation_version_id}</span>
                <span className={sameVersion ? 'badge success' : 'badge partial'}>
                  {sameVersion ? '同版本（身份哈希一致）' : '跨版本（身份哈希不同）'}
                </span>
                <span className="muted small">
                  数据集：{selectedBase.dataset_id}@{selectedBase.dataset_version} · 案例数{' '}
                  {selectedBase.dataset_case_count ?? '—'}
                </span>
              </div>
            )}
          </>
        )}
        {error ? <div className="mt"><ErrorBox message={error} /></div> : null}
      </Card>

      {!cmp ? (
        <Card title="对比结果">
          <EmptyState text="选择基线版本与候选版本后点击「开始对比」。" />
        </Card>
      ) : (
        <>
          <div className={`vc-banner ${verdictTone(cmp.comparability.verdict)}`}>
            <div className="vc-banner-head">
              <span className={`badge ${cmp.comparability.verdict === 'COMPARABLE' ? 'success' : cmp.comparability.verdict === 'NOT_COMPARABLE' ? 'failed' : 'partial'}`}>
                可比性：{cmp.comparability.verdict}
              </span>
              <span className="mono small">对比报告 {cmp.comparison_id}</span>
              <span className="muted small">
                共同案例 {cmp.cases.counts.common} · 已配对 {cmp.limits.compared_cases}
              </span>
            </div>
            <p className="vc-banner-note">{cmp.comparability.note}</p>
            {cmp.comparability.issues.length > 0 && (
              <ul className="vc-issues">
                {cmp.comparability.issues.map((issue) => (
                  <li key={`${issue.code}-${issue.field}`} className={`vc-issue ${issue.severity.toLowerCase()}`}>
                    <span className={`badge ${issue.severity === 'CRITICAL' ? 'failed' : 'partial'}`}>
                      {issue.severity}
                    </span>
                    <span className="mono small">{issue.code}</span>
                    <span>{issue.message}</span>
                    <span className="muted small">
                      （A: {String(issue.baseline ?? '—')} / B: {String(issue.candidate ?? '—')}）
                    </span>
                  </li>
                ))}
              </ul>
            )}
            {cmp.comparability.skipped_checks.length > 0 && (
              <div className="muted small">
                跳过检查：
                {cmp.comparability.skipped_checks.map((s) => `${s.code}（${s.reason}）`).join('；')}
              </div>
            )}
            {!cmp.comparability.is_comparable && (
              <div className="vc-warning-strong">
                ⚠ 两个版本不可比：下列指标差异只能作为诊断线索，不得解释为性能提升或退化。
              </div>
            )}
          </div>

          <div className="vc-meta-grid">
            <VersionMetaCard title="基线版本（A）" desc={cmp.baseline_version} />
            <VersionMetaCard title="候选版本（B）" desc={cmp.candidate_version} />
          </div>

          <Card title="指标差异（基线 → 候选）">
            <div className="vc-metric-grid">
              {cmp.metric_deltas.map((m) => (
                <div key={m.metric} className="vc-metric">
                  <div className="vc-metric-name">{METRIC_LABEL[m.metric] ?? m.metric}</div>
                  <div className="vc-metric-values">
                    <span>{fmtMetricValue(m, m.baseline)}</span>
                    <span className="vc-metric-arrow" aria-hidden>
                      →
                    </span>
                    <span>{fmtMetricValue(m, m.candidate)}</span>
                  </div>
                  <div className="vc-metric-delta">{fmtDelta(m)}</div>
                  <div className="muted small">{fmtRelative(m)}</div>
                  <div className="muted small">
                    分子/分母：A {fmtFraction(m.baseline)} · B {fmtFraction(m.candidate)}
                  </div>
                  {m.denominator_definition && (
                    <div className="muted small">统计分母：{m.denominator_definition}</div>
                  )}
                  <div className="vc-metric-foot">
                    <span className={deltaClass(m.status)}>{m.status}</span>
                  </div>
                  {m.note && <div className="muted small">{m.note}</div>}
                </div>
              ))}
            </div>
            <p className="muted small mt">
              pp 为百分点差；相对变化以基线为分母；UNKNOWN 不计为 0。
            </p>
          </Card>

          <Card title="维度通过率差异（dimension_breakdown）">
            {cmp.dimension_deltas.length === 0 ? (
              <EmptyState text="两个版本均未提供维度分解" />
            ) : (
              <div className="vc-table-wrap">
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>维度</th>
                      <th>基线通过/可判定</th>
                      <th>候选通过/可判定</th>
                      <th>Δ（pp）</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cmp.dimension_deltas.map((d) => (
                      <tr key={d.dimension}>
                        <td>{DIMENSION_LABEL[d.dimension] ?? d.dimension}</td>
                        <td>
                          {d.baseline.pass ?? '—'} / {d.baseline.applicable_denominator ?? '—'}
                        </td>
                        <td>
                          {d.candidate.pass ?? '—'} / {d.candidate.applicable_denominator ?? '—'}
                        </td>
                        <td>{d.delta === null ? '—' : `${d.delta > 0 ? '+' : ''}${d.delta.toFixed(2)}`}</td>
                        <td>
                          <span className={deltaClass(d.status)}>{d.status}</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card
            title={`Case-level Diff（共同案例 ${cmp.cases.counts.common} 例）`}
            action={
              <span className="muted small">
                Bad Case：A {cmp.bad_case_diff.baseline.failed_cases ?? '—'} → B{' '}
                {cmp.bad_case_diff.candidate.failed_cases ?? '—'}
              </span>
            }
          >
            <div className="row vc-filter">
              <label className="vc-field">
                <span>按状态迁移筛选</span>
                <select
                  aria-label="按状态迁移筛选"
                  value={transition}
                  onChange={(e) => setTransition(e.target.value)}
                >
                  {TRANSITIONS.map((t) => (
                    <option key={t.value} value={t.value}>
                      {t.label}
                    </option>
                  ))}
                </select>
              </label>
              <span className="muted small">{cmp.cases.note}</span>
            </div>

            <div className="vc-counts">
              <span className="badge success">改善 {cmp.cases.counts.improved}</span>
              <span className="badge failed">回归 {cmp.cases.counts.regressed}</span>
              <span className="badge success">稳定通过 {cmp.cases.counts.stable_pass}</span>
              <span className="badge muted">稳定失败 {cmp.cases.counts.stable_fail}</span>
              <span className="badge partial">含 UNKNOWN {cmp.cases.counts.unknown_involved}</span>
              <span className="badge muted">仅 A {cmp.cases.counts.only_in_baseline}</span>
              <span className="badge muted">仅 B {cmp.cases.counts.only_in_candidate}</span>
            </div>

            {filteredCases.length === 0 ? (
              <div className="vc-empty">该迁移下没有案例</div>
            ) : (
              <div className="vc-table-wrap">
                <table className="tbl vc-cases">
                  <thead>
                    <tr>
                      <th>案例</th>
                      <th>场景</th>
                      <th>基线</th>
                      <th>候选</th>
                      <th>状态迁移</th>
                      <th>Judge Δ</th>
                      <th>Trace / DAG</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredCases.map((row) => (
                      <tr
                        key={row.case_id}
                        className={
                          row.is_regression ? 'vc-case-row regressed' : row.is_improvement ? 'vc-case-row improved' : 'vc-case-row'
                        }
                      >
                        <td className="mono">{row.case_id}</td>
                        <td className="small">{row.scenario_type ?? '—'}</td>
                        <td>
                          <span className={statusClass(row.status_baseline)}>{row.status_baseline ?? '—'}</span>
                        </td>
                        <td>
                          <span className={statusClass(row.status_candidate)}>{row.status_candidate ?? '—'}</span>
                        </td>
                        <td>
                          <span className={`badge ${transitionTone(row.transition)}`}>
                            {transitionLabel(row.transition)}
                          </span>
                        </td>
                        <td>{row.judge_overall_delta === null ? '—' : row.judge_overall_delta}</td>
                        <td>
                          <div className="row vc-trace-actions">
                            {row.trace_id_baseline && (
                              <button
                                className="btn vc-tbtn"
                                onClick={() => navigate(`/traces/${row.trace_id_baseline}`)}
                              >
                                基线 Trace
                              </button>
                            )}
                            {row.trace_id_candidate && (
                              <button
                                className="btn vc-tbtn"
                                onClick={() => navigate(`/traces/${row.trace_id_candidate}`)}
                              >
                                候选 Trace
                              </button>
                            )}
                            {row.trace_id_candidate && (
                              <button
                                className="btn vc-tbtn"
                                onClick={() => navigate(`/dag/${row.trace_id_candidate}`)}
                              >
                                候选 DAG
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {(cmp.cases.missing_cases.only_in_baseline.length > 0 ||
              cmp.cases.missing_cases.only_in_candidate.length > 0) && (
              <div className="vc-missing">
                <b>缺失案例（不参与配对统计）：</b>
                {cmp.cases.missing_cases.only_in_baseline.length > 0 && (
                  <div>仅基线：{cmp.cases.missing_cases.only_in_baseline.join('、')}</div>
                )}
                {cmp.cases.missing_cases.only_in_candidate.length > 0 && (
                  <div>仅候选：{cmp.cases.missing_cases.only_in_candidate.join('、')}</div>
                )}
              </div>
            )}
          </Card>

          <Card title="失败模式分布变化（复用 Failure Pattern Mining）">
            {cmp.failure_pattern_diff.status !== 'AVAILABLE' ? (
              <EmptyState text={cmp.failure_pattern_diff.note ?? '失败模式分布不可用'} />
            ) : (
              <>
                <div className="row mb">
                  <span className="badge partial">
                    新增失败模式 {(cmp.failure_pattern_diff.new_patterns ?? []).length}
                  </span>
                  <span className="badge success">
                    消失失败模式 {(cmp.failure_pattern_diff.resolved_patterns ?? []).length}
                  </span>
                  <span className="muted small">
                    A 分母 bad_cases={String(
                      cmp.failure_pattern_diff.baseline?.denominators?.bad_cases ?? '—',
                    )}{' '}
                    · B 分母 bad_cases={String(
                      cmp.failure_pattern_diff.candidate?.denominators?.bad_cases ?? '—',
                    )}
                  </span>
                </div>
                {(cmp.failure_pattern_diff.rows ?? []).length === 0 ? (
                  <EmptyState text="两个版本均无失败模式样本" />
                ) : (
                  <div className="vc-table-wrap">
                    <table className="tbl">
                      <thead>
                        <tr>
                          <th>失败模式指纹</th>
                          <th>失败类型</th>
                          <th>A 案例数</th>
                          <th>B 案例数</th>
                          <th>变化</th>
                          <th>状态</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(cmp.failure_pattern_diff.rows ?? []).map((p) => (
                          <tr key={p.fingerprint_id}>
                            <td className="small">{p.fingerprint_label}</td>
                            <td className="mono small">{p.failure_type}</td>
                            <td>{p.cases_baseline}</td>
                            <td>{p.cases_candidate}</td>
                            <td>{p.delta > 0 ? `+${p.delta}` : p.delta}</td>
                            <td>
                              <span
                                className={
                                  p.status === 'NEW'
                                    ? 'badge failed'
                                    : p.status === 'RESOLVED'
                                      ? 'badge success'
                                      : 'badge muted'
                                }
                              >
                                {p.status}
                              </span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                {cmp.failure_pattern_diff.note && (
                  <p className="muted small mt">{cmp.failure_pattern_diff.note}</p>
                )}
              </>
            )}
          </Card>

          {regError ? <ErrorBox message={regError} /> : null}

          {reg && <RegressionSection report={reg} />}

          {gateError ? <ErrorBox message={gateError} /> : null}

          {gate && <QualityGateSection report={gate} />}

          <Card title="限制与证据说明">
            <ul className="vc-limits">
              {cmp.limitations.map((line) => (
                <li key={`cmp-${line}`}>{line}</li>
              ))}
              {reg?.limitations.map((line) => (
                <li key={`reg-${line}`}>{line}</li>
              ))}
            </ul>
          </Card>
        </>
      )}
    </div>
  )
}

function VersionMetaCard(props: { title: string; desc: VersionDescriptor }) {
  const d = props.desc
  return (
    <div className="card vc-meta">
      <div className="card-head">
        <span>{props.title}</span>
        <span className="mono small">{shortRunId(d.evaluation_run_id)}</span>
      </div>
      <div className="card-body">
        <dl className="vc-kv">
          <dt>运行 ID</dt>
          <dd className="mono">{d.evaluation_run_id}</dd>
          <dt>版本 ID</dt>
          <dd className="mono">{d.evaluation_version_id}</dd>
          <dt>身份哈希</dt>
          <dd className="mono" title={d.identity_hash}>
            {d.identity_hash.slice(0, 16)}…
          </dd>
          <dt>数据集</dt>
          <dd>
            {d.dataset_id}@{d.dataset_version}（{d.dataset_case_count ?? '—'} 例）
          </dd>
          <dt>数据指纹</dt>
          <dd className="mono" title={d.dataset_fingerprint ?? ''}>
            {d.dataset_fingerprint ? `${d.dataset_fingerprint.slice(0, 16)}…` : '—'}
          </dd>
          <dt>评测器版本</dt>
          <dd className="mono">{d.evaluator_version ?? '—'}</dd>
          <dt>Judge 模式</dt>
          <dd>{d.judge_mode}</dd>
          <dt>Rubric</dt>
          <dd>
            {d.rubric_id
              ? `${d.rubric_id}@${d.rubric_version ?? '—'}`
              : '未启用 Judge（Rubric 不适用）'}
          </dd>
          <dt>模型</dt>
          <dd>
            {d.model_name ?? '—'}
            {d.model_simulated ? '（模拟执行器）' : ''}
          </dd>
          <dt>工具配置</dt>
          <dd className="mono">
            {d.tool_config_version ?? '—'}
            {d.tools.length > 0 ? ` · ${d.tools.join(', ')}` : ''}
          </dd>
          <dt>执行模式</dt>
          <dd>{d.execution_config.execution_engine ?? '—'}</dd>
          <dt>agent_version</dt>
          <dd>
            {d.agent_version ?? '未提供'}
            {d.agent_version_note ? `（${d.agent_version_note}）` : ''}
          </dd>
          <dt>prompt_version</dt>
          <dd>{d.prompt_version ?? d.prompt_version_note ?? '未提供'}</dd>
          <dt>创建时间</dt>
          <dd className="small">{d.created_at ?? '—'}</dd>
        </dl>
      </div>
    </div>
  )
}

function RegressionSection(props: { report: RegressionReport }) {
  const rep = props.report
  const byRule = new Map(rep.rule_events.map((e) => [e.rule_id, e]))
  return (
    <Card
      title="回归检测"
      action={<span className="badge muted">阈值来源：{rep.threshold_source}</span>}
    >
      <div className={`rx-banner ${gateTone(rep.gate_status)}`}>
        <div className="row rx-banner-head">
          <span className={`badge ${riskTone(rep.risk_level)}`}>风险等级 {rep.risk_level}</span>
          <span className={`badge ${gateTone(rep.gate_status)}`}>门禁 {rep.gate_status}</span>
          <span className="mono small">{rep.regression_report_id}</span>
        </div>
        <p>
          <b>风险判定：</b>
          {rep.risk_reason}
        </p>
        <p>
          <b>门禁判定：</b>
          {rep.gate_reason}
        </p>
        <p className="muted small">
          INCONCLUSIVE 视为无法判定，不等同 PASS；风险等级取规则严重度最大值。
        </p>
      </div>

      <h4 className="vc-h4">回归风险矩阵（风险维度 × 严重度）</h4>
      <div className="rx-matrix-wrap">
        <table className="tbl rx-matrix">
          <thead>
            <tr>
              <th>风险维度</th>
              {SEVERITIES.map((s) => (
                <th key={s}>{s}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {RISK_DIMENSIONS.map((dim) => (
              <tr key={dim.key}>
                <th scope="row">{dim.label}</th>
                {SEVERITIES.map((sev) => {
                  const rules = dim.ruleIds
                    .map((id) => byRule.get(id))
                    .filter((e): e is RuleEvent => Boolean(e) && e?.severity === sev)
                  return (
                    <td key={sev}>
                      <div className="rx-matrix-cell">
                        {rules.length === 0 ? (
                          <span className="muted small">—</span>
                        ) : (
                          rules.map((e) => (
                            <span key={e.rule_id} className={ruleStateClass(e.status)} title={`${e.rule_id}：${e.reason}`}>
                              {RULE_LABEL[e.rule_id] ?? e.rule_id}
                            </span>
                          ))
                        )}
                      </div>
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="row rx-legend">
        <span className="rx-chip triggered">已触发</span>
        <span className="rx-chip clean">未触发</span>
        <span className="rx-chip unknown">无法求值</span>
        <span className="rx-chip off">不适用 / 已禁用</span>
        <span className="muted small">阻断规则：{rep.rules.filter((r) => r.blocking).length} 条</span>
      </div>

      <h4 className="vc-h4">规则求值明细</h4>
      <div className="vc-table-wrap">
        <table className="tbl rx-rules">
          <thead>
            <tr>
              <th>规则</th>
              <th>指标</th>
              <th>阈值</th>
              <th>严重度</th>
              <th>阻断</th>
              <th>状态</th>
              <th>判定说明</th>
            </tr>
          </thead>
          <tbody>
            {rep.rule_events.map((e) => (
              <tr key={e.rule_id}>
                <td className="mono small">{e.rule_id}</td>
                <td className="small">{e.metric_name}</td>
                <td className="small">
                  {e.threshold}
                  {e.threshold_unit === 'percentage_point'
                    ? 'pp'
                    : e.threshold_unit === 'ratio'
                      ? '（相对）'
                      : '（计数）'}
                </td>
                <td>
                  <span className={riskTone(e.severity)}>{e.severity}</span>
                </td>
                <td>{e.blocking ? '是' : '否'}</td>
                <td>
                  <span className={ruleStateClass(e.status).replace('rx-chip', 'badge')}>{e.status}</span>
                </td>
                <td className="small">
                  {e.reason}
                  {e.observed ? <span className="muted"> · {fmtObserved(e.observed)}</span> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="rx-lists">
        <div className="rx-list">
          <div className="rx-list-head">触发规则（{rep.triggered_rules.length}）</div>
          {rep.triggered_rules.length === 0 ? (
            <div className="muted small">无触发规则</div>
          ) : (
            <ul>
              {rep.triggered_rules.map((t) => (
                <li key={t.rule_id}>
                  <span className="mono small">{t.rule_id}</span>
                  <span className={riskTone(t.severity)}> {t.severity}</span>
                  <span className="muted small">
                    {' '}
                    · 阈值 {t.threshold}
                    {t.threshold_unit === 'percentage_point' ? 'pp' : ''} · {t.reason}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="rx-list">
          <div className="rx-list-head">无法求值（{rep.not_evaluable_rules.length}）</div>
          {rep.not_evaluable_rules.length === 0 ? (
            <div className="muted small">无</div>
          ) : (
            <ul>
              {rep.not_evaluable_rules.map((r) => (
                <li key={r.rule_id}>
                  <span className="mono small">{r.rule_id}</span>
                  <span className="muted small"> · {r.reason}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="rx-list">
          <div className="rx-list-head">不适用 / 已禁用</div>
          {rep.not_applicable_rules.length + rep.disabled_rules.length === 0 ? (
            <div className="muted small">无</div>
          ) : (
            <ul>
              {rep.not_applicable_rules.map((r) => (
                <li key={`na-${r.rule_id}`}>
                  <span className="mono small">{r.rule_id}</span>
                  <span className="muted small"> · {r.reason}</span>
                </li>
              ))}
              {rep.disabled_rules.map((r) => (
                <li key={`dis-${r.rule_id}`}>
                  <span className="mono small">{r.rule_id}</span>
                  <span className="muted small"> · {r.reason}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <div className="muted small mt">
        证据引用：{rep.evidence_refs.map((e) => `${e.kind}:${e.ref_id}`).join(' · ') || '—'}
      </div>
    </Card>
  )
}

function fmtThreshold(threshold: GateCheck['threshold']): string {
  if (!threshold || threshold.value === undefined || threshold.value === null) {
    return threshold?.unit ?? '—'
  }
  return `${String(threshold.value)}${threshold.unit ? ' ' + threshold.unit : ''}`
}

function QualityGateSection(props: { report: QualityGateReport }) {
  const navigate = useNavigate()
  const rep = props.report
  const checks = rep.checks ?? []
  const triggered = rep.triggered_rules ?? []
  return (
    <Card
      title="质量门禁（CI）"
      action={<span className="badge muted">配置 {rep.config.fingerprint}</span>}
    >
      <div className={`rx-banner ${gateTone(rep.gate_status)}`}>
        <div className="row rx-banner-head">
          <span className={`badge ${gateTone(rep.gate_status)}`}>门禁 {rep.gate_status}</span>
          <span className="badge muted">退出码 {rep.exit_code}</span>
          <span className="mono small">CI Run ID：{rep.ci_run_id}</span>
          <span className="mono small">commit {rep.git_commit}</span>
        </div>
        <p>
          <b>门禁判定：</b>
          {rep.gate_reason}
        </p>
        <p className="muted small">
          关联评测：基线 {rep.baseline_version.evaluation_run_id} / 候选{' '}
          {rep.candidate_version.evaluation_run_id} · 数据集 {rep.dataset_version ?? '—'} · 评测器{' '}
          {rep.evaluator_version ?? '—'} · 对比 {rep.comparison_id} · 回归{' '}
          {rep.regression_report_id}
        </p>
        <p className="muted small">
          阈值来源：{rep.config.label}。WARN 默认退出码 0（仅告警）。
        </p>
        {rep.advisory_regression_rules.length > 0 && (
          <p className="muted small">
            非阻断回归告警（不改变门禁状态）：
            {rep.advisory_regression_rules.map((r) => r.rule_id).join('、')}
          </p>
        )}
      </div>

      <h4 className="vc-h4">触发规则（{triggered.length}）</h4>
      {triggered.length === 0 ? (
        <div className="muted small">无触发规则</div>
      ) : (
        <div className="vc-table-wrap">
          <table className="tbl vc-gate-rules">
            <thead>
              <tr>
                <th>规则</th>
                <th>状态</th>
                <th>严重度</th>
                <th>阈值</th>
                <th>实际指标</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              {triggered.map((r) => (
                <tr key={`${r.check_id}-${r.rule_id}`}>
                  <td className="mono small">{r.rule_id}</td>
                  <td>
                    <span className={`badge ${gateTone(r.status)}`}>{r.status}</span>
                  </td>
                  <td>
                    <span className={riskTone(r.severity)}>{r.severity}</span>
                  </td>
                  <td className="small">{fmtThreshold(r.threshold)}</td>
                  <td className="small mono">{JSON.stringify(r.actual)}</td>
                  <td className="small">{r.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h4 className="vc-h4">全部检查（{checks.length}）</h4>
      <div className="vc-table-wrap">
        <table className="tbl vc-gate-checks">
          <thead>
            <tr>
              <th>检查项</th>
              <th>类别</th>
              <th>状态</th>
              <th>阈值</th>
              <th>实际指标</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            {checks.map((c) => (
              <tr key={c.check_id}>
                <td className="mono small">{c.check_id}</td>
                <td className="small">{c.category}</td>
                <td>
                  <span className={`badge ${gateTone(c.status)}`}>{c.status}</span>
                </td>
                <td className="small">{fmtThreshold(c.threshold)}</td>
                <td className="small mono">{JSON.stringify(c.actual)}</td>
                <td className="small">{c.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h4 className="vc-h4">证据入口（{rep.evidence_refs.length}）</h4>
      <div className="vc-gate-evidence">
        {rep.evidence_refs.length === 0 && <span className="muted small">无证据引用</span>}
        {rep.evidence_refs.map((ref) => {
          const canOpenTrace = ref.kind === 'case' && (ref.trace_ids ?? []).length > 0
          if (canOpenTrace) {
            return (
              <button
                key={`${ref.kind}-${ref.ref_id}`}
                className="btn vc-tbtn"
                onClick={() => navigate(`/traces/${ref.trace_ids![0]}`)}
                title={ref.detail}
              >
                案例 {ref.ref_id} · Trace
              </button>
            )
          }
          return (
            <span key={`${ref.kind}-${ref.ref_id}`} className="badge muted" title={ref.detail}>
              {ref.kind}:{ref.ref_id}
            </span>
          )
        })}
      </div>

      <details className="mt">
        <summary className="muted small">限制说明（{rep.limitations.length}）</summary>
        <ul className="vc-limits">
          {rep.limitations.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      </details>
    </Card>
  )
}

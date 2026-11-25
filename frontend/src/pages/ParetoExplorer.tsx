import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { RefreshCw, SlidersHorizontal, Trophy } from 'lucide-react'
import { api, apiErrorMessage } from '../api'
import { Card, EmptyState, ErrorBox, Spinner } from '../components/ui'

/**
 * 第 54 步：Interactive Pareto Explorer（成本–延迟–质量）。
 *
 * 左侧：交互约束（数据集/最低成功率/最大延迟/最大成本/安全门禁）；
 * 中部：可旋转三维散点图 + 二维投影切换（移动端友好）；
 * 右侧：候选版本详情（指标/来源/可比性/成本口径/关联资源）；
 * 底部：Pareto Frontier、被支配解、支配关系与排除原因。
 *
 * 诚实性：候选全部来自本地真实评测运行；模拟数据不代表真实模型性能；
 * 缺失指标显示「—」而不是 0；成本为价格表估算，不是 Provider 实际账单。
 */

interface Candidate {
  candidate_id: string
  agent_version: string | null
  agent_version_note?: string | null
  evaluation_run_id: string
  evaluation_created_at?: string | null
  evaluation_elapsed_ms?: number | null
  dataset_id: string | null
  dataset_version: string | null
  dataset_group_key?: string | null
  evaluator_version?: string | null
  judge_mode?: string | null
  task_success_rate: number | null
  average_latency_ms: number | null
  average_cost: number | null
  total_cost?: number | null
  currency: string | null
  cost_status?: string | null
  cost_note?: string | null
  cost_error?: string | null
  pricing_versions?: string[]
  sample_size: number | null
  data_source: string
  execution_mode?: string | null
  paid_api_called?: boolean
  safety_pass_rate: number | null
  safety_gate_status: string | null
  gate_report_id?: string | null
  missing_fields: string[]
  metrics_complete: boolean
  trace_ids: string[]
  trace_count?: number
  comparability_status: string
  comparability_issues: { code: string; severity?: string; message: string }[]
  in_comparison_group: boolean
  exclusion_reasons: string[]
  eligible: boolean
  dominance: string
  dominated_by: string[]
  dominates: string[]
  tie_group: string | null
  frontier_rank?: number
}

interface ComparableIssue {
  code: string
  severity: string
  field?: string
  message: string
  candidate_ids?: string[]
}

interface Comparability {
  verdict: string
  comparable: boolean
  group_key?: string
  group_dataset_id?: string
  group_dataset_version?: string
  group_candidate_count?: number
  outside_group_candidate_count?: number
  dataset_groups?: { group_key: string; dataset_id: string; dataset_version: string; candidate_count: number }[]
  issues: ComparableIssue[]
  critical_issues: ComparableIssue[]
  warning_issues: ComparableIssue[]
  mixed_real_and_simulated?: boolean
  real_candidate_count?: number
  simulated_candidate_count?: number
  minimum_sample_size?: number
  notes?: string[]
}

interface DominanceRelation {
  dominator_id: string
  dominated_id: string
  strict_axes: string[]
  margins: { quality: number; latency_ms: number; cost: number }
  definition: string
}

interface Analysis {
  analysis_id: string
  generated_at: string
  status: string
  status_message: string
  candidate_count: number
  feasible_count: number
  comparable_candidate_count?: number
  constraints: Record<string, number | string | null>
  constraints_applied?: Record<string, number | string>
  candidates: Candidate[]
  frontier: Candidate[]
  dominated: Candidate[]
  dominance_relations: DominanceRelation[]
  tie_groups: { tie_group: string; candidate_ids: string[]; metrics: Record<string, number>; note: string }[]
  pareto_frontier_count: number
  dominated_count: number
  comparability: Comparability
  datasets?: { group_key: string; dataset_id: string; dataset_version: string; candidate_count: number }[]
  data_source_summary?: Record<string, number>
  warnings: string[]
  limitations: string[]
  report_path?: string
  metric_definitions: Record<string, { field: string; direction: string; label: string; definition: string }>
  objective: { quality: string; latency: string; cost: string; definition: string }
  paid_api_called: boolean
  view_of?: string
}

interface CandidateList {
  candidates: Candidate[]
  datasets: { group_key: string; dataset_id: string; dataset_version: string; candidate_count: number }[]
  data_sources: Record<string, number>
  notes: string[]
}

interface EvaluationDetail {
  evaluation_run_id: string
  status: string
  dataset_id: string
  dataset_version: string
  created_at: string
  elapsed_ms: number | null
  total_cases: number
  executed_cases: number
  metrics: {
    task_success_rate: { value: number | null; numerator: number; denominator: number } | null
    safety_pass_rate: { value: number | null; numerator: number; denominator: number } | null
    average_duration_ms: number | null
  }
  trace_ids: string[]
}

type Projection = '3d' | 'quality-latency' | 'quality-cost' | 'latency-cost'

const PROJECTIONS: { value: Projection; label: string; hint: string }[] = [
  { value: '3d', label: '三维散点', hint: '质量 / 延迟 / 成本' },
  { value: 'quality-latency', label: '质量-延迟', hint: '二维投影' },
  { value: 'quality-cost', label: '质量-成本', hint: '二维投影' },
  { value: 'latency-cost', label: '延迟-成本', hint: '二维投影' },
]

const SAFETY_OPTIONS = ['NOT_REQUIRED', 'PASS', 'WARN', 'INCONCLUSIVE', 'BLOCK']

function fmtPct(value: number | null | undefined): string {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`
}

function fmtMs(value: number | null | undefined): string {
  return value == null ? '—' : `${Math.round(value)} ms`
}

function fmtAmount(value: number | null | undefined, currency: string | null | undefined): string {
  if (value == null) return '—'
  return `${currency ?? ''} ${value.toFixed(8)}`.trim()
}

function verdictTone(verdict: string | undefined): string {
  if (verdict === 'COMPARABLE') return 'success'
  if (verdict === 'COMPARABLE_WITH_WARNINGS') return 'partial'
  return 'failed'
}

function candidateTone(c: Candidate): string {
  if (c.dominance === 'NON_DOMINATED') return '#0f766e'
  if (c.dominance === 'DOMINATED') return '#94a3b8'
  return '#cbd5e1'
}

function datasetLabel(group: { dataset_id: string; dataset_version: string; candidate_count: number }): string {
  return `${group.dataset_id} · v${group.dataset_version} · ${group.candidate_count} 个候选`
}

interface PlotPoint {
  candidate: Candidate
  x: number
  y: number
  z: number
}

/** 归一化到 0..1（缺失指标不参与绘图；不把 null 当 0）。 */
function normalize(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return 0
  if (max <= min) return 0.5
  return (value - min) / (max - min)
}

export default function ParetoExplorer() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [candidateList, setCandidateList] = useState<CandidateList | null>(null)
  const [report, setReport] = useState<Analysis | null>(null)
  const [view, setView] = useState<Analysis | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [projection, setProjection] = useState<Projection>('3d')
  const [yaw, setYaw] = useState(38)
  const [pitch, setPitch] = useState(26)
  const [hover, setHover] = useState<{ candidate: Candidate; x: number; y: number } | null>(null)
  const [evalDetail, setEvalDetail] = useState<EvaluationDetail | null>(null)
  const [detailBusy, setDetailBusy] = useState(false)
  const scatterRef = useRef<HTMLDivElement | null>(null)
  const dragRef = useRef<{ x: number; y: number } | null>(null)
  // 请求序号：避免 React StrictMode 双执行 / 慢响应覆盖用户刚应用的约束结果
  const requestSeq = useRef(0)

  const [constraints, setConstraints] = useState<{
    dataset_version: string
    minimum_quality: string
    maximum_latency_ms: string
    maximum_cost: string
    required_safety_gate: string
  }>({
    dataset_version: '',
    minimum_quality: '',
    maximum_latency_ms: '',
    maximum_cost: '',
    required_safety_gate: 'NOT_REQUIRED',
  })

  const active = view ?? report
  const activeCandidates = active?.candidates ?? []
  const frontier = active?.frontier ?? []
  const dominated = active?.dominated ?? []
  const relations = active?.dominance_relations ?? []
  const comparability = active?.comparability ?? report?.comparability ?? null

  const selectedCandidate = useMemo(() => {
    if (!activeCandidates.length) return null
    return activeCandidates.find((c) => c.candidate_id === selectedId) ?? activeCandidates[0]
  }, [activeCandidates, selectedId])

  const plotCandidates = useMemo(
    () =>
      activeCandidates.filter(
        (c) =>
          c.task_success_rate != null &&
          c.average_latency_ms != null &&
          c.average_cost != null,
      ),
    [activeCandidates],
  )

  const constraintsPayload = useCallback(() => {
    const payload: Record<string, number | string> = {}
    const quality = Number(constraints.minimum_quality)
    if (constraints.minimum_quality !== '' && Number.isFinite(quality)) {
      payload.minimum_quality = quality
    }
    const latency = Number(constraints.maximum_latency_ms)
    if (constraints.maximum_latency_ms !== '' && Number.isFinite(latency)) {
      payload.maximum_latency_ms = latency
    }
    const cost = Number(constraints.maximum_cost)
    if (constraints.maximum_cost !== '' && Number.isFinite(cost)) {
      payload.maximum_cost = cost
    }
    if (constraints.required_safety_gate !== 'NOT_REQUIRED') {
      payload.required_safety_gate = constraints.required_safety_gate
    }
    if (constraints.dataset_version) {
      payload.dataset_version = constraints.dataset_version
    }
    return payload
  }, [constraints])

  const load = useCallback(async () => {
    const seq = ++requestSeq.current
    setLoading(true)
    setError(null)
    try {
      const list = await api.get<CandidateList>('/api/pareto/candidates', { timeout: 120000 })
      if (seq !== requestSeq.current) return
      setCandidateList(list.data)
      const group = list.data.datasets[0]
      const nextConstraints = {
        dataset_version: group?.group_key ?? '',
        minimum_quality: '',
        maximum_latency_ms: '',
        maximum_cost: '',
        required_safety_gate: 'NOT_REQUIRED',
      }
      setConstraints(nextConstraints)
      const created = await api.post<Analysis>(
        '/api/pareto/analyses',
        {
          constraints: group ? { dataset_version: group.group_key } : {},
          min_sample_size: 3,
        },
        { timeout: 120000 },
      )
      if (seq !== requestSeq.current) return
      setReport(created.data)
      setView(null)
      setSelectedId(created.data.candidates[0]?.candidate_id ?? null)
    } catch (e) {
      if (seq === requestSeq.current) setError(apiErrorMessage(e))
    } finally {
      if (seq === requestSeq.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  async function applyConstraints(createNew: boolean) {
    if (!report) return
    const seq = ++requestSeq.current
    setBusy(true)
    setError(null)
    try {
      const payload = constraintsPayload()
      if (createNew) {
        const created = await api.post<Analysis>(
          '/api/pareto/analyses',
          { constraints: payload, min_sample_size: 3 },
          { timeout: 120000 },
        )
        if (seq !== requestSeq.current) return
        setReport(created.data)
        setView(null)
        setSelectedId(created.data.candidates[0]?.candidate_id ?? null)
      } else {
        const filtered = await api.post<Analysis>(
          `/api/pareto/analyses/${report.analysis_id}/filter`,
          payload,
          { timeout: 120000 },
        )
        if (seq !== requestSeq.current) return
        setView(filtered.data)
        setSelectedId(filtered.data.candidates[0]?.candidate_id ?? null)
      }
    } catch (e) {
      if (seq === requestSeq.current) setError(apiErrorMessage(e))
    } finally {
      if (seq === requestSeq.current) setBusy(false)
    }
  }

  async function resetConstraints() {
    const group = report?.comparability?.group_key ?? candidateList?.datasets[0]?.group_key ?? ''
    setConstraints({
      dataset_version: group,
      minimum_quality: '',
      maximum_latency_ms: '',
      maximum_cost: '',
      required_safety_gate: 'NOT_REQUIRED',
    })
    if (!report) return
    const seq = ++requestSeq.current
    try {
      const response = await api.post<Analysis>(
        `/api/pareto/analyses/${report.analysis_id}/filter`,
        group ? { dataset_version: group } : {},
        { timeout: 120000 },
      )
      if (seq !== requestSeq.current) return
      setView(response.data)
      setSelectedId(response.data.candidates[0]?.candidate_id ?? null)
      setError(null)
    } catch (e) {
      if (seq === requestSeq.current) setError(apiErrorMessage(e))
    }
  }

  async function loadEvaluationDetail(candidate: Candidate) {
    if (!report) return
    setDetailBusy(true)
    setEvalDetail(null)
    try {
      const r = await api.get<EvaluationDetail>(
        `/api/pareto/analyses/${report.analysis_id}/candidates/${candidate.candidate_id}/evaluation`,
        { timeout: 60000 },
      )
      setEvalDetail(r.data)
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setDetailBusy(false)
    }
  }

  useEffect(() => {
    setEvalDetail(null)
  }, [selectedCandidate?.candidate_id])

  // ---------- 投影计算 ----------
  const domain = useMemo(() => {
    const latencies = plotCandidates.map((c) => c.average_latency_ms as number)
    const costs = plotCandidates.map((c) => c.average_cost as number)
    const span = (values: number[], padRatio = 0.08) => {
      if (!values.length) return [0, 1] as const
      const min = Math.min(...values)
      const max = Math.max(...values)
      if (max === min) return [Math.max(0, min - 0.5), min + 0.5] as const
      const pad = (max - min) * padRatio
      return [min - pad, max + pad] as const
    }
    return {
      quality: [0, 1] as const,
      latency: span(latencies),
      cost: span(costs),
    }
  }, [plotCandidates])

  const points: PlotPoint[] = useMemo(() => {
    const cy = Math.cos((yaw * Math.PI) / 180)
    const sy = Math.sin((yaw * Math.PI) / 180)
    const cp = Math.cos((pitch * Math.PI) / 180)
    const sp = Math.sin((pitch * Math.PI) / 180)
    const qn = (v: number) => normalize(v, domain.quality[0], domain.quality[1])
    const ln = (v: number) => normalize(v, domain.latency[0], domain.latency[1])
    const cn = (v: number) => normalize(v, domain.cost[0], domain.cost[1])
    return plotCandidates.map((candidate) => {
      const quality = qn(candidate.task_success_rate as number)
      const latency = ln(candidate.average_latency_ms as number)
      const cost = cn(candidate.average_cost as number)
      if (projection === 'quality-latency') {
        return { candidate, x: 60 + quality * 500, y: 360 - quality * 0, z: 0 }
      }
      if (projection === 'quality-cost') {
        return { candidate, x: 60 + quality * 500, y: 360 - cost * 280, z: 0 }
      }
      if (projection === 'latency-cost') {
        return { candidate, x: 60 + latency * 500, y: 360 - cost * 280, z: 0 }
      }
      const x = latency * cy - cost * sy
      const y = (latency * sy + cost * cy) * sp - quality * cp
      return { candidate, x: 330 + x * 210, y: 235 + y * 210, z: 0 }
    })
  }, [plotCandidates, projection, yaw, pitch, domain])

  const xyPoints = useMemo(() => {
    if (projection === 'quality-latency') {
      return plotCandidates.map((c) => ({
        candidate: c,
        x: 70 + normalize(c.task_success_rate as number, 0, 1) * 480,
        y: 350 - normalize(c.average_latency_ms as number, domain.latency[0], domain.latency[1]) * 260,
      }))
    }
    if (projection === 'quality-cost') {
      return plotCandidates.map((c) => ({
        candidate: c,
        x: 70 + normalize(c.task_success_rate as number, 0, 1) * 480,
        y: 350 - normalize(c.average_cost as number, domain.cost[0], domain.cost[1]) * 260,
      }))
    }
    return plotCandidates.map((c) => ({
      candidate: c,
      x: 70 + normalize(c.average_latency_ms as number, domain.latency[0], domain.latency[1]) * 480,
      y: 350 - normalize(c.average_cost as number, domain.cost[0], domain.cost[1]) * 260,
    }))
  }, [plotCandidates, projection, domain])

  const frontierLine = useMemo(() => {
    const ids = new Set(frontier.map((c) => c.candidate_id))
    return xyPoints
      .filter((p) => ids.has(p.candidate.candidate_id))
      .sort((a, b) => a.x - b.x)
  }, [xyPoints, frontier])

  function onPointerDown(e: React.PointerEvent<SVGSVGElement>) {
    if (projection !== '3d') return
    dragRef.current = { x: e.clientX, y: e.clientY }
    e.currentTarget.setPointerCapture(e.pointerId)
  }

  function onPointerMove(e: React.PointerEvent<SVGSVGElement>) {
    if (!dragRef.current || projection !== '3d') return
    const dx = e.clientX - dragRef.current.x
    const dy = e.clientY - dragRef.current.y
    dragRef.current = { x: e.clientX, y: e.clientY }
    setYaw((prev) => (prev + dx * 0.5 + 360) % 360)
    setPitch((prev) => Math.max(8, Math.min(64, prev + dy * 0.3)))
  }

  function onPointerUp() {
    dragRef.current = null
  }

  function onDotEnter(candidate: Candidate, e: React.MouseEvent) {
    const rect = scatterRef.current?.getBoundingClientRect()
    if (!rect) return
    setHover({ candidate, x: e.clientX - rect.left, y: e.clientY - rect.top })
  }

  function onDotMove(candidate: Candidate, e: React.MouseEvent) {
    const rect = scatterRef.current?.getBoundingClientRect()
    if (!rect) return
    setHover({ candidate, x: e.clientX - rect.left, y: e.clientY - rect.top })
  }

  function axisTitle(): { x: string; y: string } {
    if (projection === 'quality-latency') return { x: '质量（越高越好）', y: '延迟（越低越好）' }
    if (projection === 'quality-cost') return { x: '质量（越高越好）', y: '成本（越低越好）' }
    if (projection === 'latency-cost') return { x: '延迟（越低越好）', y: '成本（越低越好）' }
    return { x: '延迟', y: '成本' }
  }

  const projectionTitle = PROJECTIONS.find((p) => p.value === projection)?.label ?? ''

  return (
    <div className="stack">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <h2 style={{ margin: 0 }}>Pareto Explorer · 成本–延迟–质量</h2>
          <div className="muted small">
            质量越高、延迟与成本越低越好；展示非支配解与并列候选。
          </div>
        </div>
        <div className="row">
          <span className="badge blue">本地确定性数据 · 付费 API 0 次</span>
          <button className="btn" onClick={() => void load()} disabled={loading || busy}>
            <RefreshCw size={14} /> 重新加载候选
          </button>
        </div>
      </div>

      {error && <ErrorBox message={error} />}
      {loading && !report && <Spinner />}

      {report && (
        <>
          {active?.status === 'NO_FEASIBLE_CANDIDATE' && (
            <div className="pareto-status warn">
              <strong>NO_FEASIBLE_CANDIDATE</strong> · {active.status_message}
            </div>
          )}
          {active?.status === 'NO_COMPARABLE_CANDIDATE' && (
            <div className="pareto-status warn">
              <strong>NO_COMPARABLE_CANDIDATE</strong> · {active.status_message}
            </div>
          )}
          {active?.status === 'INSUFFICIENT_DATA' && (
            <div className="pareto-status warn">
              <strong>INSUFFICIENT_DATA</strong> · {active.status_message}
            </div>
          )}
          {active?.status === 'NO_CANDIDATE' && (
            <div className="pareto-status warn">
              <strong>NO_CANDIDATE</strong> · 暂无评测候选，请先到 Evaluation Center 运行评测
            </div>
          )}

          <div className="pareto-kpis">
            <div className="card stat">
              <div className="k">候选版本</div>
              <div className="v">{active?.candidate_count ?? 0}</div>
              <div className="muted small">
                满足约束 {active?.feasible_count ?? 0} · 可比{' '}
                {active?.comparable_candidate_count ?? 0}
              </div>
            </div>
            <div className="card stat">
              <div className="k">非支配解</div>
              <div className="v">{active?.pareto_frontier_count ?? 0}</div>
              <div className="muted small">Pareto Frontier</div>
            </div>
            <div className="card stat">
              <div className="k">被支配解</div>
              <div className="v">{active?.dominated_count ?? 0}</div>
              <div className="muted small">存在更优候选</div>
            </div>
            <div className="card stat">
              <div className="k">支配关系</div>
              <div className="v">{relations.length}</div>
              <div className="muted small">严格更优至少一维</div>
            </div>
          </div>

          {comparability && comparability.group_key && (
            <div className={`pareto-compare ${verdictTone(comparability.verdict)}`}>
              <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
                <strong>可比性：{comparability.verdict}</strong>
                <span className="badge muted">
                  数据集 {comparability.group_dataset_id} · v{comparability.group_dataset_version}
                </span>
                <span className="badge muted">
                  模拟 {comparability.simulated_candidate_count ?? 0} · 真实{' '}
                  {comparability.real_candidate_count ?? 0}
                </span>
                {comparability.mixed_real_and_simulated && (
                  <span className="badge failed">含模拟数据，不可直接比较模型性能</span>
                )}
              </div>
              {comparability.issues.length > 0 && (
                <ul className="pareto-issues">
                  {comparability.issues.map((issue) => (
                    <li key={issue.code + issue.message} className={`sev-${issue.severity}`}>
                      <span className="mono small">{issue.code}</span> · {issue.message}
                    </li>
                  ))}
                </ul>
              )}
              <div className="muted small">
                候选来自本地评测运行；模拟数据不代表真实模型性能；成本为估算值。
              </div>
            </div>
          )}

          <div className="pareto-layout">
            {/* 左：约束控制面板 */}
            <Card title="约束控制" className="pareto-controls">
              <div className="pareto-field">
                <label htmlFor="pareto-dataset">评测数据集</label>
                <select
                  id="pareto-dataset"
                  value={constraints.dataset_version}
                  onChange={(e) =>
                    setConstraints((prev) => ({ ...prev, dataset_version: e.target.value }))
                  }
                >
                  <option value="">全部数据集（自动选最大可比组）</option>
                  {(candidateList?.datasets ?? report.datasets ?? []).map((group) => (
                    <option key={group.group_key} value={group.group_key}>
                      {datasetLabel(group)}
                    </option>
                  ))}
                </select>
              </div>
              <div className="pareto-field">
                <label htmlFor="pareto-quality">最低成功率</label>
                <input
                  id="pareto-quality"
                  type="number"
                  min={0}
                  max={1}
                  step={0.01}
                  placeholder="例如 0.80"
                  value={constraints.minimum_quality}
                  onChange={(e) =>
                    setConstraints((prev) => ({ ...prev, minimum_quality: e.target.value }))
                  }
                />
                <span className="muted small">0..1；留空表示不限制</span>
              </div>
              <div className="pareto-field">
                <label htmlFor="pareto-latency">最大延迟（ms）</label>
                <input
                  id="pareto-latency"
                  type="number"
                  min={0}
                  step={10}
                  placeholder="例如 500"
                  value={constraints.maximum_latency_ms}
                  onChange={(e) =>
                    setConstraints((prev) => ({ ...prev, maximum_latency_ms: e.target.value }))
                  }
                />
              </div>
              <div className="pareto-field">
                <label htmlFor="pareto-cost">最大平均成本</label>
                <input
                  id="pareto-cost"
                  type="number"
                  min={0}
                  step={0.000001}
                  placeholder="例如 0.01"
                  value={constraints.maximum_cost}
                  onChange={(e) =>
                    setConstraints((prev) => ({ ...prev, maximum_cost: e.target.value }))
                  }
                />
                <span className="muted small">成本为价格表估算，不是实际账单</span>
              </div>
              <div className="pareto-field">
                <label htmlFor="pareto-safety">安全门禁</label>
                <select
                  id="pareto-safety"
                  value={constraints.required_safety_gate}
                  onChange={(e) =>
                    setConstraints((prev) => ({
                      ...prev,
                      required_safety_gate: e.target.value,
                    }))
                  }
                >
                  {SAFETY_OPTIONS.map((option) => (
                    <option key={option} value={option}>
                      {option === 'NOT_REQUIRED' ? '不限制' : `至少 ${option}`}
                    </option>
                  ))}
                </select>
                <span className="muted small">无门禁记录的候选不满足门禁筛选</span>
              </div>
              <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginTop: 10 }}>
                <button
                  className="btn primary"
                  onClick={() => void applyConstraints(false)}
                  disabled={busy}
                >
                  <SlidersHorizontal size={14} /> 应用约束
                </button>
                <button className="btn" onClick={() => void applyConstraints(true)} disabled={busy}>
                  生成新分析
                </button>
                <button className="btn" onClick={resetConstraints} disabled={busy}>
                  重置
                </button>
              </div>
              {busy && <div className="muted small">重新计算帕累托前沿…</div>}
              <div className="pareto-sources">
                <div className="muted small">数据来源</div>
                {Object.entries(active?.data_source_summary ?? candidateList?.data_sources ?? {}).map(
                  ([key, value]) => (
                    <span key={key} className="badge muted">
                      {key} {value}
                    </span>
                  ),
                )}
                <div className="muted small" style={{ marginTop: 6 }}>
                  缺失指标显示「—」；不可比候选不进入前沿。
                </div>
              </div>
            </Card>

            {/* 中：散点图 */}
            <Card
              title={`交互式散点图 · ${projectionTitle}`}
              action={
                <div className="row" style={{ gap: 4, flexWrap: 'wrap' }}>
                  {PROJECTIONS.map((item) => (
                    <button
                      key={item.value}
                      className={`btn small ${projection === item.value ? 'primary' : ''}`}
                      onClick={() => setProjection(item.value)}
                      title={item.hint}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
              }
            >
              <div className="pareto-scatter" ref={scatterRef}>
                {plotCandidates.length === 0 ? (
                  <EmptyState text="没有同时具备质量/延迟/成本的候选，无法绘图（缺失指标不按 0 计）" />
                ) : (
                  <svg
                    viewBox="0 0 640 420"
                    role="img"
                    aria-label={`帕累托散点图：${projectionTitle}`}
                    onPointerDown={onPointerDown}
                    onPointerMove={onPointerMove}
                    onPointerUp={onPointerUp}
                    onPointerLeave={() => {
                      onPointerUp()
                      setHover(null)
                    }}
                  >
                    <rect x="0" y="0" width="640" height="420" fill="#ffffff" />
                    {projection === '3d' ? (
                      <>
                        {(() => {
                          const cy = Math.cos((yaw * Math.PI) / 180)
                          const sy = Math.sin((yaw * Math.PI) / 180)
                          const cp = Math.cos((pitch * Math.PI) / 180)
                          const sp = Math.sin((pitch * Math.PI) / 180)
                          const project = (latency: number, cost: number, quality: number) => {
                            const x = latency * cy - cost * sy
                            const y = (latency * sy + cost * cy) * sp - quality * cp
                            return [330 + x * 210, 235 + y * 210] as const
                          }
                          const origin = project(0, 0, 0)
                          const xAxis = project(1, 0, 0)
                          const yAxis = project(0, 1, 0)
                          const zAxis = project(0, 0, 1)
                          return (
                            <>
                              <line x1={origin[0]} y1={origin[1]} x2={xAxis[0]} y2={xAxis[1]} stroke="#cbd5e1" />
                              <line x1={origin[0]} y1={origin[1]} x2={yAxis[0]} y2={yAxis[1]} stroke="#cbd5e1" />
                              <line x1={origin[0]} y1={origin[1]} x2={zAxis[0]} y2={zAxis[1]} stroke="#94a3b8" />
                              <text x={xAxis[0] + 6} y={xAxis[1] + 14} className="pareto-axis-label">
                                延迟 →
                              </text>
                              <text x={yAxis[0] + 6} y={yAxis[1] + 14} className="pareto-axis-label">
                                成本 →
                              </text>
                              <text x={zAxis[0] + 4} y={zAxis[1] - 6} className="pareto-axis-label">
                                质量 ↑
                              </text>
                            </>
                          )
                        })()}
                        {points.map((point) => (
                          <circle
                            key={point.candidate.candidate_id}
                            cx={point.x}
                            cy={point.y}
                            r={point.candidate.dominance === 'NON_DOMINATED' ? 7 : 5.5}
                            fill={candidateTone(point.candidate)}
                            opacity={
                              selectedCandidate?.candidate_id === point.candidate.candidate_id
                                ? 1
                                : 0.85
                            }
                            stroke={
                              selectedCandidate?.candidate_id === point.candidate.candidate_id
                                ? '#1d4ed8'
                                : '#ffffff'
                            }
                            strokeWidth={selectedCandidate?.candidate_id === point.candidate.candidate_id ? 3 : 1.5}
                            className="pareto-dot"
                            onMouseEnter={(e) => onDotEnter(point.candidate, e)}
                            onMouseMove={(e) => onDotMove(point.candidate, e)}
                            onClick={() => setSelectedId(point.candidate.candidate_id)}
                          >
                            <title>
                              {point.candidate.candidate_id} · 质量 {fmtPct(point.candidate.task_success_rate)} · 延迟{' '}
                              {fmtMs(point.candidate.average_latency_ms)} · 成本{' '}
                              {fmtAmount(point.candidate.average_cost, point.candidate.currency)}
                            </title>
                          </circle>
                        ))}
                        <text x="16" y="404" className="pareto-hint">
                          拖拽旋转 · 悬停查看指标 · 点击选中候选
                        </text>
                      </>
                    ) : (
                      <>
                        <line x1="70" y1="350" x2="550" y2="350" stroke="#cbd5e1" />
                        <line x1="70" y1="90" x2="70" y2="350" stroke="#cbd5e1" />
                        <text x="300" y="392" className="pareto-axis-label">
                          {axisTitle().x} →
                        </text>
                        <text x="16" y="230" transform="rotate(-90 16 230)" className="pareto-axis-label">
                          {axisTitle().y} ↑
                        </text>
                        {frontierLine.length > 1 && (
                          <polyline
                            points={frontierLine.map((p) => `${p.x},${p.y}`).join(' ')}
                            fill="none"
                            stroke="#0f766e"
                            strokeDasharray="6 4"
                            strokeWidth="1.5"
                            opacity="0.6"
                          />
                        )}
                        {xyPoints.map((point) => (
                          <circle
                            key={point.candidate.candidate_id}
                            cx={point.x}
                            cy={point.y}
                            r={point.candidate.dominance === 'NON_DOMINATED' ? 8 : 6}
                            fill={candidateTone(point.candidate)}
                            stroke={
                              selectedCandidate?.candidate_id === point.candidate.candidate_id
                                ? '#1d4ed8'
                                : '#ffffff'
                            }
                            strokeWidth={selectedCandidate?.candidate_id === point.candidate.candidate_id ? 3 : 1.5}
                            className="pareto-dot"
                            onMouseEnter={(e) => onDotEnter(point.candidate, e)}
                            onMouseMove={(e) => onDotMove(point.candidate, e)}
                            onClick={() => setSelectedId(point.candidate.candidate_id)}
                          >
                            <title>
                              {point.candidate.candidate_id} · 质量 {fmtPct(point.candidate.task_success_rate)} · 延迟{' '}
                              {fmtMs(point.candidate.average_latency_ms)} · 成本{' '}
                              {fmtAmount(point.candidate.average_cost, point.candidate.currency)}
                            </title>
                          </circle>
                        ))}
                      </>
                    )}
                  </svg>
                )}
                <div className="pareto-legend">
                  <span><i style={{ background: '#0f766e' }} />非支配解（Frontier）</span>
                  <span><i style={{ background: '#94a3b8' }} />被支配解</span>
                  <span><i style={{ background: '#cbd5e1' }} />受约束排除</span>
                </div>
                {hover && (
                  <div
                    className="pareto-tooltip"
                    style={{ left: Math.min(hover.x + 14, 520), top: Math.max(8, hover.y - 10) }}
                  >
                    <div className="mono small">{hover.candidate.candidate_id}</div>
                    <div>质量：{fmtPct(hover.candidate.task_success_rate)}</div>
                    <div>延迟：{fmtMs(hover.candidate.average_latency_ms)}</div>
                    <div>成本：{fmtAmount(hover.candidate.average_cost, hover.candidate.currency)}</div>
                    <div className="muted small">{hover.candidate.dominance}</div>
                  </div>
                )}
                {projection === '3d' && (
                  <div className="pareto-rotation">
                    <label htmlFor="pareto-yaw">旋转</label>
                    <input
                      id="pareto-yaw"
                      type="range"
                      min={0}
                      max={360}
                      value={yaw}
                      onChange={(e) => setYaw(Number(e.target.value))}
                    />
                    <label htmlFor="pareto-pitch">俯仰</label>
                    <input
                      id="pareto-pitch"
                      type="range"
                      min={8}
                      max={64}
                      value={pitch}
                      onChange={(e) => setPitch(Number(e.target.value))}
                    />
                  </div>
                )}
              </div>
            </Card>

            {/* 右：候选详情 */}
            <Card
              title="候选版本详情"
              action={<Trophy size={16} color="#0f766e" />}
              className="pareto-detail"
            >
              {!selectedCandidate ? (
                <EmptyState text="点击散点图或下方表格选择候选版本" />
              ) : (
                <div className="stack small">
                  <div className="mono small">{selectedCandidate.candidate_id}</div>
                  <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
                    <span className={`badge ${selectedCandidate.dominance === 'NON_DOMINATED' ? 'success' : 'muted'}`}>
                      {selectedCandidate.dominance}
                    </span>
                    {selectedCandidate.frontier_rank && (
                      <span className="badge blue">Frontier #{selectedCandidate.frontier_rank}</span>
                    )}
                    <span className={`badge ${verdictTone(selectedCandidate.comparability_status)}`}>
                      {selectedCandidate.comparability_status}
                    </span>
                    <span className="badge muted">{selectedCandidate.data_source}</span>
                  </div>
                  <div className="pareto-metric-row">
                    <div><span className="muted small">质量</span><strong>{fmtPct(selectedCandidate.task_success_rate)}</strong></div>
                    <div><span className="muted small">平均延迟</span><strong>{fmtMs(selectedCandidate.average_latency_ms)}</strong></div>
                    <div><span className="muted small">平均成本</span><strong>{fmtAmount(selectedCandidate.average_cost, selectedCandidate.currency)}</strong></div>
                  </div>
                  <dl className="pl-kv">
                    <dt>Agent 版本</dt>
                    <dd>{selectedCandidate.agent_version ?? '未提供'}</dd>
                    <dt>数据集</dt>
                    <dd>{selectedCandidate.dataset_id} · v{selectedCandidate.dataset_version}</dd>
                    <dt>评测器版本</dt>
                    <dd>{selectedCandidate.evaluator_version ?? '—'}</dd>
                    <dt>样本数</dt>
                    <dd>{selectedCandidate.sample_size ?? '—'}</dd>
                    <dt>安全通过率</dt>
                    <dd>{fmtPct(selectedCandidate.safety_pass_rate)}</dd>
                    <dt>安全门禁</dt>
                    <dd>{selectedCandidate.safety_gate_status ?? '—'}</dd>
                    <dt>成本状态</dt>
                    <dd>
                      {selectedCandidate.cost_status ?? '—'}
                      {selectedCandidate.currency ? ` · ${selectedCandidate.currency}` : ''}
                    </dd>
                    <dt>价格版本</dt>
                    <dd>{(selectedCandidate.pricing_versions ?? []).join(' / ') || '—'}</dd>
                    <dt>被支配原因</dt>
                    <dd>{selectedCandidate.dominated_by.length ? selectedCandidate.dominated_by.join('、') : '非支配'}</dd>
                  </dl>
                  {selectedCandidate.exclusion_reasons.length > 0 && (
                    <div className="pareto-warn-box">
                      未进入前沿：{selectedCandidate.exclusion_reasons.join('；')}
                    </div>
                  )}
                  {selectedCandidate.comparability_issues.length > 0 && (
                    <ul className="pareto-issues">
                      {selectedCandidate.comparability_issues.map((issue) => (
                        <li key={issue.code}>{issue.message}</li>
                      ))}
                    </ul>
                  )}
                  <div className="muted small">
                    金额为估算值；PRICE_UNKNOWN 与缺失成本显示「—」。
                  </div>
                  <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
                    <button
                      className="btn small"
                      onClick={() => void loadEvaluationDetail(selectedCandidate)}
                      disabled={detailBusy}
                    >
                      {detailBusy ? '加载中…' : '查看 Evaluation Run'}
                    </button>
                    {selectedCandidate.trace_ids[0] && (
                      <button
                        className="btn small"
                        onClick={() => navigate(`/traces/${selectedCandidate.trace_ids[0]}`)}
                      >
                        查看关联 Trace
                      </button>
                    )}
                  </div>
                  {evalDetail && (
                    <div className="pareto-query-result">
                      <div className="mono small">{evalDetail.evaluation_run_id}</div>
                      <div>状态：{evalDetail.status}</div>
                      <div>案例：执行 {evalDetail.executed_cases} / 总 {evalDetail.total_cases}</div>
                      <div>成功率：{fmtPct(evalDetail.metrics.task_success_rate?.value ?? null)}</div>
                      <div>平均耗时：{fmtMs(evalDetail.metrics.average_duration_ms)}</div>
                      <div>Trace 数：{evalDetail.trace_ids.length}</div>
                    </div>
                  )}
                </div>
              )}
            </Card>
          </div>

          {/* 底部：Frontier + 支配关系 + 排除原因 */}
          <div className="pareto-bottom">
            <Card title={`Pareto Frontier（${frontier.length}）`}>
              {frontier.length === 0 ? (
                <EmptyState text={active?.status === 'NO_FEASIBLE_CANDIDATE' ? 'NO_FEASIBLE_CANDIDATE' : '当前没有非支配解'} />
              ) : (
                <div className="pareto-table-wrap">
                  <table className="tbl pareto-table">
                    <thead>
                      <tr>
                        <th>#</th>
                        <th>候选</th>
                        <th>质量</th>
                        <th>延迟</th>
                        <th>平均成本</th>
                        <th>数据来源</th>
                        <th>安全门禁</th>
                        <th>并列</th>
                        <th>操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {frontier.map((candidate) => (
                        <tr
                          key={candidate.candidate_id}
                          className={selectedCandidate?.candidate_id === candidate.candidate_id ? 'selected' : ''}
                          onClick={() => setSelectedId(candidate.candidate_id)}
                        >
                          <td>{candidate.frontier_rank ?? '—'}</td>
                          <td className="mono small">{candidate.candidate_id}</td>
                          <td>{fmtPct(candidate.task_success_rate)}</td>
                          <td>{fmtMs(candidate.average_latency_ms)}</td>
                          <td>{fmtAmount(candidate.average_cost, candidate.currency)}</td>
                          <td>{candidate.data_source}</td>
                          <td>{candidate.safety_gate_status ?? '—'}</td>
                          <td>{candidate.tie_group ?? '—'}</td>
                          <td>
                            <button
                              className="btn small"
                              onClick={(e) => {
                                e.stopPropagation()
                                setSelectedId(candidate.candidate_id)
                              }}
                            >
                              详情
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {(active?.tie_groups.length ?? 0) > 0 && (
                <div className="muted small">
                  并列组：{active?.tie_groups.map((tie) => tie.candidate_ids.join(' = ')).join('；')}
                  （指标完全相同，互不支配）
                </div>
              )}
            </Card>

            <Card title={`被支配解（${dominated.length}）`}>
              {dominated.length === 0 ? (
                <EmptyState text="没有被支配的候选" />
              ) : (
                <div className="pareto-table-wrap">
                  <table className="tbl pareto-table">
                    <thead>
                      <tr>
                        <th>候选</th>
                        <th>质量</th>
                        <th>延迟</th>
                        <th>成本</th>
                        <th>被谁支配</th>
                      </tr>
                    </thead>
                    <tbody>
                      {dominated.map((candidate) => (
                        <tr
                          key={candidate.candidate_id}
                          className={selectedCandidate?.candidate_id === candidate.candidate_id ? 'selected' : ''}
                          onClick={() => setSelectedId(candidate.candidate_id)}
                        >
                          <td className="mono small">{candidate.candidate_id}</td>
                          <td>{fmtPct(candidate.task_success_rate)}</td>
                          <td>{fmtMs(candidate.average_latency_ms)}</td>
                          <td>{fmtAmount(candidate.average_cost, candidate.currency)}</td>
                          <td className="small">{candidate.dominated_by.join('、')}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          </div>

          <div className="pareto-bottom">
            <Card title={`支配关系（${relations.length}）`}>
              {relations.length === 0 ? (
                <EmptyState text="没有严格支配关系（可能全部并列或只有单一候选）" />
              ) : (
                <div className="pareto-table-wrap">
                  <table className="tbl pareto-table">
                    <thead>
                      <tr>
                        <th>支配者</th>
                        <th>被支配者</th>
                        <th>严格更优维度</th>
                        <th>质量差</th>
                        <th>延迟差</th>
                        <th>成本差</th>
                      </tr>
                    </thead>
                    <tbody>
                      {relations.map((relation) => (
                        <tr key={`${relation.dominator_id}-${relation.dominated_id}`}>
                          <td className="mono small">{relation.dominator_id}</td>
                          <td className="mono small">{relation.dominated_id}</td>
                          <td className="small">{relation.strict_axes.join(' / ')}</td>
                          <td>{relation.margins.quality.toFixed(4)}</td>
                          <td>{relation.margins.latency_ms.toFixed(1)} ms</td>
                          <td>{relation.margins.cost.toFixed(8)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>

            <Card title="排除候选与可比性警告">
              {activeCandidates.filter((c) => !c.metrics_complete || c.comparability_issues.length).length === 0 ? (
                <EmptyState text="没有排除候选；缺失指标不会被当作 0" />
              ) : (
                <div className="pareto-excluded">
                  {activeCandidates
                    .filter((c) => !c.metrics_complete || c.comparability_issues.length)
                    .slice(0, 12)
                    .map((candidate) => (
                      <div key={candidate.candidate_id} className="pareto-excluded-row">
                        <span className="mono small">{candidate.candidate_id}</span>
                        <span className="small">
                          {candidate.comparability_issues.map((issue) => issue.message).join('；') ||
                            candidate.exclusion_reasons.join('；')}
                        </span>
                        <span className="badge muted">缺失 {candidate.missing_fields.join(', ') || '—'}</span>
                      </div>
                    ))}
                </div>
              )}
              {(active?.warnings.length ?? 0) > 0 && (
                <ul className="pareto-issues">
                  {active?.warnings.map((warning) => (
                    <li key={warning} className="sev-WARNING">{warning}</li>
                  ))}
                </ul>
              )}
              <div className="muted small">
                {active?.limitations.join('；')}
              </div>
            </Card>
          </div>
        </>
      )}
    </div>
  )
}

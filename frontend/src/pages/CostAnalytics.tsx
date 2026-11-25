import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Coins, RefreshCw, Search } from 'lucide-react'
import { api, apiErrorMessage } from '../api'
import { Card, EmptyState, ErrorBox, Spinner } from '../components/ui'

/**
 * 第 53 步：Token & Cost Analytics 成本分析工作台。
 *
 * 顶部：Token / 成本 / 延迟核心指标（成本带「非实际账单」声明）；
 * 中部：成本趋势、模型成本分布、延迟分析、价格版本与数据来源；
 * 底部：Cost Attribution Explorer（model_call / trace / agent_run / evaluation / pipeline）
 *        + 单次对象查询 + UNKNOWN / 缺失数据清单。
 *
 * 严禁把估算成本称为实际账单；UNKNOWN / PRICE_UNKNOWN 以 null 展示，不当作 0。
 */

interface Cost {
  amount: number | null
  currency: string | null
  status: string
  complete: boolean
  partial: boolean
  priced_calls: number
  estimated_calls: number
  simulated_calls: number
  price_unknown_calls: number
  unknown_calls: number
  pricing_versions: string[]
  pricing_sources: string[]
  disclaimer?: string
}

interface Overview {
  generated_at: string
  trace_count: number
  model_call_count: number
  source_scope?: {
    selected: string | null
    total_trace_count: number
    filtered_trace_count: number
    available_by_source: Record<string, number>
    source_classes: string[]
    note: string
  }
  tokens: {
    prompt_tokens: number | null
    completion_tokens: number | null
    total_tokens: number | null
    traces_with_known_tokens: number
    usage_sources: Record<string, number>
    unknown_model_calls: number
  }
  cost: Cost & {
    status_counts: Record<string, number>
    traces_with_known_cost: number
  }
  latency: {
    model_latency_avg_ms: number | null
    tool_latency_avg_ms: number | null
    total_run_latency_avg_ms: number | null
    traces_with_known_latency: number
    unknown_fields: string[]
  }
  models: {
    provider: string
    model_name: string
    model_calls: number
    tokens: { prompt_tokens: number; completion_tokens: number; total_tokens: number }
    cost: Cost
    usage_sources: Record<string, number>
  }[]
  recent: {
    trace_id: string
    run_id: string
    started_at: string | null
    provider: string
    model_name: string
    total_tokens: number | null
    cost_amount: number | null
    cost_status: string
    model_latency_ms: number | null
    trace_status: string
  }[]
  unknown: {
    unknown_usage_calls: number
    price_unknown_calls: number
    examples: {
      trace_id: string
      model_call_id: string
      provider: string
      model_name: string
      usage_source: string
      cost_status: string
      reason: string
    }[]
    note: string
  }
  pricing: {
    pricing_version: string
    effective_date: string | null
    pricing_source: string
    currency: string
    path: string
    disclaimer: string
    entries: { provider: string; model_name: string }[]
  }
  versions: {
    agent_version_field_available: boolean
    agent_version_note: string
    by_model: { group: string; trace_count: number; total_tokens: number | null; cost: Cost }[]
    by_execution_mode: { group: string; trace_count: number; total_tokens: number | null; cost: Cost }[]
  }
  task_outcomes: {
    succeeded_traces: number
    failed_traces: number
    priced_successful_traces: number
    avg_cost_per_task: number | null
    avg_cost_per_successful_task: number | null
    failed_task_cost_total: number | null
    note: string
  }
  data_sources: Record<string, unknown>
  paid_api_called: boolean
  limitations: string[]
}

interface AttributionRow {
  level: string
  id: string
  parent_refs: Record<string, unknown>
  provider: string | null
  model_name: string | null
  prompt_tokens: number | null
  completion_tokens: number | null
  total_tokens: number | null
  usage_source: string
  cost: Cost
  latency_ms: number | null
  allocation_basis: string
}

interface Attribution {
  level: string
  usage_source_filter: string | null
  source_class_filter?: string | null
  count: number
  rows: AttributionRow[]
  pricing_version: string
  disclaimer: string
  paid_api_called: boolean
  limitations: string[]
}

/** 数据来源范围（与后端 data_governance.SOURCE_CLASSES 对齐）。 */
const SCOPE_OPTIONS = [
  { value: '', label: '全部来源（全库）' },
  { value: 'REAL_API', label: 'REAL_API（真实 API）' },
  { value: 'MOCK', label: 'MOCK（本地 Mock 执行）' },
  { value: 'SIMULATED', label: 'SIMULATED（合成/模拟）' },
  { value: 'REPLAY', label: 'REPLAY（重放/反事实派生）' },
  { value: 'UNKNOWN', label: 'UNKNOWN（来源未确认）' },
]

const LEVELS = [
  { value: 'model_call', label: '按模型调用' },
  { value: 'trace', label: '按 Trace' },
  { value: 'agent_run', label: '按 Agent Run' },
  { value: 'evaluation', label: '按 Evaluation Run' },
  { value: 'pipeline', label: '按 Pipeline Run' },
]

/** 用量来源过滤（只筛选用量来源，不改变金额口径）；MIXED 仅聚合维度可能出现。 */
const SOURCE_OPTIONS = [
  { value: '', label: '全部用量来源' },
  { value: 'PROVIDER_REPORTED', label: 'PROVIDER_REPORTED' },
  { value: 'ESTIMATED', label: 'ESTIMATED' },
  { value: 'SIMULATED', label: 'SIMULATED' },
  { value: 'UNKNOWN', label: 'UNKNOWN' },
  { value: 'MIXED', label: 'MIXED' },
]

const SOURCE_FILTER_LEVELS = new Set(['model_call', 'trace', 'agent_run'])

const QUERY_KINDS = [
  { value: 'trace', label: 'Trace', path: (id: string) => `/api/cost/traces/${encodeURIComponent(id)}` },
  { value: 'run', label: 'Agent Run', path: (id: string) => `/api/cost/runs/${encodeURIComponent(id)}` },
  { value: 'evaluation', label: 'Evaluation Run', path: (id: string) => `/api/cost/evaluations/${encodeURIComponent(id)}` },
  { value: 'pipeline', label: 'Pipeline Run', path: (id: string) => `/api/cost/pipelines/${encodeURIComponent(id)}` },
]

function statusTone(status: string | undefined): string {
  switch (status) {
    case 'ACTUAL_USAGE_PRICED':
      return 'success'
    case 'ESTIMATED':
      return 'partial'
    case 'SIMULATED':
      return 'muted'
    case 'PRICE_UNKNOWN':
    case 'UNKNOWN':
      return 'failed'
    default:
      return 'muted'
  }
}

function fmtTokens(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'UNKNOWN'
  return value.toLocaleString('en-US')
}

function fmtAmount(amount: number | null | undefined, currency: string | null | undefined): string {
  if (amount === null || amount === undefined) return 'PRICE_UNKNOWN'
  const value = amount < 0.01 ? amount.toFixed(8) : amount.toFixed(4)
  return `${currency ?? ''} ${value}`.trim()
}

/**
 * 成本展示：UNKNOWN / PRICE_UNKNOWN 不显示为 0。
 * 只有状态完整（ACTUAL_USAGE_PRICED / ESTIMATED / SIMULATED）时才展示金额数字。
 */
function fmtCostValue(cost: Cost | null | undefined): string {
  if (!cost) return 'UNKNOWN'
  if (cost.status === 'UNKNOWN') return 'UNKNOWN'
  if (cost.status === 'PRICE_UNKNOWN') return 'PRICE_UNKNOWN'
  return fmtAmount(cost.amount, cost.currency)
}

function fmtMaybeAmount(
  amount: number | null | undefined,
  currency: string | null | undefined,
): string {
  if (amount === null || amount === undefined) return 'UNKNOWN'
  return fmtAmount(amount, currency)
}

function fmtCostDetail(cost: Cost | null | undefined): string {
  if (!cost) return ''
  if (cost.status === 'UNKNOWN' || cost.status === 'PRICE_UNKNOWN') {
    const parts: string[] = []
    if (cost.amount !== null && cost.amount !== undefined) {
      parts.push(`已定价部分 ${fmtAmount(cost.amount, cost.currency)}`)
    }
    if (cost.price_unknown_calls) parts.push(`价格缺失 ${cost.price_unknown_calls} 次`)
    if (cost.unknown_calls) parts.push(`未知用量 ${cost.unknown_calls} 次`)
    return parts.join(' · ') || '金额未知，不按 0 计'
  }
  if (cost.status === 'SIMULATED') {
    return '模拟执行 · 未调用付费 API'
  }
  return ''
}

function fmtMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'UNKNOWN'
  return value < 1000 ? `${value.toFixed(0)} ms` : `${(value / 1000).toFixed(2)} s`
}

export default function CostAnalytics() {
  const navigate = useNavigate()
  const [overview, setOverview] = useState<Overview | null>(null)
  const [attribution, setAttribution] = useState<Attribution | null>(null)
  const [level, setLevel] = useState('model_call')
  const [sourceFilter, setSourceFilter] = useState('')
  const [sourceScope, setSourceScope] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [queryKind, setQueryKind] = useState('trace')
  const [queryId, setQueryId] = useState('')
  const [queryResult, setQueryResult] = useState<Record<string, unknown> | null>(null)
  const [queryError, setQueryError] = useState<string | null>(null)

  const loadOverview = useCallback(async (refresh = false, scopeOverride?: string) => {
    setLoading(true)
    setError(null)
    try {
      const scope = scopeOverride !== undefined ? scopeOverride : sourceScope
      const r = await api.get<Overview>('/api/cost/overview', {
        params: {
          recent: 20,
          refresh,
          source_class: scope || undefined,
        },
        // 首次冷启动需要分析全库 1.6 万 Trace，允许更长超时
        timeout: 120000,
      })
      setOverview(r.data)
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setLoading(false)
    }
  }, [sourceScope])

  const loadAttribution = useCallback(async (nextLevel: string, nextSource = '', nextScope = sourceScope) => {
    try {
      const r = await api.get<Attribution>('/api/cost/attribution', {
        params: {
          level: nextLevel,
          limit: 50,
          usage_source: nextSource || undefined,
          // 来源筛选只对基于 Trace 分析的维度生效；评测/流水线维度不套用
          source_class:
            nextScope && SOURCE_FILTER_LEVELS.has(nextLevel) ? nextScope : undefined,
        },
        timeout: 120000,
      })
      setAttribution(r.data)
    } catch (e) {
      setError(apiErrorMessage(e))
    }
  }, [sourceScope])

  useEffect(() => {
    // 仅在挂载时初始化；来源范围变化由 changeScope 显式触发，避免重复请求
    void loadOverview()
    void loadAttribution('model_call')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function changeLevel(next: string) {
    setLevel(next)
    setSourceFilter('')
    await loadAttribution(next, '')
  }

  async function changeSource(next: string) {
    setSourceFilter(next)
    await loadAttribution(level, next)
  }

  async function changeScope(next: string) {
    setSourceScope(next)
    setAttribution(null)
    await Promise.all([loadOverview(false, next), loadAttribution(level, sourceFilter, next)])
  }

  async function runQuery() {
    const kind = QUERY_KINDS.find((item) => item.value === queryKind)
    if (!kind || !queryId.trim()) {
      setQueryError('请输入要查询的对象 ID')
      return
    }
    setBusy(true)
    setQueryError(null)
    setQueryResult(null)
    try {
      const r = await api.get<Record<string, unknown>>(kind.path(queryId.trim()), { timeout: 60000 })
      setQueryResult(r.data)
    } catch (e) {
      setQueryError(apiErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  const topToken = overview?.tokens.total_tokens
  const promptTokens = overview?.tokens.prompt_tokens
  const completionTokens = overview?.tokens.completion_tokens
  const cost = overview?.cost
  const latency = overview?.latency

  return (
    <div className="stack">
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <h2 style={{ margin: 0 }}>Cost Analytics · Token 与成本分析</h2>
          <div className="muted small">
            Token 用量、价格表成本估算、延迟与成本归因；含 UNKNOWN 与缺失数据。
          </div>
        </div>
        <div className="row">
          <label className="small muted" htmlFor="ca-scope">数据来源</label>
          <select
            id="ca-scope"
            value={sourceScope}
            onChange={(e) => void changeScope(e.target.value)}
          >
            {SCOPE_OPTIONS.map((item) => (
              <option key={item.value || 'ALL'} value={item.value}>{item.label}</option>
            ))}
          </select>
          <span className="badge blue">成本估算 ≠ 实际账单</span>
          <button className="btn" onClick={() => void loadOverview(true)} disabled={loading}>
            <RefreshCw size={14} /> 刷新数据
          </button>
        </div>
      </div>

      {overview?.source_scope && (
        <div className={`ca-scope-banner ${sourceScope ? 'active' : ''}`}>
          <strong>统计范围：</strong>
          当前范围 <code>{sourceScope || '全部来源'}</code> · 命中{' '}
          {overview.source_scope.filtered_trace_count} / 全库{' '}
          {overview.source_scope.total_trace_count} 条 Trace（全库 = 主 Trace 目录 + 评测 Trace 目录）。
          {sourceScope === 'REAL_API' && overview.trace_count === 0 && (
            <strong> 暂无已验证的真实模型运行记录。</strong>
          )}
        </div>
      )}

      {sourceScope === 'REAL_API' && overview && overview.trace_count === 0 && (
        <Card title="REAL_API 数据">
          <EmptyState text="暂无已验证的真实模型运行记录。" />
        </Card>
      )}

      {error && <ErrorBox message={error} />}
      {loading && !overview && <Spinner />}

      {overview && (
        <>
          {/* 顶部核心指标 */}
          <div className="ca-kpis">
            <div className="card stat">
              <div className="k">总 Token</div>
              <div className="v">{fmtTokens(topToken)}</div>
              <div className="muted small">
                输入 {fmtTokens(promptTokens)} · 输出 {fmtTokens(completionTokens)}
              </div>
            </div>
            <div className="card stat">
              <div className="k">成本估算</div>
              <div className="v">{fmtCostValue(cost)}</div>
              <div className="row" style={{ gap: 6 }}>
                <span className={`badge ${statusTone(cost?.status)}`}>{cost?.status}</span>
                <span className="muted small">{cost?.complete ? '完整' : '部分未知'}</span>
              </div>
              <div className="muted small">{fmtCostDetail(cost)}</div>
            </div>
            <div className="card stat">
              <div className="k">平均模型延迟</div>
              <div className="v">{fmtMs(latency?.model_latency_avg_ms)}</div>
              <div className="muted small">
                总运行 {fmtMs(latency?.total_run_latency_avg_ms)} · 工具 {fmtMs(latency?.tool_latency_avg_ms)}
              </div>
            </div>
            <div className="card stat">
              <div className="k">数据覆盖</div>
              <div className="v">{overview.trace_count}</div>
              <div className="muted small">
                Trace / {overview.model_call_count} 次模型调用 · 未知用量 {overview.tokens.unknown_model_calls}
              </div>
              {overview.source_scope && (
                <div className="muted small">
                  范围 {sourceScope || '全部来源'} · 全库 {overview.source_scope.total_trace_count}
                </div>
              )}
            </div>
            <div className="card stat">
              <div className="k">UNKNOWN / 无价格</div>
              <div className="v">
                {overview.unknown.unknown_usage_calls} / {overview.unknown.price_unknown_calls}
              </div>
              <div className="muted small">不计为 0，不计入平均成本</div>
            </div>
          </div>

          <div className="ca-disclaimer">
            <strong>成本说明：</strong>
            成本为本地价格表估算，不是 Provider 实际账单；无价格的模型标记为 <code>PRICE_UNKNOWN</code>。
          </div>

          {/* 中部：趋势 / 模型分布 / 延迟 + 价格来源 */}
          <div className="ca-grid">
            <Card title="成本趋势（最近 Trace）" className="ca-panel">
              {overview.recent.length === 0 ? (
                <EmptyState text="暂无 Trace 数据" />
              ) : (
                <div className="ca-bars">
                  {overview.recent.map((row) => {
                    const max = Math.max(
                      ...overview.recent.map((item) => item.total_tokens ?? 0),
                      1,
                    )
                    const width = ((row.total_tokens ?? 0) / max) * 100
                    return (
                      <div key={row.trace_id} className="ca-bar-row">
                        <button
                          className="ca-bar-label"
                          onClick={() => navigate(`/traces/${row.trace_id}`)}
                          title={`${row.trace_id} · ${row.cost_status}`}
                        >
                          {row.trace_id.slice(0, 18)}…
                        </button>
                        <div className="ca-bar-track">
                          <span style={{ width: `${width}%` }} />
                        </div>
                        <span className="ca-bar-value">
                          {fmtTokens(row.total_tokens)} tok ·{' '}
                          {row.cost_status === 'UNKNOWN' || row.cost_status === 'PRICE_UNKNOWN'
                            ? row.cost_status
                            : fmtAmount(row.cost_amount, cost?.currency)}{' '}
                          · {row.cost_status}
                        </span>
                      </div>
                    )
                  })}
                </div>
              )}
              <div className="muted small mt">
                任务结果：成功 {overview.task_outcomes.succeeded_traces} / 失败{' '}
                {overview.task_outcomes.failed_traces} · 平均每成功任务成本{' '}
                {fmtMaybeAmount(overview.task_outcomes.avg_cost_per_successful_task, cost?.currency)} ·
                失败任务成本合计{' '}
                {fmtMaybeAmount(overview.task_outcomes.failed_task_cost_total, cost?.currency)}
              </div>
            </Card>

            <Card title="模型成本分布" className="ca-panel">
              {overview.models.length === 0 ? (
                <EmptyState
                  text={
                    sourceScope
                      ? '该来源范围暂无可归因的模型调用'
                      : '暂无模型调用'
                  }
                />
              ) : (
                <div className="ca-table-wrap">
                  <table className="tbl ca-models">
                    <thead>
                      <tr>
                        <th>模型</th>
                        <th>调用</th>
                        <th>Token</th>
                        <th>成本</th>
                        <th>状态</th>
                      </tr>
                    </thead>
                    <tbody>
                      {overview.models.map((row) => (
                        <tr key={`${row.provider}:${row.model_name}`}>
                          <td className="small">
                            {row.provider} / {row.model_name}
                          </td>
                          <td>{row.model_calls}</td>
                          <td>{fmtTokens(row.tokens.total_tokens)}</td>
                          <td>{fmtCostValue(row.cost)}</td>
                          <td>
                            <span className={`badge ${statusTone(row.cost.status)}`}>
                              {row.cost.status}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>

            <Card title="延迟分析" className="ca-panel">
              <dl className="pl-kv">
                <dt>模型延迟均值</dt>
                <dd>{fmtMs(latency?.model_latency_avg_ms)}</dd>
                <dt>工具延迟均值</dt>
                <dd>{fmtMs(latency?.tool_latency_avg_ms)}</dd>
                <dt>总运行延迟均值</dt>
                <dd>{fmtMs(latency?.total_run_latency_avg_ms)}</dd>
                <dt>可用样本</dt>
                <dd>{latency?.traces_with_known_latency ?? 0} 条 Trace</dd>
                <dt>缺失字段</dt>
                <dd>
                  {latency?.unknown_fields.length
                    ? latency.unknown_fields.join('、')
                    : '无'}
                </dd>
              </dl>
              <div className="muted small">UNKNOWN 延迟不按 0 计算；只统计真实计时数据。</div>
            </Card>

            <Card title="数据来源与价格版本" className="ca-panel">
              <dl className="pl-kv">
                <dt>价格版本</dt>
                <dd className="mono small">{overview.pricing.pricing_version}</dd>
                <dt>生效日期</dt>
                <dd>{overview.pricing.effective_date ?? '—'}</dd>
                <dt>来源</dt>
                <dd className="small">{overview.pricing.pricing_source}</dd>
                <dt>货币</dt>
                <dd>{overview.pricing.currency}</dd>
                <dt>配置路径</dt>
                <dd className="mono small">{overview.pricing.path}</dd>
                <dt>Trace 目录</dt>
                <dd className="mono small">
                  {(overview.data_sources.trace_dirs as string[] | undefined)?.join(' · ')}
                </dd>
                <dt>用量来源</dt>
                <dd>
                  {Object.entries(overview.tokens.usage_sources).map(([key, value]) => (
                    <span key={key} className="badge muted" style={{ marginRight: 4 }}>
                      {key} {value}
                    </span>
                  ))}
                </dd>
                <dt>来源范围</dt>
                <dd>
                  {sourceScope ? (
                    <span className="badge blue">{sourceScope}</span>
                  ) : (
                    <span className="badge muted">全部来源</span>
                  )}
                  {overview.source_scope &&
                    Object.entries(overview.source_scope.available_by_source).map(
                      ([key, value]) => (
                        <span key={key} className="badge muted" style={{ marginLeft: 4 }}>
                          {key} {value}
                        </span>
                      ),
                    )}
                </dd>
                <dt>付费 API</dt>
                <dd>
                  <span className="badge success">0 次</span>
                </dd>
              </dl>
              <div className="ca-prices">
                {overview.pricing.entries.map((entry) => (
                  <span key={`${entry.provider}:${entry.model_name}`} className="badge muted">
                    {entry.provider}/{entry.model_name}
                  </span>
                ))}
              </div>
            </Card>
          </div>

          {/* 版本成本变化 */}
          <Card title="版本成本变化">
            <div className={`ca-note ${overview.versions.agent_version_field_available ? '' : 'warn'}`}>
              {overview.versions.agent_version_note}
            </div>
            <div className="ca-version-grid">
              <div>
                <h4 className="pl-h4">按模型</h4>
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>分组</th>
                      <th>Trace</th>
                      <th>Token</th>
                      <th>成本</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {overview.versions.by_model.map((row) => (
                      <tr key={row.group}>
                        <td className="small">{row.group}</td>
                        <td>{row.trace_count}</td>
                        <td>{fmtTokens(row.total_tokens)}</td>
                        <td>{fmtCostValue(row.cost)}</td>
                        <td>
                          <span className={`badge ${statusTone(row.cost.status)}`}>
                            {row.cost.status}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div>
                <h4 className="pl-h4">按执行模式</h4>
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>分组</th>
                      <th>Trace</th>
                      <th>Token</th>
                      <th>成本</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {overview.versions.by_execution_mode.map((row) => (
                      <tr key={row.group}>
                        <td className="small">{row.group}</td>
                        <td>{row.trace_count}</td>
                        <td>{fmtTokens(row.total_tokens)}</td>
                        <td>{fmtCostValue(row.cost)}</td>
                        <td>
                          <span className={`badge ${statusTone(row.cost.status)}`}>
                            {row.cost.status}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </Card>

          {/* 底部：Cost Attribution Explorer */}
          <Card
            title="Cost Attribution Explorer"
            action={
              <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                <label className="small muted" htmlFor="ca-level">归因维度</label>
                <select
                  id="ca-level"
                  value={level}
                  onChange={(e) => void changeLevel(e.target.value)}
                >
                  {LEVELS.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
                {SOURCE_FILTER_LEVELS.has(level) && (
                  <>
                    <label className="small muted" htmlFor="ca-source">用量来源</label>
                    <select
                      id="ca-source"
                      value={sourceFilter}
                      onChange={(e) => void changeSource(e.target.value)}
                    >
                      {SOURCE_OPTIONS.map((item) => (
                        <option key={item.value || 'ALL'} value={item.value}>
                          {item.label}
                        </option>
                      ))}
                    </select>
                  </>
                )}
              </div>
            }
          >
            {!attribution ? (
              <Spinner />
            ) : attribution.count === 0 ? (
              <EmptyState
                text={
                  sourceScope && SOURCE_FILTER_LEVELS.has(level)
                    ? `该维度在来源范围 ${sourceScope} 下暂无可归因数据`
                    : '该维度暂无可归因数据'
                }
              />
            ) : (
              <div className="ca-table-wrap">
                <table className="tbl ca-attribution">
                  <thead>
                    <tr>
                      <th>对象</th>
                      <th>来源</th>
                      <th>Token</th>
                      <th>成本</th>
                      <th>状态</th>
                      <th>用量来源</th>
                      <th>延迟</th>
                      <th>关联 / 操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {attribution.rows.map((row) => {
                      const traceId = (row.parent_refs.trace_id as string | undefined) ??
                        ((row.parent_refs.trace_ids as string[] | undefined)?.[0])
                      return (
                        <tr key={`${row.level}-${row.id}`}>
                          <td className="mono small">{row.id}</td>
                          <td className="small">
                            {row.provider ?? '—'} / {row.model_name ?? '—'}
                          </td>
                          <td>{fmtTokens(row.total_tokens)}</td>
                          <td>{fmtCostValue(row.cost)}</td>
                          <td>
                            <span className={`badge ${statusTone(row.cost.status)}`}>
                              {row.cost.status}
                            </span>
                          </td>
                          <td className="small">{row.usage_source}</td>
                          <td>{fmtMs(row.latency_ms)}</td>
                          <td>
                            <div className="row" style={{ gap: 4 }}>
                              {traceId && (
                                <button
                                  className="btn pl-case-btn"
                                  onClick={() => navigate(`/traces/${traceId}`)}
                                >
                                  Trace
                                </button>
                              )}
                              <span className="muted small" title={row.allocation_basis}>
                                分摊方式
                              </span>
                            </div>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
            {attribution && (
              <div className="muted small mt">
                {attribution.disclaimer} · 价格版本 {attribution.pricing_version} · 付费 API{' '}
                {attribution.paid_api_called ? '有' : '0 次'}
                {sourceScope && !SOURCE_FILTER_LEVELS.has(level) && (
                  <> · 当前维度不受来源筛选影响</>
                )}
                {sourceScope && SOURCE_FILTER_LEVELS.has(level) && (
                  <> · 来源筛选 {sourceScope}</>
                )}
              </div>
            )}
          </Card>

          {/* 单次对象查询 + UNKNOWN 清单 */}
          <div className="ca-grid ca-bottom-grid">
            <Card title="单次运行 / Trace / 评测 / 流水线成本查询">
              <div className="row ca-query">
                <select
                  aria-label="查询类型"
                  value={queryKind}
                  onChange={(e) => setQueryKind(e.target.value)}
                >
                  {QUERY_KINDS.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
                <input
                  aria-label="对象 ID"
                  placeholder="例如 trace-... / eval-... / pipe-..."
                  value={queryId}
                  onChange={(e) => setQueryId(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') void runQuery()
                  }}
                />
                <button className="btn primary" onClick={() => void runQuery()} disabled={busy}>
                  <Search size={14} /> 查询
                </button>
              </div>
              {queryError && <ErrorBox message={queryError} />}
              {busy && <Spinner />}
              {queryResult && (
                <div className="ca-query-result">
                  <div className="row">
                    <span className="badge muted">
                      {String((queryResult as { trace_id?: string }).trace_id ??
                        (queryResult as { evaluation_run_id?: string }).evaluation_run_id ??
                        (queryResult as { pipeline_run_id?: string }).pipeline_run_id ??
                        (queryResult as { run_id?: string }).run_id ?? queryId)}
                    </span>
                  </div>
                  <pre className="ca-json">
                    {JSON.stringify(queryResult, null, 2).slice(0, 6000)}
                  </pre>
                </div>
              )}
            </Card>

            <Card title="UNKNOWN 与缺失数据">
              <div className="row mb">
                <span className="badge failed">
                  未知用量 {overview.unknown.unknown_usage_calls}
                </span>
                <span className="badge failed">
                  无价格 {overview.unknown.price_unknown_calls}
                </span>
                <span className="badge muted">
                  缺失延迟 {overview.latency.unknown_fields.join('、') || '无'}
                </span>
              </div>
              <div className="muted small mb">{overview.unknown.note}</div>
              {overview.unknown.examples.length === 0 ? (
                <EmptyState text="没有 UNKNOWN 数据" />
              ) : (
                <ul className="ca-unknown-list">
                  {overview.unknown.examples.slice(0, 8).map((item) => (
                    <li key={`${item.trace_id}:${item.model_call_id}`}>
                      <button
                        className="ca-link"
                        onClick={() => navigate(`/traces/${item.trace_id}`)}
                      >
                        {item.trace_id.slice(0, 20)}…
                      </button>
                      <span className="mono small">{item.model_call_id}</span>
                      <span className={`badge ${statusTone(item.cost_status)}`}>
                        {item.cost_status}
                      </span>
                      <span className="muted small">{item.reason}</span>
                    </li>
                  ))}
                </ul>
              )}
              <details className="mt">
                <summary className="muted small">
                  限制说明（{overview.limitations.length}）
                </summary>
                <ul className="vc-limits">
                  {overview.limitations.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              </details>
            </Card>
          </div>
        </>
      )}

      {!loading && !overview && !error && <EmptyState text="暂无成本数据" />}

      <div className="muted small">
        <Coins size={13} /> 成本为估算值，不是 Provider 实际账单；无价格时显示 PRICE_UNKNOWN。
      </div>
    </div>
  )
}

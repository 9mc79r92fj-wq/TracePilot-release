// V2 实验（/v2）：真实 API 运营状态、公开 Benchmark 三组对照、Judge 校准、
// QueryForge Trace 导入与管理员会话。
//
// 数据真实性约定：
// - 所有数字都来自服务端接口；接口没有数据时只显示「尚未生成实验结果」等空态，
//   不在前端补默认值或示例数字；
// - 估算费用一律标注「估算，不是 Provider 账单」；
// - 真实调用与导入由服务端强制管理员/预算校验，前端只负责提前说明与禁用按钮。

import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, FlaskConical, RefreshCw, Upload } from 'lucide-react'
import {
  fetchBenchmarkRootcause,
  fetchQueryforgeSchema,
  fetchRealBudget,
  fetchV2Status,
  importQueryforgeTrace,
  readableReadiness,
  v2ErrorMessage,
} from '../v2api'
import type {
  BenchmarkMethodMetrics,
  QueryforgeImportResult,
  QueryforgeSchemaInfo,
  V2Budget,
  V2Status,
} from '../v2api'
import { AdminSessionCard, useAdminAuth } from '../components/AdminAuth'
import { Card, EmptyState, ErrorBox, Spinner } from '../components/ui'

// 三组对照方法的固定顺序（与后端 rootcause_v2.aggregate_metrics 一致）
const BENCHMARK_METHODS: { key: string; label: string }[] = [
  { key: 'baseline_b', label: 'baseline_b' },
  { key: 'baseline_a', label: 'baseline_a' },
  { key: 'tracepilot_v2', label: 'tracepilot_v2' },
]

function fmtPct(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(1)}%`
}

function fmtNum(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return String(value)
}

function fmtCost(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `¥${value.toFixed(4)}`
}

function fmtTokens(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value.toLocaleString()
}

function fmtLatency(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${value} ms`
}

/** 从完整 Benchmark 报告中读取「未产生预测」的服务端错误计数（不推测原因）。 */
function benchmarkErrorCounts(detail: Record<string, unknown> | null): { method: string; count: number }[] {
  if (!detail || !Array.isArray(detail.results)) return []
  const counts: Record<string, number> = {}
  for (const item of detail.results) {
    if (!item || typeof item !== 'object') continue
    const errors = (item as { errors?: unknown }).errors
    if (!errors || typeof errors !== 'object') continue
    for (const [method, message] of Object.entries(errors as Record<string, unknown>)) {
      if (typeof message === 'string' && message.trim()) counts[method] = (counts[method] ?? 0) + 1
    }
  }
  return Object.entries(counts).map(([method, count]) => ({ method, count }))
}

/** 真实 API 状态与预算卡。visitor 来自 /api/real-api/budget（含访客维度余量）。 */
function RealApiStatusCard({ status, visitor }: { status: V2Status; visitor: V2Budget | null }) {
  const real = status.real_api
  const budget = status.budget
  const blockers = readableReadiness(real.blocking_reasons)
  const tokens = budget.total_tokens ?? {}
  const visitorRemaining = visitor?.visitor_calls_remaining ?? budget.visitor_calls_remaining
  const visitorLimit = visitor?.visitor_daily_limit ?? budget.visitor_daily_limit
  const visitorUsed = visitor?.visitor_calls_used ?? budget.visitor_calls_used
  return (
    <Card
      title="真实 API 状态"
      action={
        <span className={'badge ' + (real.available ? 'success' : 'muted')}>
          <span className="dot" />
          {real.available ? '可用' : '不可用'}
        </span>
      }
    >
      <div className="stack">
        <div className="row" style={{ gap: 8 }}>
          <span className="badge blue">V2 {status.version}</span>
          <span className={'badge ' + (real.public_real_enabled ? 'success' : 'muted')}>
            {real.public_real_enabled ? '访客真实调用已开放' : '访客真实调用未开放'}
          </span>
          <span className={'badge ' + (budget.enabled ? 'success' : 'partial')}>
            {budget.enabled ? '预算已启用' : '预算未启用'}
          </span>
        </div>

        <div className="field">
          <div className="k">允许模型</div>
          <div className="row" style={{ gap: 6 }}>
            {real.models.length === 0 ? (
              <span className="muted small">未提供模型列表</span>
            ) : (
              real.models.map((model) => (
                <span key={model} className="badge muted mono">
                  {model}
                </span>
              ))
            )}
          </div>
        </div>

        <div className="detail-grid">
          <div className="field">
            <div className="k">总预算已用 / 上限</div>
            <div className="v mono">
              {fmtNum(budget.total_calls_used)} / {fmtNum(budget.total_calls_limit)}
            </div>
          </div>
          <div className="field">
            <div className="k">总剩余调用</div>
            <div className="v mono">{fmtNum(budget.total_calls_remaining)}</div>
          </div>
          <div className="field">
            <div className="k">访客今日剩余 / 上限</div>
            <div className="v mono">
              {fmtNum(visitorRemaining)} / {fmtNum(visitorLimit)}
            </div>
          </div>
          <div className="field">
            <div className="k">估算费用</div>
            <div className="v mono">{fmtCost(budget.estimated_cost_cny)}</div>
          </div>
        </div>

        <div className="muted small">
          Token 累计：输入 {fmtTokens(tokens.input)} · 输出 {fmtTokens(tokens.output)}
          {visitorUsed !== null && visitorUsed !== undefined ? ` · 访客今日已用 ${visitorUsed}` : ''}
          {budget.visitor_day ? ` · 访客统计日 ${budget.visitor_day}` : ''}
        </div>

        <div className="cf-note small">
          <strong>估算，不是 Provider 账单</strong>：费用按服务端配置的人民币单价 × Token
          估算，仅用于预算控制；真实账单请以 Provider 为准。
          {budget.price_estimate_note ? ` 服务端说明：${budget.price_estimate_note}` : ''}
        </div>

        {budget.error && <ErrorBox message={`预算状态异常：${budget.error}`} />}

        {!budget.enabled && (
          <div className="errorbox" style={{ background: 'var(--warning-soft)', color: 'var(--warning)' }}>
            真实调用预算未启用（总调用上限为 0）：所有真实调用都会被服务端拒绝，不会产生费用。
          </div>
        )}

        {blockers.length > 0 && (
          <div className="errorbox" style={{ background: 'var(--warning-soft)', color: 'var(--warning)' }}>
            <strong>真实 API 拦截原因</strong>
            <ul style={{ margin: '6px 0 0 16px' }}>
              {blockers.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          </div>
        )}

        {real.available && (
          <p className="muted small" style={{ margin: 0 }}>
            真实调用仍受服务端管理员校验与持久预算限制：每次上游调用按预算扣减，失败不自动重试；
            前端仅提前提示，最终是否放行由服务端决定。
          </p>
        )}
      </div>
    </Card>
  )
}

/** 公开 Benchmark 三组对照表 + 方法说明/限制（可展开）。 */
function BenchmarkCard({
  status,
  detail,
  detailError,
}: {
  status: V2Status
  detail: Record<string, unknown> | null
  detailError: string | null
}) {
  const summary = status.benchmark
  const metrics: Record<string, BenchmarkMethodMetrics> | null =
    summary.metrics ??
    ((detail?.metrics as Record<string, BenchmarkMethodMetrics> | undefined) ?? null)
  const dataset =
    summary.dataset ?? (typeof detail?.dataset === 'string' ? (detail.dataset as string) : null)
  const caseCount =
    summary.case_count ?? (typeof detail?.case_count === 'number' ? (detail.case_count as number) : null)
  const limitations =
    summary.limitations ??
    (Array.isArray(detail?.limitations) ? (detail.limitations as string[]) : null)
  const methodDescriptions =
    detail && typeof detail.method_descriptions === 'object' && detail.method_descriptions
      ? (detail.method_descriptions as Record<string, string>)
      : null
  const labelIsolation = typeof detail?.label_isolation === 'string' ? detail.label_isolation : null
  const split = typeof detail?.split === 'string' ? detail.split : null
  const errorCounts = benchmarkErrorCounts(detail)
  const generated = summary.available && metrics !== null

  return (
    <Card
      title="公开 Benchmark · 三组根因归因对照"
      action={
        <span className={'badge ' + (generated ? 'success' : 'muted')}>
          {generated ? '真实实验结果' : '尚未生成'}
        </span>
      }
    >
      <div className="stack">
        <div className="row" style={{ gap: 10 }}>
          <span className="small muted">数据集：</span>
          <span className="small">{dataset ?? '—'}</span>
          <span className="badge muted">样本量 {caseCount === null ? '—' : `${caseCount} 例`}</span>
          {split && <span className="badge muted">split {split}</span>}
        </div>

        {!generated ? (
          <EmptyState text="尚未生成实验结果" />
        ) : (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th>方法</th>
                  <th>Top-1</th>
                  <th>Top-3</th>
                  <th>Failure Step</th>
                  <th>Category Accuracy</th>
                  <th>Macro-F1</th>
                  <th>调用数</th>
                  <th>Token</th>
                  <th>平均延迟</th>
                </tr>
              </thead>
              <tbody>
                {BENCHMARK_METHODS.map((method) => {
                  const row = metrics[method.key]
                  const description = methodDescriptions?.[method.key]
                  return (
                    <tr key={method.key}>
                      <td>
                        <div className="mono small">{method.label}</div>
                        {description && (
                          <div className="muted" style={{ fontSize: 11.5 }}>
                            {description}
                          </div>
                        )}
                        {row && (
                          <div className="muted" style={{ fontSize: 11 }}>
                            有效预测 {fmtNum(row.predictions)} / {fmtNum(row.cases)}
                          </div>
                        )}
                      </td>
                      <td className="mono">{fmtPct(row?.top1_accuracy)}</td>
                      <td className="mono">{fmtPct(row?.top3_hit_rate)}</td>
                      <td className="mono">{fmtPct(row?.failure_step_accuracy)}</td>
                      <td className="mono">{fmtPct(row?.category_accuracy)}</td>
                      <td className="mono">
                        {row?.category_macro_f1 === null || row?.category_macro_f1 === undefined
                          ? '—'
                          : row.category_macro_f1.toFixed(4)}
                      </td>
                      <td className="mono">{fmtNum(row?.llm_calls)}</td>
                      <td className="mono">{fmtTokens(row?.total_tokens)}</td>
                      <td className="mono">{fmtLatency(row?.avg_latency_ms)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        {detailError && <ErrorBox message={`完整 Benchmark 报告读取失败：${detailError}`} />}

        <details className="ar-runtime-details" style={{ marginTop: 0 }}>
          <summary>方法说明与限制</summary>
          <div className="stack small" style={{ marginTop: 10 }}>
            {methodDescriptions ? (
              <div>
                <strong>方法说明</strong>
                <ul style={{ margin: '4px 0 0 16px' }}>
                  {Object.entries(methodDescriptions).map(([method, text]) => (
                    <li key={method}>
                      <span className="mono">{method}</span>：{text}
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <div className="muted">方法说明需服务端已生成完整 Benchmark 报告（当前不可用）。</div>
            )}

            {limitations && limitations.length > 0 ? (
              <div>
                <strong>实验限制</strong>
                <ul style={{ margin: '4px 0 0 16px' }}>
                  {limitations.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : (
              <div className="muted">服务端未提供实验限制说明。</div>
            )}

            {errorCounts.length > 0 && (
              <div className="muted">
                未产生预测的案例（服务端返回的错误）：{' '}
                {errorCounts.map((item) => `${item.method} ${item.count} 例`).join('；')}
              </div>
            )}

            {labelIsolation && <div className="muted">标签隔离：{labelIsolation}</div>}

            <div className="muted mono" style={{ fontSize: 11 }}>
              结果文件：{summary.file}
            </div>
          </div>
        </details>
      </div>
    </Card>
  )
}

/** Judge 校准卡（字段缺失时不显示对应指标）。 */
function JudgeCalibrationCard({ status }: { status: V2Status }) {
  const calibration = status.judge_calibration
  const judge = calibration.judge
  const metrics = calibration.metrics
  const isMock = judge?.is_mock === true || judge?.simulated === true

  if (!calibration.available) {
    return (
      <Card title="Judge 校准">
        <EmptyState text="尚未生成 Judge 校准结果" />
      </Card>
    )
  }

  // evaluated_cases 为参与评估的样本总数；agreement 的分母是
  // 「参考标注与 Judge 均为 SCORED」的可评估样本数（metrics.evaluable_cases）
  const evaluatedCases = calibration.evaluated_cases ?? null
  const scoredCases = metrics?.evaluable_cases ?? calibration.evaluated_cases ?? null

  return (
    <Card
      title="Judge 校准"
      action={<span className="badge success">真实校准结果</span>}
    >
      <div className="stack small">
        <div className="row" style={{ gap: 8 }}>
          <span className="badge blue">provider {judge?.provider_kind ?? '—'}</span>
          <span className="mono small">{judge?.model_name ?? '未记录模型'}</span>
          <span className={'badge ' + (isMock ? 'partial' : 'success')}>
            {isMock ? '模拟 provider' : '真实 provider'}
          </span>
          {judge?.rubric_id && (
            <span className="badge muted">
              rubric {judge.rubric_id}
              {judge.rubric_version ? ` · ${judge.rubric_version}` : ''}
            </span>
          )}
        </div>

        <div className="detail-grid">
          <div className="field">
            <div className="k">evaluated_cases</div>
            <div className="v mono">{fmtNum(evaluatedCases)}</div>
          </div>
          {metrics?.exact_agreement !== null && metrics?.exact_agreement !== undefined && (
            <div className="field">
              <div className="k">exact agreement</div>
              <div className="v mono">
                {fmtPct(metrics.exact_agreement)}
                {metrics.exact_agreement_count !== null &&
                metrics.exact_agreement_count !== undefined ? (
                  <span className="muted small">
                    （{metrics.exact_agreement_count}/{scoredCases ?? '—'}）
                  </span>
                ) : null}
              </div>
            </div>
          )}
          {metrics?.mae !== null && metrics?.mae !== undefined && (
            <div className="field">
              <div className="k">MAE（分数绝对误差）</div>
              <div className="v mono">{metrics.mae}</div>
            </div>
          )}
          {metrics?.within_1_agreement !== null &&
            metrics?.within_1_agreement !== undefined && (
              <div className="field">
                <div className="k">within-1 agreement</div>
                <div className="v mono">
                  {fmtPct(metrics.within_1_agreement)}
                  {metrics.within_1_agreement_count !== null &&
                  metrics.within_1_agreement_count !== undefined ? (
                    <span className="muted small">
                      （{metrics.within_1_agreement_count}/{scoredCases ?? '—'}）
                    </span>
                  ) : null}
                </div>
              </div>
            )}
        </div>

        {!metrics && <div className="muted">服务端未返回校准指标。</div>}
        {metrics && scoredCases !== null && evaluatedCases !== null && scoredCases !== evaluatedCases && (
          <div className="muted" style={{ fontSize: 11.5 }}>
            agreement 分母为可评估样本数（metrics.evaluable_cases = {scoredCases}）；
            evaluated_cases = {evaluatedCases} 为参与评估的样本总数。
          </div>
        )}

        <div className="muted" style={{ fontSize: 11.5 }}>
          校准基于人工标注样本；指标只在有数据时显示，不代表线上 Judge 的一致率。
        </div>
      </div>
    </Card>
  )
}

/** QueryForge Trace v1 导入卡（需管理员会话，写入 data/traces）。 */
function QueryforgeCard({
  admin,
  schemaInfo,
}: {
  admin: ReturnType<typeof useAdminAuth>
  schemaInfo: QueryforgeSchemaInfo | null
}) {
  const navigate = useNavigate()
  const [payload, setPayload] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<QueryforgeImportResult | null>(null)

  const session = admin.session
  const adminRequired = session?.required === true
  const adminReady = session ? !adminRequired || session.authenticated : admin.error !== null

  async function importTrace() {
    setError(null)
    setResult(null)
    let parsed: unknown
    try {
      parsed = JSON.parse(payload)
    } catch {
      setError('粘贴的内容不是合法 JSON')
      return
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      setError('需要粘贴 QueryForge Trace v1 JSON 对象（不是数组或空值）')
      return
    }
    setBusy(true)
    try {
      setResult(await importQueryforgeTrace(parsed))
    } catch (e) {
      setError(v2ErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card
      title="QueryForge Trace 导入"
      action={
        <span className="badge blue">
          protocol {schemaInfo?.protocol_version ?? '1.0'}
        </span>
      }
    >
      <div className="stack">
        <p className="muted small" style={{ margin: 0 }}>
          粘贴一条 QueryForge Trace v1 JSON 对象，服务端校验后写入 <span className="mono">data/traces</span>；
          同名 trace_id 不会覆盖（返回 409）。导入成功后可直接打开 Trace 详情。
        </p>

        <label className="field" style={{ marginBottom: 0 }}>
          <span className="k">QueryForge Trace v1 JSON</span>
          <textarea
            className="cf-textarea"
            rows={8}
            spellCheck={false}
            value={payload}
            onChange={(e) => setPayload(e.target.value)}
            placeholder='在此粘贴 QueryForge Trace v1 JSON 对象，例如 {"trace_id": "qf-demo-001", "schema_version": "1.0", ...}'
            aria-label="QueryForge Trace JSON"
          />
        </label>

        <div className="row" style={{ gap: 10 }}>
          <button
            className="btn primary"
            type="button"
            onClick={() => void importTrace()}
            disabled={busy || !payload.trim() || !adminReady}
            title={
              !adminReady
                ? '需要管理员登录后才能导入'
                : !payload.trim()
                  ? '请先粘贴 JSON'
                  : undefined
            }
          >
            <Upload size={14} /> {busy ? '校验并导入中…' : '校验并导入'}
          </button>
          <button className="btn" type="button" onClick={() => setPayload('')} disabled={busy || !payload}>
            清空
          </button>
          {busy && <span className="muted small">正在校验并写入 Trace 目录…</span>}
        </div>

        {!adminReady && (
          <div className="cf-note small">
            {session
              ? '需要管理员登录：请在上方「管理员登录」卡片完成登录后再导入。'
              : '正在读取管理员会话；若接口不可用请联系服务端确认。'}
          </div>
        )}
        {session && !session.configured && (
          <p className="muted small" style={{ margin: 0 }}>
            服务端未配置管理员令牌（本机开发模式）：导入接口不强制管理员校验。
          </p>
        )}

        {error && <ErrorBox message={error} />}

        {result && (
          <div className="cf-note small">
            <div className="row" style={{ gap: 8 }}>
              <span className="badge success">导入成功</span>
              <span className="mono">{result.trace_id}</span>
              {result.saved === false && <span className="badge partial">未写入磁盘</span>}
              {result.step_count !== undefined && (
                <span className="badge muted">{result.step_count} 步</span>
              )}
            </div>
            {result.warnings && result.warnings.length > 0 && (
              <div style={{ marginTop: 6 }}>
                <strong>warnings</strong>
                <ul style={{ margin: '4px 0 0 16px' }}>
                  {result.warnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              </div>
            )}
            <div style={{ marginTop: 8 }}>
              <button
                className="btn"
                type="button"
                onClick={() => navigate('/traces/' + encodeURIComponent(result.trace_id))}
              >
                打开 Trace 详情 <ArrowRight size={14} />
              </button>
            </div>
          </div>
        )}

        {schemaInfo && (
          <details className="ar-runtime-details" style={{ marginTop: 0 }}>
            <summary>QueryForge Trace v1 协议说明（只读）</summary>
            <div className="stack small" style={{ marginTop: 10 }}>
              <div>
                协议版本：<span className="mono">{schemaInfo.protocol_version}</span> · 来源：
                <span className="mono"> {schemaInfo.source}</span>
              </div>
              <div className="muted">{schemaInfo.note}</div>
              <pre className="json">{JSON.stringify(schemaInfo.schema, null, 2)}</pre>
            </div>
          </details>
        )}
      </div>
    </Card>
  )
}

export default function V2Experiments() {
  const admin = useAdminAuth()
  const [status, setStatus] = useState<V2Status | null>(null)
  const [benchmarkDetail, setBenchmarkDetail] = useState<Record<string, unknown> | null>(null)
  const [benchmarkError, setBenchmarkError] = useState<string | null>(null)
  const [schemaInfo, setSchemaInfo] = useState<QueryforgeSchemaInfo | null>(null)
  const [visitorBudget, setVisitorBudget] = useState<V2Budget | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    setBenchmarkError(null)
    const [statusResult, benchmarkResult, schemaResult, budgetResult] = await Promise.allSettled([
      fetchV2Status(),
      fetchBenchmarkRootcause(),
      fetchQueryforgeSchema(),
      fetchRealBudget(),
    ])
    if (statusResult.status === 'fulfilled') setStatus(statusResult.value)
    else {
      setStatus(null)
      setError(v2ErrorMessage(statusResult.reason))
    }
    if (benchmarkResult.status === 'fulfilled') setBenchmarkDetail(benchmarkResult.value)
    else setBenchmarkError(v2ErrorMessage(benchmarkResult.reason))
    if (schemaResult.status === 'fulfilled') setSchemaInfo(schemaResult.value)
    else setSchemaInfo(null)
    setVisitorBudget(budgetResult.status === 'fulfilled' ? budgetResult.value.budget : null)
    setLoading(false)
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <div className="stack">
      <div className="row">
        <div>
          <h2 style={{ margin: 0 }}>V2 实验</h2>
          <p className="muted small" style={{ margin: '4px 0 0' }}>
            真实模型受控调用、公开 Benchmark 三组对照与跨框架 Trace 导入。所有数字均来自服务端接口，
            不生成占位数据。
          </p>
        </div>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => void load()} disabled={loading}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>

      {loading && <Spinner />}
      {!loading && error && <ErrorBox message={`V2 状态接口不可用：${error}`} />}

      {!loading && status && (
        <>
          <RealApiStatusCard status={status} visitor={visitorBudget} />
          <BenchmarkCard status={status} detail={benchmarkDetail} detailError={benchmarkError} />
        </>
      )}

      {!loading && (
        <>
          <div className="grid grid-2">
            {status ? (
              <JudgeCalibrationCard status={status} />
            ) : (
              <Card title="Judge 校准">
                <EmptyState text="V2 状态接口不可用，无法读取 Judge 校准结果" />
              </Card>
            )}
            <AdminSessionCard auth={admin} />
          </div>

          <QueryforgeCard admin={admin} schemaInfo={schemaInfo} />

          <Card title="安全与费用边界">
            <ul className="ar-safety-list" style={{ paddingLeft: 17 }}>
              <li>
                真实调用（反事实实验 / 真实修复验证）会消耗真实模型预算；单次执行按服务端持久预算扣减，
                失败不自动重试。
              </li>
              <li>
                管理员令牌只存于服务端环境变量，前端仅在登录请求中临时提交，不写入 localStorage；
                会话为 HttpOnly 签名 Cookie。
              </li>
              <li>
                估算费用按服务端配置的人民币单价 × Token 计算，<strong>不是 Provider 账单</strong>。
              </li>
              <li>
                Benchmark 汇总与 Judge 校准为服务端离线实验产物；未生成时页面只显示空态，不展示示例数字。
              </li>
              <li>
                QueryForge 导入写入 <span className="mono">data/traces</span>，不覆盖同名 trace_id。
              </li>
            </ul>
            <div className="row" style={{ marginTop: 12 }}>
              <FlaskConical size={15} />
              <span className="muted small">
                受控真实实验入口：反事实分析页与修复工作台页底部的「真实 API 受控实验」卡片。
              </span>
            </div>
          </Card>
        </>
      )}
    </div>
  )
}

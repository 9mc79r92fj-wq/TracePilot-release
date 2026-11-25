import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { BarChart3, GitCompare, RefreshCw, X } from 'lucide-react'
import { api, apiErrorMessage } from '../api'
import { EmptyState, ErrorBox, Spinner } from './ui'
import { AttributionPanel, type AttributionReport } from './AttributionPanel'

interface PatternFields {
  failure_type: string
  tool_name: string | null
  failed_metric: string | null
  recovery_status: string
  scenario_type: string | null
}

interface Pattern {
  pattern_id: string
  fingerprint_id: string
  fingerprint_fields: PatternFields
  fingerprint_label: string
  missing_fields: string[]
  case_count: number
  share_of_bad_cases: number | null
  has_unknown_cases: boolean
  cause_type_counts: Record<string, number>
  evidence_level_counts: Record<string, number>
  representative_case: {
    case_id: string
    trace_id: string | null
    attribution_id: string | null
    overall_status: string
  }
  case_ids: string[]
  trace_ids: string[]
}

interface CaseRow {
  case_id: string
  trace_id: string | null
  scenario_type: string | null
  fingerprint_id: string
  fingerprint_label: string
  failure_type: string
  tool_name: string | null
  failed_metric: string | null
  recovery_status: string
  is_unknown: boolean
  attribution_id: string | null
  evidence_levels: Record<string, number>
  cause_types: string[]
  failed_metrics: string[]
}

interface PatternReport {
  engine_version: string
  pattern_report_id: string
  evaluation_run_id: string
  dataset_id: string | null
  strategy: string
  generated_at: string
  denominators: {
    total_cases_in_run: number
    executed_cases: number
    bad_cases: number
    analyzed_cases: number
    unknown_cases: number
    notes: Record<string, string>
  }
  pattern_count: number
  patterns: Pattern[]
  cases: CaseRow[]
  unknown_cases: { case_id: string; trace_id: string | null; reason: string }[]
  top_patterns: string[]
  limitations: string[]
  paid_api_called: boolean
}

interface CompareRow {
  fingerprint_id: string
  fingerprint_label: string
  failure_type: string
  tool_name: string | null
  cases_a: number
  cases_b: number
  delta: number
  status: 'NEW' | 'RESOLVED' | 'INCREASED' | 'DECREASED' | 'UNCHANGED'
}

interface CompareResult {
  run_a: { evaluation_run_id: string; pattern_report_id: string; pattern_count: number }
  run_b: { evaluation_run_id: string; pattern_report_id: string; pattern_count: number }
  rows: CompareRow[]
  summary: {
    new_patterns: number
    resolved_patterns: number
    increased: number
    decreased: number
    unchanged: number
    bad_cases_a: number
    bad_cases_b: number
  }
  notes: string[]
  paid_api_called: boolean
}

const TYPE_BADGE: Record<string, string> = {
  TOOL_TIMEOUT: 'badge failed',
  TOOL_EXCEPTION: 'badge failed',
  INVALID_ARGUMENTS: 'badge partial',
  INVALID_TOOL_RESULT: 'badge partial',
  EMPTY_RETRIEVAL: 'badge warning',
  PERMISSION_DENIED: 'badge failed',
  TASK_INCOMPLETE: 'badge warning',
  OUTPUT_MISMATCH: 'badge partial',
  PROCESS_VIOLATION: 'badge blue',
  UNKNOWN: 'badge muted',
}

const STATUS_BADGE: Record<string, string> = {
  NEW: 'badge failed',
  RESOLVED: 'badge success',
  INCREASED: 'badge warning',
  DECREASED: 'badge blue',
  UNCHANGED: 'badge muted',
}

/** 失败模式分布条（真实数据驱动，宽度按占比） */
function DistributionBars({
  patterns,
  total,
  onSelect,
}: {
  patterns: Pattern[]
  total: number
  onSelect: (p: Pattern) => void
}) {
  if (patterns.length === 0) return <EmptyState text="无可分析的失败模式" />
  return (
    <div className="fp-bars">
      {patterns.map((p) => {
        const pct = total ? (p.case_count / total) * 100 : 0
        return (
          <button key={p.pattern_id} className="fp-bar-row" onClick={() => onSelect(p)}>
            <span className="fp-bar-label">
              <span className={TYPE_BADGE[p.fingerprint_fields.failure_type] ?? 'badge muted'}>
                {p.fingerprint_fields.failure_type}
              </span>
              <span className="mono small muted">{p.fingerprint_fields.tool_name ?? '-'}</span>
            </span>
            <span className="fp-bar-track">
              <span className="fp-bar-fill" style={{ width: `${Math.max(pct, 2)}%` }} />
            </span>
            <span className="fp-bar-num small">
              {p.case_count}
              <span className="muted"> · {pct.toFixed(1)}%</span>
            </span>
          </button>
        )
      })}
    </div>
  )
}

/** Failure Pattern Explorer（嵌入 Evaluation Center） */
export function FailurePatternExplorer({
  runId,
  runOptions,
}: {
  runId: string | null
  runOptions: { run_id: string; status: string; dataset_id?: string }[]
}) {
  const navigate = useNavigate()
  const [report, setReport] = useState<PatternReport | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [filterType, setFilterType] = useState('')
  const [filterTool, setFilterTool] = useState('')
  const [active, setActive] = useState<Pattern | null>(null)
  const [attribution, setAttribution] = useState<AttributionReport | null>(null)
  const [attrError, setAttrError] = useState<string | null>(null)

  const [compareA, setCompareA] = useState('')
  const [compareB, setCompareB] = useState('')
  const [compare, setCompare] = useState<CompareResult | null>(null)
  const [compareError, setCompareError] = useState<string | null>(null)
  const [compareBusy, setCompareBusy] = useState(false)

  const analyze = useCallback(
    async (targetRunId: string) => {
      setBusy(true)
      setError(null)
      setReport(null)
      setActive(null)
      setAttribution(null)
      // 评测刚完成时后端可能仍报告"运行中"，做有限次自动重试（最多 3 次）
      for (let attempt = 0; attempt < 3; attempt += 1) {
        try {
          // 失败模式分析为 CPU 密集任务，并发负载下 15s 默认超时会误报；
          // 显式放宽到 120s（仍受服务端分析上限约束）。
          const r = await api.post<PatternReport>(
            '/api/failure-patterns/runs',
            {
              evaluation_run_id: targetRunId,
              max_cases: 50,
            },
            { timeout: 120000 },
          )
          setReport(r.data)
          setBusy(false)
          return
        } catch (e) {
          const msg = apiErrorMessage(e)
          const retryable = msg.includes('运行中') || msg.includes('running')
          if (!retryable || attempt === 2) {
            setError(msg)
            setBusy(false)
            return
          }
          await new Promise((resolve) => setTimeout(resolve, 1200))
        }
      }
      setBusy(false)
    },
    [],
  )

  useEffect(() => {
    if (runId) {
      setCompareA(runId)
      void analyze(runId)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId])

  const filtered = useMemo(() => {
    if (!report) return []
    return report.cases.filter((c) => {
      if (filterType && c.failure_type !== filterType) return false
      if (filterTool) {
        if (filterTool === '__none__') {
          if (c.tool_name) return false
        } else if (c.tool_name !== filterTool) {
          return false
        }
      }
      return true
    })
  }, [report, filterType, filterTool])

  const failureTypes = useMemo(
    () => Array.from(new Set((report?.cases ?? []).map((c) => c.failure_type))).sort(),
    [report],
  )
  const toolNames = useMemo(() => {
    const names = new Set<string>()
    let hasNone = false
    for (const c of report?.cases ?? []) {
      if (c.tool_name) names.add(c.tool_name)
      else hasNone = true
    }
    return { names: Array.from(names).sort(), hasNone }
  }, [report])

  async function openCaseEvidence(caseRow: CaseRow) {
    // 证据面板位于模式抽屉内：先打开所属模式，再加载归因
    const pat = report?.patterns.find((p) => p.pattern_id === caseRow.fingerprint_id) ?? null
    setActive(pat)
    await openAttribution(caseRow.attribution_id)
  }

  async function openAttribution(attributionId: string | null) {
    setAttrError(null)
    if (!attributionId) {
      setAttrError('该案例没有归因报告，无法查看证据')
      return
    }
    try {
      const r = await api.get<{ report: AttributionReport }>(
        `/api/attribution/reports/${attributionId}`,
      )
      setAttribution(r.data.report)
    } catch (e) {
      setAttrError(apiErrorMessage(e))
    }
  }

  async function runCompare() {
    if (!compareA || !compareB || compareA === compareB) {
      setCompareError('请选择两个不同的评测运行')
      return
    }
    setCompareBusy(true)
    setCompareError(null)
    setCompare(null)
    try {
      const r = await api.post<CompareResult>('/api/failure-patterns/compare', {
        run_a: compareA,
        run_b: compareB,
        max_cases: 50,
      })
      setCompare(r.data)
    } catch (e) {
      setCompareError(apiErrorMessage(e))
    } finally {
      setCompareBusy(false)
    }
  }

  return (
    <div className="stack">
      <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
        <span className="row" style={{ gap: 6 }}>
          <BarChart3 size={15} />
          <strong>Failure Pattern Explorer</strong>
        </span>
        <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
          <button
            className="btn"
            onClick={() => runId && void analyze(runId)}
            disabled={busy || !runId}
          >
            <RefreshCw size={13} /> 重新分析
          </button>
          {report && (
            <span className="muted small">
              模式 {report.pattern_count} · 分析 {report.denominators.analyzed_cases} 例
            </span>
          )}
        </div>
      </div>

      {!runId && <EmptyState text="请先运行一次评测，再查看失败模式" />}
      {error && <ErrorBox message={error} />}
      {busy && <Spinner />}

      {report && (
        <>
          {/* 分母透明声明 */}
          <div className="cf-info small">
            <div>
              <span className="muted">统计分母</span> 运行案例 {report.denominators.total_cases_in_run} ·
              已执行 {report.denominators.executed_cases} · Bad Case{' '}
              {report.denominators.bad_cases} · 本次分析{' '}
              {report.denominators.analyzed_cases} · UNKNOWN{' '}
              {report.denominators.unknown_cases}
            </div>
            <div className="muted">
              占比分母为本次分析的 Bad Case 数；UNKNOWN 单独计数。
            </div>
          </div>

          {/* 分布图 + 排行榜 */}
          <div className="fp-grid">
            <div className="fp-panel">
              <div className="fp-panel-head">失败模式分布（点击下钻）</div>
              <DistributionBars
                patterns={report.patterns}
                total={report.denominators.analyzed_cases}
                onSelect={setActive}
              />
            </div>
            <div className="fp-panel">
              <div className="fp-panel-head">高频问题排行</div>
              {report.patterns.slice(0, 5).map((p, i) => (
                <div key={p.pattern_id} className="fp-rank-row">
                  <span className="fp-rank-no">{i + 1}</span>
                  <span className="fp-rank-body">
                    <span className="mono small">{p.fingerprint_label}</span>
                    <span className="muted small">
                      缺字段 {p.missing_fields.length ? p.missing_fields.join(', ') : '无'}
                    </span>
                  </span>
                  <span className="fp-rank-num">{p.case_count}</span>
                </div>
              ))}
              {report.patterns.length === 0 && <EmptyState text="无失败模式" />}
            </div>
          </div>

          {/* 筛选 + 案例表 */}
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            <select value={filterType} onChange={(e) => setFilterType(e.target.value)} aria-label="按失败类型筛选">
              <option value="">全部失败类型</option>
              {failureTypes.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
            <select value={filterTool} onChange={(e) => setFilterTool(e.target.value)} aria-label="按工具名称筛选">
              <option value="">全部工具</option>
              {toolNames.names.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
              {toolNames.hasNone && <option value="__none__">（无失败工具）</option>}
            </select>
            <span className="muted small">匹配 {filtered.length} / {report.cases.length} 例</span>
          </div>

          <table className="tbl">
            <thead>
              <tr>
                <th>案例</th><th>失败类型</th><th>工具</th><th>失败指标</th>
                <th>恢复状态</th><th>场景</th><th>指纹</th><th></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((c) => (
                <tr key={c.case_id}>
                  <td className="mono small">
                    {c.case_id}
                    {c.is_unknown && <span className="badge muted" style={{ marginLeft: 4 }}>UNKNOWN</span>}
                  </td>
                  <td>
                    <span className={TYPE_BADGE[c.failure_type] ?? 'badge muted'}>{c.failure_type}</span>
                  </td>
                  <td className="small">{c.tool_name ?? '-'}</td>
                  <td className="small">{c.failed_metric ?? '-'}</td>
                  <td className="small">{c.recovery_status}</td>
                  <td className="small">{c.scenario_type ?? '-'}</td>
                  <td className="mono small muted">{c.fingerprint_id}</td>
                  <td>
                    <div className="row" style={{ gap: 4 }}>
                      <button
                        className="btn"
                        style={{ padding: '2px 8px' }}
                        onClick={() => void openCaseEvidence(c)}
                      >
                        证据
                      </button>
                      {c.trace_id && (
                        <>
                          <button
                            className="btn"
                            style={{ padding: '2px 8px' }}
                            onClick={() => navigate('/traces/' + c.trace_id)}
                          >
                            Trace
                          </button>
                          <button
                            className="btn"
                            style={{ padding: '2px 8px' }}
                            onClick={() => navigate('/dag/' + c.trace_id)}
                          >
                            DAG
                          </button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {report.unknown_cases.length > 0 && (
            <div className="cf-note small">
              <strong>UNKNOWN 案例（{report.unknown_cases.length}）</strong>：证据不足，未计入占比
              <ul style={{ margin: '4px 0 0 16px' }}>
                {report.unknown_cases.map((u) => (
                  <li key={u.case_id} className="mono small">{u.case_id} — {u.reason}</li>
                ))}
              </ul>
            </div>
          )}

          {/* 运行对比 */}
          <div className="fp-panel">
            <div className="fp-panel-head row" style={{ gap: 6 }}>
              <GitCompare size={13} /> 评测版本变化对比
            </div>
            <div className="row" style={{ gap: 8, flexWrap: 'wrap', marginTop: 8 }}>
              <select value={compareA} onChange={(e) => setCompareA(e.target.value)} aria-label="对比运行A">
                <option value="">选择运行 A</option>
                {runOptions.map((r) => (
                  <option key={r.run_id} value={r.run_id}>{r.run_id}（{r.dataset_id ?? '-'}）</option>
                ))}
              </select>
              <select value={compareB} onChange={(e) => setCompareB(e.target.value)} aria-label="对比运行B">
                <option value="">选择运行 B</option>
                {runOptions.map((r) => (
                  <option key={r.run_id} value={r.run_id}>{r.run_id}（{r.dataset_id ?? '-'}）</option>
                ))}
              </select>
              <button className="btn primary" onClick={() => void runCompare()} disabled={compareBusy}>
                对比
              </button>
              {compareBusy && <span className="muted small">对比中…</span>}
            </div>
            {compareError && <ErrorBox message={compareError} />}
            {compare && (
              <div className="stack small" style={{ marginTop: 8 }}>
                <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                  <span className="badge failed">新增 {compare.summary.new_patterns}</span>
                  <span className="badge success">消失 {compare.summary.resolved_patterns}</span>
                  <span className="badge warning">增加 {compare.summary.increased}</span>
                  <span className="badge blue">减少 {compare.summary.decreased}</span>
                  <span className="badge muted">不变 {compare.summary.unchanged}</span>
                  <span className="muted">
                    Bad Case：A {compare.summary.bad_cases_a} → B {compare.summary.bad_cases_b}
                  </span>
                </div>
                <table className="tbl">
                  <thead>
                    <tr><th>指纹</th><th>失败类型</th><th>A</th><th>B</th><th>Δ</th><th>状态</th></tr>
                  </thead>
                  <tbody>
                    {compare.rows.map((r) => (
                      <tr key={r.fingerprint_id}>
                        <td className="mono small">{r.fingerprint_label}</td>
                        <td><span className={TYPE_BADGE[r.failure_type] ?? 'badge muted'}>{r.failure_type}</span></td>
                        <td>{r.cases_a}</td>
                        <td>{r.cases_b}</td>
                        <td>{r.delta > 0 ? `+${r.delta}` : r.delta}</td>
                        <td><span className={STATUS_BADGE[r.status] ?? 'badge muted'}>{r.status}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="muted">{compare.notes.join(' ')}</div>
              </div>
            )}
          </div>

          <div className="muted small">
            {report.limitations.map((l) => <div key={l}>· {l}</div>)}
            <div>付费 API 调用：{report.paid_api_called ? '是' : '否'}</div>
          </div>
        </>
      )}

      {/* 证据独立抽屉：当只有归因、没有选中模式时也能查看证据 */}
      {attribution && !active && (
        <div className="drawer-backdrop" onClick={() => setAttribution(null)}>
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <span className="small">归因证据</span>
              <button className="btn" onClick={() => setAttribution(null)} aria-label="关闭"><X size={15} /></button>
            </div>
            <div className="drawer-body">
              <AttributionPanel report={attribution} />
            </div>
          </div>
        </div>
      )}

      {/* 模式详情抽屉 */}
      {active && (
        <div
          className="drawer-backdrop"
          onClick={() => {
            setActive(null)
            setAttribution(null)
          }}
        >
          <div className="drawer" onClick={(e) => e.stopPropagation()}>
            <div className="drawer-head">
              <span className="row" style={{ gap: 6 }}>
                <span className={TYPE_BADGE[active.fingerprint_fields.failure_type] ?? 'badge muted'}>
                  {active.fingerprint_fields.failure_type}
                </span>
                <span className="mono small">{active.fingerprint_id}</span>
              </span>
              <button
                className="btn"
                onClick={() => {
                  setActive(null)
                  setAttribution(null)
                }}
                aria-label="关闭"
              >
                <X size={15} />
              </button>
            </div>
            <div className="drawer-body stack small">
              <div className="cf-info">
                <div><span className="muted">指纹</span> <span className="mono small">{active.fingerprint_label}</span></div>
                <div><span className="muted">案例数</span> {active.case_count}（占分析案例 {((active.share_of_bad_cases ?? 0) * 100).toFixed(1)}%）</div>
                <div><span className="muted">缺失字段</span> {active.missing_fields.length ? active.missing_fields.join(', ') : '无'}</div>
                <div><span className="muted">证据等级</span> {Object.entries(active.evidence_level_counts).map(([k, v]) => `${k}×${v}`).join(' / ') || '-'}</div>
              </div>
              <table className="tbl">
                <thead><tr><th>字段</th><th>值</th></tr></thead>
                <tbody>
                  {Object.entries(active.fingerprint_fields).map(([k, v]) => (
                    <tr key={k}>
                      <td className="small muted">{k}</td>
                      <td className="mono small">{v === null ? '—（不可得）' : String(v)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div>
                <strong>该模式案例</strong>
                <ul style={{ margin: '4px 0 0 16px' }}>
                  {active.case_ids.map((cid) => (
                    <li key={cid}>
                      <button
                        className="btn"
                        style={{ padding: '1px 6px' }}
                        onClick={() => {
                          const row = report?.cases.find((c) => c.case_id === cid)
                          void openAttribution(row?.attribution_id ?? null)
                        }}
                      >
                        {cid}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
              {attrError && <ErrorBox message={attrError} />}
              {attribution && (
                <div>
                  <strong>归因证据（Evidence Drill-down）</strong>
                  <AttributionPanel report={attribution} />
                </div>
              )}
              <div className="cf-note small">
                相同指纹只表示失败特征相似，<strong>不代表已证明的相同根因</strong>。
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, RefreshCw, ShieldCheck, Wrench } from 'lucide-react'
import { api, apiErrorMessage, fetchTraces } from '../api'
import {
  fetchV2Status,
  readableReadiness,
  validateRealRepairs,
  v2ErrorMessage,
} from '../v2api'
import type { RealRepairValidation, V2RealApiStatus } from '../v2api'
import { useAdminAuth } from '../components/AdminAuth'
import { useTraceUrlParam } from '../hooks/useTraceUrlParam'
import type { TraceSummary } from '../types'
import { Card, EmptyState, ErrorBox, Spinner, StatusBadge } from '../components/ui'
import { LineagePanel } from '../components/LineagePanel'

interface Candidate {
  repair_id: string
  repair_type: string
  target_step_id: string | null
  target_tool: string | null
  modification: Record<string, unknown>
  expected_effect: string
  risks: string[]
  applicable_conditions: string[]
  verification_status: string
  verification_error?: string | null
  repair_trace_id?: string | null
  experiment_id?: string | null
  outcome_before?: { status: string; trace_id: string; fault_types: string[] } | null
  outcome_after?: { status: string; trace_id: string; fault_types: string[] } | null
  cost_delta?: {
    token_before: number | null
    token_after: number | null
    duration_before_ms: number | null
    duration_after_ms: number | null
    tool_calls_delta: number | null
    missing_metrics: string[]
  } | null
  score?: number | null
  score_breakdown?: Record<string, unknown>
  rank?: number | null
}

interface CandidatesResp {
  trace_id: string
  status: string
  step_count: number
  fault_steps: {
    step_id: string
    step_type: string
    tool_name: string | null
    fault_type: string | null
    status: string
    error_type: string | null
  }[]
  candidate_count: number
  candidates: Candidate[]
  note: string
}

interface RepairReport {
  source_trace_id: string
  fault_type: string | null
  fault_nodes: string[]
  candidate_count: number
  candidates: Candidate[]
  recommended: {
    repair_id: string
    repair_type: string
    score: number
    basis: string[]
    caveat: string
  } | null
  recommendation_basis: string
  unverified: string[]
  original_trace_modified: boolean
  disclaimer: string
}

const STATUS_CLS: Record<string, { cls: string; label: string }> = {
  verified: { cls: 'success', label: '已验证有效' },
  failed: { cls: 'failed', label: '验证未通过' },
  not_tested: { cls: 'muted', label: '未验证' },
}

function fmt(v: number | null | undefined, unit = ''): string {
  return v === null || v === undefined ? '未记录' : `${v}${unit}`
}

// Repair Confidence Breakdown：完全由真实报告字段推导，不生成虚假成功率
function ConfidenceBreakdown({ c }: { c: Candidate }) {
  const bd = c.score_breakdown ?? {}
  const scope = bd.scope as { changed_fields?: number } | undefined
  const latency = bd.latency as { before_ms?: number | null; after_ms?: number | null } | undefined
  const token = bd.token as { before?: number | null; after?: number | null } | undefined
  const toolCalls = bd.tool_calls as { delta?: number | null } | undefined
  const side = bd.side_effect as { risk_count?: number } | undefined
  const evidence = bd.causal_evidence_level as string | undefined

  const rows: { label: string; value: string }[] = [
    { label: '验证状态', value: STATUS_CLS[c.verification_status]?.label ?? c.verification_status },
    { label: '实验次数', value: c.experiment_id ? '1（受控实验 1 次）' : '0（未执行）' },
    { label: '修改范围', value: scope?.changed_fields != null ? `${scope.changed_fields} 个字段` : '未记录' },
    {
      label: '延迟成本',
      value: `${fmt(latency?.before_ms, 'ms')} → ${fmt(latency?.after_ms, 'ms')}`,
    },
    { label: 'Token 成本', value: `${fmt(token?.before)} → ${fmt(token?.after)}` },
    { label: '工具调用成本', value: toolCalls?.delta != null ? `${toolCalls.delta}` : '未记录' },
    { label: '风险等级', value: side?.risk_count != null ? `${side.risk_count} 项风险` : '未记录' },
    { label: '证据限制', value: evidence ? `证据等级 ${evidence}` : '模拟环境，未验证真实生产' },
  ]

  return (
    <div className="cf-note" style={{ marginTop: 10 }}>
      <strong>修复候选评估</strong>
      <table className="tbl" style={{ marginTop: 8 }}>
        <tbody>
          {rows.map((r) => (
            <tr key={r.label}>
              <td className="small muted" style={{ width: 190 }}>{r.label}</td>
              <td className="small">{r.value}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small" style={{ margin: '8px 0 0' }}>
        以上为模拟环境下的验证元数据，不代表生产结果。
      </p>
    </div>
  )
}

export default function RepairStudio() {
  const navigate = useNavigate()
  const { traceId: urlTraceId, stepId: urlStepId, setTraceId: setUrlTraceId } = useTraceUrlParam()
  const [traces, setTraces] = useState<TraceSummary[]>([])
  const [traceId, setTraceId] = useState('')
  const [eligibility, setEligibility] = useState<{
    eligible: boolean
    reasons: string[]
    requirements: string[]
  } | null>(null)
  const [cands, setCands] = useState<CandidatesResp | null>(null)
  const [loadingCands, setLoadingCands] = useState(false)
  const [busy, setBusy] = useState(false)
  const [reportId, setReportId] = useState<string | null>(null)
  const [report, setReport] = useState<RepairReport | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // 真实 API 修复验证（会消耗真实模型预算；需管理员会话 + 服务端预算）
  const admin = useAdminAuth()
  const [realStatus, setRealStatus] = useState<V2RealApiStatus | null>(null)
  const [realStatusError, setRealStatusError] = useState<string | null>(null)
  const [realMaxCandidates, setRealMaxCandidates] = useState(2)
  const [realBusy, setRealBusy] = useState(false)
  const [realError, setRealError] = useState<string | null>(null)
  const [realResult, setRealResult] = useState<RealRepairValidation | null>(null)

  useEffect(() => {
    let cancelled = false
    void fetchV2Status()
      .then((s) => {
        if (cancelled) return
        setRealStatus(s.real_api)
        setRealStatusError(null)
      })
      .catch((e) => {
        if (cancelled) return
        setRealStatus(null)
        setRealStatusError(v2ErrorMessage(e))
      })
    return () => {
      cancelled = true
    }
  }, [])

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
      // URL 指定 Trace 优先；否则优先选中「故障类型可生成修复候选」的 Trace
      const REPAIRABLE_FAULTS = ['tool_timeout', 'tool_exception', 'invalid_arguments', 'permission_denied']
      const repairable = t.traces.find((x) =>
        x.fault_types.some((ft) => REPAIRABLE_FAULTS.includes(ft)),
      )
      const fallback = t.traces.find((x) => x.fault_types.length > 0)
      const chosen = urlHit ?? repairable ?? fallback
      // 仅当用户尚未选择时设置默认值（异步响应不得覆盖用户选择）
      if (urlHit) setTraceId(urlHit.trace_id)
      else if (chosen) setTraceId((prev) => prev || chosen.trace_id)
    } catch (e) {
      setError(apiErrorMessage(e))
    }
  }, [urlTraceId])

  useEffect(() => {
    void loadTraces()
  }, [loadTraces])

  // 读取修复候选资格（第 55-A 步治理接口）：不满足时解释原因，避免只看到「无候选」
  useEffect(() => {
    if (!traceId) {
      setEligibility(null)
      return
    }
    let cancelled = false
    void api
      .get<{ eligibility: { capabilities: { repair_candidates: { eligible: boolean; reasons: string[]; requirements: string[] } } } }>(
        `/api/governance/traces/${encodeURIComponent(traceId)}/eligibility`,
        { timeout: 60000 },
      )
      .then((r) => {
        if (!cancelled) setEligibility(r.data.eligibility.capabilities.repair_candidates)
      })
      .catch(() => {
        if (!cancelled) setEligibility(null)
      })
    return () => {
      cancelled = true
    }
  }, [traceId])

  const loadCandidates = useCallback(async (tid: string) => {
    if (!tid) return
    setLoadingCands(true)
    setError(null)
    setCands(null)
    setReport(null)
    setReportId(null)
    setSelected(null)
    try {
      const r = await api.get<CandidatesResp>(`/api/repairs/candidates/${encodeURIComponent(tid)}`)
      setCands(r.data)
      const first = r.data.candidates[0]
      setSelected(first ? first.repair_id : null)
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setLoadingCands(false)
    }
  }, [])

  useEffect(() => {
    if (traceId) void loadCandidates(traceId)
  }, [traceId, loadCandidates])

  async function validateRepair() {
    if (!traceId) return
    setBusy(true)
    setError(null)
    setReport(null)
    try {
      const create = await api.post<{ report_id: string; status: string }>('/api/repairs/validate', {
        trace_id: traceId,
        max_candidates: 5,
      })
      setReportId(create.data.report_id)
      const rid = create.data.report_id
      // 有界轮询
      for (let i = 0; i < 300; i++) {
        const st = await api.get<{ status: string; error?: string }>(`/api/repairs/reports/${rid}`)
        if (st.data.status !== 'running') {
          if (st.data.status === 'failed') setError(st.data.error ?? '修复验证失败')
          break
        }
        await new Promise((r) => setTimeout(r, 200))
      }
      const full = await api.get<{ status: string; result: RepairReport | null }>(
        `/api/repairs/reports/${rid}`,
      )
      if (full.data.result) {
        setReport(full.data.result)
        // 验证后的候选是重新生成的对象（repair_id 与验证前不同），
        // 必须重新选中，否则右侧详情/证据面板会变为空。
        const rec = full.data.result.recommended
        const first = full.data.result.candidates[0]
        const next = rec?.repair_id ?? first?.repair_id ?? null
        if (next) setSelected(next)
      }
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  async function validateReal() {
    if (!traceId) return
    setRealBusy(true)
    setRealError(null)
    setRealResult(null)
    try {
      setRealResult(await validateRealRepairs(traceId, realMaxCandidates))
    } catch (e) {
      setRealError(v2ErrorMessage(e))
    } finally {
      setRealBusy(false)
    }
  }

  const realAvailable = realStatus?.available === true
  const realBlockers = readableReadiness(realStatus?.blocking_reasons)
  const realAdminRequired = admin.session?.required === true
  const realAdminReady = admin.session
    ? !realAdminRequired || admin.session.authenticated
    : admin.error !== null
  const realDisabled = realBusy || !traceId || !realAvailable || !realAdminReady

  const shown: Candidate[] = report?.candidates ?? cands?.candidates ?? []
  const selectedCandidate = shown.find((c) => c.repair_id === selected)

  return (
    <div className="stack">
      <div className="row">
        <h2 style={{ margin: 0 }}>Repair Studio</h2>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => void loadTraces()}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>
      <p className="muted small" style={{ margin: 0 }}>
        生成最小修复候选，并用受控反事实实验验证。原始 Trace 只读，结果为模拟环境。
      </p>

      {error && <ErrorBox message={error} />}

      <Card title="选择故障 Trace">
        <div className="stack">
          <select
            value={traceId}
            onChange={(e) => {
              setTraceId(e.target.value)
              setUrlTraceId(e.target.value)
            }}
            aria-label="选择 Trace"
          >
            {traces.length === 0 && <option value="">Trace 列表为空（加载失败或目录无数据）</option>}
            {traces.map((t) => (
              <option key={t.trace_id} value={t.trace_id}>
                {t.trace_id}{t.fault_types.length ? ' · 故障: ' + t.fault_types.join('/') : ''}
              </option>
            ))}
          </select>

          {eligibility && !eligibility.eligible && (
            <div className="errorbox" style={{ background: 'var(--warning-soft)', color: 'var(--warning)' }}>
              该 Trace 不满足修复候选条件：{eligibility.reasons.join('；')}
            </div>
          )}
          {urlStepId && (
            <div className="cf-note small">
              来源节点：<span className="mono">{urlStepId}</span>
              （候选以整条 Trace 为单位）
            </div>
          )}
          {cands && cands.candidates.length === 0 && (
            <div className="cf-note small">
              该 Trace 未生成修复候选：{eligibility?.reasons.join('；') ||
                '故障类型不在可修复白名单内，或缺少可定位的失败步骤'}
            </div>
          )}

          {cands && (
            <div className="cf-info">
              <div><span className="muted">状态</span> <StatusBadge status={cands.status} /></div>
              <div><span className="muted">步骤</span> {cands.step_count}</div>
              <div><span className="muted">故障节点</span> {cands.fault_steps.length}</div>
              <div className="mono small">{cands.trace_id}</div>
            </div>
          )}

          {cands && cands.fault_steps.length > 0 && (
            <table className="tbl">
              <thead>
                <tr><th>故障节点</th><th>类型</th><th>工具</th><th>故障类型</th><th>状态</th></tr>
              </thead>
              <tbody>
                {cands.fault_steps.map((f) => (
                  <tr key={f.step_id}>
                    <td className="mono small">{f.step_id}</td>
                    <td className="small">{f.step_type}</td>
                    <td className="small">{f.tool_name ?? '—'}</td>
                    <td><span className="badge failed">{f.fault_type ?? '—'}</span></td>
                    <td><StatusBadge status={f.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div className="row">
            <button
              className="btn primary"
              onClick={() => void validateRepair()}
              disabled={busy || !traceId || eligibility?.eligible === false}
              title={eligibility?.eligible === false ? eligibility.reasons.join('；') : undefined}
            >
              <ShieldCheck size={15} /> 验证修复（全部候选）
            </button>
            {busy && <span className="muted small">验证中…</span>}
            {reportId && <span className="mono small muted">{reportId}</span>}
          </div>
          {cands && <p className="muted small" style={{ margin: 0 }}>{cands.note}</p>}
        </div>
      </Card>

      {loadingCands && <Spinner />}
      {busy && <Spinner />}

      {!loadingCands && !busy && shown.length === 0 && !error && (
        <EmptyState text="该 Trace 无可用修复候选（无故障或故障类型不在白名单）" />
      )}

      {shown.length > 0 && (
        <div className="rs-layout">
          <Card title={`修复候选（${shown.length}）`}>
            <div className="stack">
              {shown.map((c) => {
                const meta = STATUS_CLS[c.verification_status] ?? STATUS_CLS.not_tested
                return (
                  <button
                    key={c.repair_id}
                    className={'rs-card' + (selected === c.repair_id ? ' active' : '')}
                    onClick={() => setSelected(c.repair_id)}
                  >
                    <div className="row" style={{ justifyContent: 'space-between' }}>
                      <span className="row" style={{ gap: 8 }}>
                        {c.rank != null && <span className="badge muted">#{c.rank}</span>}
                        <span className="badge blue">{c.repair_type}</span>
                        <span className={'badge ' + meta.cls}>{meta.label}</span>
                      </span>
                      {c.score != null && <span className="small muted">score {c.score}</span>}
                    </div>
                    <div className="small muted" style={{ marginTop: 6 }}>{c.expected_effect}</div>
                  </button>
                )
              })}
            </div>
          </Card>

          <div className="stack">
            {report?.recommended && (
              <Card title="推荐方案">
                <div className="stack small">
                  <div className="row">
                    <span className="badge success">{report.recommended.repair_type}</span>
                    <span className="mono small">{report.recommended.repair_id}</span>
                    <span className="muted">score {report.recommended.score}</span>
                  </div>
                  <div>
                    <strong>推荐依据</strong>
                    <ul style={{ margin: '4px 0 0 16px' }}>
                      {report.recommended.basis.map((b) => <li key={b}>{b}</li>)}
                    </ul>
                  </div>
                  <div className="muted">{report.recommended.caveat}</div>
                  <div className="muted">{report.recommendation_basis}</div>
                </div>
              </Card>
            )}

            {selectedCandidate && (
              <Card title="候选详情">
                <div className="stack small">
                  <div className="row">
                    <span className={'badge ' + (STATUS_CLS[selectedCandidate.verification_status] ?? STATUS_CLS.not_tested).cls}>
                      {(STATUS_CLS[selectedCandidate.verification_status] ?? STATUS_CLS.not_tested).label}
                    </span>
                    <span className="mono small">{selectedCandidate.repair_id}</span>
                  </div>

                  <div><div className="k muted">修改内容</div>
                    <pre className="json">{JSON.stringify(selectedCandidate.modification, null, 2)}</pre>
                  </div>
                  <div><div className="k muted">预期作用</div><div>{selectedCandidate.expected_effect}</div></div>
                  <div><div className="k muted">适用条件</div>
                    <ul style={{ margin: '4px 0 0 16px' }}>
                      {selectedCandidate.applicable_conditions.map((x) => <li key={x}>{x}</li>)}
                    </ul>
                  </div>
                  <div><div className="k muted">潜在风险</div>
                    <ul style={{ margin: '4px 0 0 16px' }}>
                      {selectedCandidate.risks.map((x) => <li key={x}>{x}</li>)}
                    </ul>
                  </div>

                  {selectedCandidate.verification_error && (
                    <div className="errorbox">{selectedCandidate.verification_error}</div>
                  )}

                  {selectedCandidate.outcome_before && selectedCandidate.outcome_after && (
                    <div className="cf-track">
                      <div className="cf-track-col">
                        <div className="cf-track-head">修复前</div>
                        <div>状态 <StatusBadge status={selectedCandidate.outcome_before.status} /></div>
                        <div>故障 {selectedCandidate.outcome_before.fault_types.join(', ') || '无'}</div>
                        <div>耗时 {fmt(selectedCandidate.cost_delta?.duration_before_ms, 'ms')}</div>
                        <div>Token {fmt(selectedCandidate.cost_delta?.token_before)}</div>
                      </div>
                      <div className="cf-track-arrow">→</div>
                      <div className="cf-track-col">
                        <div className="cf-track-head">修复后</div>
                        <div>状态 <StatusBadge status={selectedCandidate.outcome_after.status} /></div>
                        <div>故障 {selectedCandidate.outcome_after.fault_types.join(', ') || '无'}</div>
                        <div>耗时 {fmt(selectedCandidate.cost_delta?.duration_after_ms, 'ms')}</div>
                        <div>Token {fmt(selectedCandidate.cost_delta?.token_after)}</div>
                      </div>
                    </div>
                  )}

                  {selectedCandidate.cost_delta && (
                    <div className="muted">
                      工具调用次数变化：{fmt(selectedCandidate.cost_delta.tool_calls_delta)}
                      {selectedCandidate.cost_delta.missing_metrics.length > 0 && (
                        <> （缺失指标：{selectedCandidate.cost_delta.missing_metrics.join(', ')}）</>
                      )}
                    </div>
                  )}

                  <ConfidenceBreakdown c={selectedCandidate} />

                  <div className="row">
                    {selectedCandidate.repair_trace_id && (
                      <button className="btn" onClick={() => navigate('/dag/' + selectedCandidate.repair_trace_id)}>
                        查看修复后 DAG
                      </button>
                    )}
                    <button className="btn" onClick={() => navigate('/traces/' + traceId)}>
                      查看原始 Trace
                    </button>
                  </div>
                </div>
              </Card>
            )}

            {report && (
              <>
                <Card title="任务结果变化">
                  <div className="stack small">
                    {Object.entries(report.candidates.reduce<Record<string, string>>((acc, c) => {
                      acc[c.repair_id] = c.outcome_after?.status ?? '未记录'
                      return acc
                    }, {})).map(([rid, st]) => (
                      <div key={rid} className="row" style={{ justifyContent: 'space-between' }}>
                        <span className="mono small">{rid}</span>
                        <StatusBadge status={st} />
                      </div>
                    ))}
                    <div className="muted">原始 Trace 是否被修改：{report.original_trace_modified ? '是' : '否'}</div>
                  </div>
                </Card>

                <Card title="风险与限制">
                  <div className="stack small">
                    <div className="muted">{report.disclaimer}</div>
                    <div>
                      <strong>未验证事项</strong>
                      <ul style={{ margin: '4px 0 0 16px' }}>
                        {report.unverified.map((u) => <li key={u}>{u}</li>)}
                      </ul>
                    </div>
                  </div>
                </Card>
              </>
            )}
          </div>
        </div>
      )}

      {!traceId && !error && <EmptyState text="请选择一条故障 Trace" />}

      <Card
        title="真实 API 受控实验（修复验证）"
        action={
          <span className={'badge ' + (realAvailable ? 'partial' : 'muted')}>
            {realAvailable ? '付费 · 真实调用' : '不可用'}
          </span>
        }
      >
        <div className="stack">
          <p className="muted small" style={{ margin: 0 }}>
            对当前选中 Trace 的修复候选，用真实 DeepSeek API 逐个重放验证。真实执行只能表达「调整超时/重试」与
            「修正参数」类候选；需要伪造工具返回的候选会被服务端明确拒绝，而不是假装验证。
          </p>

          <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
            <span className="badge muted">目标 Trace</span>
            <span className="mono small">{traceId || '未选择 Trace'}</span>
            {realStatus && (
              <span className="badge muted">真实 API {realAvailable ? '可用' : '不可用'}</span>
            )}
            {admin.session && (
              <span className={'badge ' + (admin.session.authenticated ? 'success' : 'muted')}>
                管理员 {admin.session.authenticated ? '已认证' : realAdminRequired ? '未登录（必需）' : '未登录'}
              </span>
            )}
            {cands && <span className="badge muted">候选数 {cands.candidate_count}</span>}
          </div>

          <div className="field">
            <div className="k">最多验证候选数（1–3，每个候选 1 次真实重放）</div>
            <input
              type="number"
              min={1}
              max={3}
              value={realMaxCandidates}
              onChange={(e) => setRealMaxCandidates(Number(e.target.value))}
            />
          </div>

          <div className="errorbox" style={{ background: 'var(--warning-soft)', color: 'var(--warning)' }}>
            <strong>该操作会消耗真实模型预算</strong>：每个候选都会发起真实模型调用，按上游调用次数扣减服务端
            持久预算，费用不可撤销；预算不足时服务端直接拒绝（不会发出上游请求）。
          </div>

          {realStatusError && (
            <div className="muted small">无法读取 V2 真实 API 状态：{realStatusError}</div>
          )}
          {!realStatus && !realStatusError && (
            <div className="muted small">正在读取 V2 真实 API 状态…</div>
          )}
          {realStatus && !realAvailable && (
            <div className="errorbox" style={{ background: 'var(--warning-soft)', color: 'var(--warning)' }}>
              真实 API 不可用：{realBlockers.length ? realBlockers.join('；') : '服务端未开启或预算未配置'}
            </div>
          )}
          {!realAdminReady && (
            <div className="cf-note small">
              需要管理员登录：请先打开「V2 实验」页完成管理员登录，再回到本页执行真实验证。
            </div>
          )}
          {!traceId && <div className="muted small">请先在上方选择一条故障 Trace。</div>}

          <div className="row" style={{ gap: 10 }}>
            <button
              className="btn primary"
              type="button"
              onClick={() => void validateReal()}
              disabled={realDisabled}
              title={
                !realAvailable ? '真实 API 不可用' : !realAdminReady ? '需要管理员登录' : !traceId ? '请先选择 Trace' : undefined
              }
            >
              <Wrench size={15} /> {realBusy ? '真实 API 验证中…' : '真实 API 修复验证'}
            </button>
            <button className="btn" type="button" onClick={() => navigate('/v2')}>
              V2 实验页（登录 / 预算） <ArrowRight size={14} />
            </button>
            {realBusy && <span className="muted small">真实模型调用可能需要数十秒…</span>}
          </div>

          {realError && <ErrorBox message={realError} />}

          {realResult && (
            <div className="cf-note">
              <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                <span className="badge success">execution_mode: {realResult.execution_mode}</span>
                <span className="badge muted">
                  候选 {realResult.candidate_count} · 真实验证通过 {realResult.executed_count}
                </span>
                {realResult.paid_api_called && <span className="badge partial">已发生真实调用</span>}
              </div>
              <div className="stack small" style={{ marginTop: 10 }}>
                {realResult.candidates.length === 0 && (
                  <div className="muted">该 Trace 没有可由真实执行表达的修复候选。</div>
                )}
                {realResult.candidates.map((candidate) => {
                  const statusMeta =
                    candidate.status === 'verified_real'
                      ? { cls: 'success', label: '真实验证通过' }
                      : candidate.status === 'rejected_not_real_executable'
                        ? { cls: 'partial', label: '真实执行不可表达' }
                        : candidate.status === 'failed'
                          ? { cls: 'failed', label: '真实验证失败' }
                          : { cls: 'muted', label: candidate.status }
                  const outcomes = candidate.outcome ?? []
                  const tokens = outcomes.reduce(
                    (sum, item) => sum + (typeof item.total_tokens === 'number' ? item.total_tokens : 0),
                    0,
                  )
                  const hasToken = outcomes.some((item) => typeof item.total_tokens === 'number')
                  return (
                    <div key={candidate.repair_id} className="ev-box">
                      <div className="row" style={{ gap: 8 }}>
                        <span className={'badge ' + statusMeta.cls}>{statusMeta.label}</span>
                        <span className="badge blue">{candidate.repair_type}</span>
                        <span className="mono" style={{ fontSize: 11 }}>
                          {candidate.repair_id}
                        </span>
                      </div>
                      {candidate.reason && (
                        <div className="muted" style={{ marginTop: 4 }}>{candidate.reason}</div>
                      )}
                      {outcomes.map((outcome) => (
                        <div
                          key={outcome.trace_id}
                          className="row"
                          style={{ justifyContent: 'space-between', gap: 8, marginTop: 4 }}
                        >
                          <span className="row" style={{ gap: 8 }}>
                            <StatusBadge status={outcome.status} />
                            <span className="mono" style={{ fontSize: 11 }}>{outcome.trace_id}</span>
                          </span>
                          <span className="muted">
                            Token {outcome.total_tokens ?? '未记录'} · 延迟{' '}
                            {outcome.elapsed_ms === null || outcome.elapsed_ms === undefined
                              ? '未记录'
                              : `${outcome.elapsed_ms} ms`}
                          </span>
                        </div>
                      ))}
                      {candidate.causal_evidence_level && (
                        <div className="muted" style={{ marginTop: 4 }}>
                          因果证据等级：{candidate.causal_evidence_level}
                        </div>
                      )}
                      {outcomes.length > 0 && (
                        <div className="muted" style={{ marginTop: 4 }}>
                          合计 Token：{hasToken ? tokens : '未记录'}
                        </div>
                      )}
                    </div>
                  )
                })}
                {(realResult.limitations.length ?? 0) > 0 && (
                  <div>
                    <strong>限制</strong>
                    <ul style={{ margin: '2px 0 0 16px' }}>
                      {realResult.limitations.map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </Card>

      {reportId && (
        <Card title="证据血缘（Evidence Lineage）">
          <LineagePanel kind="repair" ids={{ reportId }} />
        </Card>
      )}
    </div>
  )
}

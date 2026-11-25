import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, RefreshCw } from 'lucide-react'
import {
  api,
  apiErrorMessage,
  fetchTraceClassification,
  fetchTrace,
  fetchTraceEligibility,
} from '../api'
import type {
  TraceClassificationResponse,
  TraceDetail,
  TraceEligibility,
} from '../types'
import { Card, EmptyState, ErrorBox, Spinner, StatusBadge } from '../components/ui'
import { LineagePanel } from '../components/LineagePanel'

interface Explanation {
  summary: string
  rationale: {
    step_id: string
    step_type: string
    status: string
    decision_summary: string
    evidence: string
  }[]
  errors: { step_id: string; error_type: string; message: string }[]
  is_chain_of_thought: boolean
  limitations: string[]
}

const CAPABILITY_LABELS: Record<string, string> = {
  recorded_replay: '只读重放',
  controlled_rerun: '受控重跑',
  counterfactual_experiment: '反事实实验',
  repair_candidates: '修复候选生成',
}

export default function TraceDetailPage() {
  const { traceId } = useParams()
  const navigate = useNavigate()
  const [trace, setTrace] = useState<TraceDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [explanation, setExplanation] = useState<Explanation | null>(null)
  const [classification, setClassification] =
    useState<TraceClassificationResponse['classification'] | null>(null)
  const [eligibility, setEligibility] = useState<TraceEligibility | null>(null)

  const load = useCallback(async () => {
    if (!traceId) return
    setLoading(true)
    setError(null)
    setExplanation(null)
    setClassification(null)
    setEligibility(null)
    try {
      const r = await fetchTrace(traceId)
      setTrace(r.trace)
      const [explanationResult, classificationResult, eligibilityResult] = await Promise.allSettled([
        api.get<Explanation>(`/api/traces/${encodeURIComponent(traceId)}/explanation`, {
          timeout: 60000,
        }),
        fetchTraceClassification(traceId),
        fetchTraceEligibility(traceId),
      ])
      // 解释 / 分类 / 资格均为辅助信息：单项失败不阻断 Trace 详情展示
      if (explanationResult.status === 'fulfilled') setExplanation(explanationResult.value.data)
      if (classificationResult.status === 'fulfilled') {
        setClassification(classificationResult.value.classification)
      }
      if (eligibilityResult.status === 'fulfilled') setEligibility(eligibilityResult.value)
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setLoading(false)
    }
  }, [traceId])

  useEffect(() => {
    void load()
  }, [load])


  return (
    <div className="stack">
      <div className="row">
        <button className="btn" onClick={() => navigate('/traces')}>
          <ArrowLeft size={15} /> 返回
        </button>
        <h2 style={{ margin: 0 }}>Trace 详情</h2>
        <div style={{ flex: 1 }} />
        {traceId && (
          <>
            <button className="btn" onClick={() => navigate('/dag/' + traceId)}>
              查看 DAG
            </button>
            <button className="btn" onClick={() => navigate('/attribution?trace_id=' + encodeURIComponent(traceId))}>
              故障归因
            </button>
            <button className="btn" onClick={() => navigate('/counterfactual?trace_id=' + encodeURIComponent(traceId))}>
              反事实
            </button>
            <button className="btn" onClick={() => navigate('/repair?trace_id=' + encodeURIComponent(traceId))}>
              修复
            </button>
          </>
        )}
        <button className="btn" onClick={() => void load()}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>

      {error && <ErrorBox message={error} />}
      {loading && <Spinner />}

      {!loading && !error && trace && (
        <>
          <Card title="概览">
            <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))' }}>
              <div><div className="k muted small">trace_id</div><div className="mono">{trace.trace_id}</div></div>
              <div><div className="k muted small">task_id</div><div className="mono">{trace.task_id}</div></div>
              <div><div className="k muted small">run_id</div><div className="mono">{trace.run_id}</div></div>
              <div><div className="k muted small">状态</div><StatusBadge status={trace.status} /></div>
              <div>
                <div className="k muted small">数据来源</div>
                {classification ? (
                  <SourceClassBadge sourceClass={classification.source_class} />
                ) : (
                  <DataSourceTag metadata={trace.metadata} />
                )}
              </div>
              <div><div className="k muted small">开始时间</div><div className="small">{new Date(trace.started_at).toLocaleString()}</div></div>
              <div><div className="k muted small">结束时间</div><div className="small">{trace.ended_at ? new Date(trace.ended_at).toLocaleString() : '—'}</div></div>
            </div>
          </Card>

          <Card title="数据来源分类">
            {!classification ? (
              <EmptyState text="来源分类暂不可用" />
            ) : (
              <div className="stack small">
                <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                  <SourceClassBadge sourceClass={classification.source_class} />
                  <span className="muted small">
                    {classification.source_definitions?.[classification.source_class]}
                  </span>
                </div>
                <div className="muted small">
                  判定依据：{classification.reasons.length ? classification.reasons.join('；') : '无可用标记'}
                </div>
                <div className="muted small">
                  {classification.definitions_note}
                  {classification.evidence.length > 0 && (
                    <> · 证据标记：{classification.evidence.join('、')}</>
                  )}
                </div>
              </div>
            )}
          </Card>

          <Card title="受控实验资格">
            {!eligibility ? (
              <EmptyState text="资格判断不可用（不影响 Trace 详情展示）" />
            ) : (
              <div className="stack small">
                <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                  <span className="badge muted">状态 {eligibility.status}</span>
                  <span className="badge muted">步骤 {eligibility.step_count}</span>
                  <span className="badge muted">工具步骤 {eligibility.tool_step_count}</span>
                  {eligibility.fault_events.map((f) => (
                    <span key={f.step_id} className="badge warning">
                      {f.fault_type ?? 'fault'} @ {f.step_id}
                    </span>
                  ))}
                </div>
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>操作</th>
                      <th>资格</th>
                      <th>原因 / 要求</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(eligibility.capabilities).map(([key, cap]) => (
                      <tr key={key}>
                        <td className="small">{CAPABILITY_LABELS[key] ?? key}</td>
                        <td>
                          <span className={`badge ${cap.eligible ? 'success' : 'failed'}`}>
                            {cap.eligible ? '可用' : '不可用'}
                          </span>
                        </td>
                        <td className="small muted">
                          {cap.eligible
                            ? cap.requirements.join('；')
                            : cap.reasons.join('；')}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="muted small">{eligibility.note}</div>
              </div>
            )}
          </Card>

          <Card title={'执行步骤（' + trace.steps.length + '）'}>
            {trace.steps.length === 0 ? (
              <EmptyState text="无步骤" />
            ) : (
              <table className="tbl">
                <thead>
                  <tr>
                    <th>步骤</th>
                    <th>类型</th>
                    <th>状态</th>
                    <th>工具</th>
                    <th>耗时(ms)</th>
                    <th>Token</th>
                    <th>错误</th>
                  </tr>
                </thead>
                <tbody>
                  {trace.steps.map((s) => (
                    <tr key={s.step_id}>
                      <td className="mono">{s.step_id}</td>
                      <td className="small">{s.step_type}</td>
                      <td><StatusBadge status={s.status} /></td>
                      <td className="small">{s.tool_name ?? '—'}</td>
                      <td>{s.duration_ms ?? '—'}</td>
                      <td>{s.token_usage ? s.token_usage.total_tokens : '—'}</td>
                      <td className="small">{s.error ? s.error.error_type : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </>
      )}
      {trace && (
        <Card title="决策解释">
          {!explanation ? (
            <EmptyState text="解释暂不可用：仅使用已记录证据" />
          ) : (
            <div className="stack small">
              <div>{explanation.summary}</div>
              <div className="badge muted">
                {explanation.is_chain_of_thought
                  ? '包含思维链'
                  : '证据化理由（非思维链）'}
              </div>
              <div className="lineage-table-wrap" style={{ maxHeight: 260, overflow: 'auto' }}>
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>步骤</th>
                      <th>类型</th>
                      <th>状态</th>
                      <th>决策摘要</th>
                      <th>证据</th>
                    </tr>
                  </thead>
                  <tbody>
                    {explanation.rationale.map((item) => (
                      <tr key={item.step_id}>
                        <td className="mono small">{item.step_id}</td>
                        <td className="small">{item.step_type}</td>
                        <td className="small">{item.status}</td>
                        <td className="small">{item.decision_summary}</td>
                        <td className="small muted">{item.evidence}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {explanation.errors.length > 0 && (
                <div className="muted small">
                  失败步骤：{explanation.errors.map((e) => `${e.step_id}（${e.error_type}）`).join('；')}
                </div>
              )}
              <div className="muted small">{explanation.limitations.join('；')}</div>
            </div>
          )}
        </Card>
      )}
      {trace && (
        <Card title="证据血缘（Evidence Lineage）">
          <LineagePanel kind="trace" ids={{ traceId: trace.trace_id }} />
        </Card>
      )}

    </div>
  )
}

/** 来源分类徽标：REAL_API 只在后端分类为 REAL_API 时展示，绝不自动升级。 */
function SourceClassBadge({ sourceClass }: { sourceClass: string }) {
  if (sourceClass === 'REAL_API') return <span className="badge success">REAL_API</span>
  if (sourceClass === 'MOCK') return <span className="badge warning">MOCK / 模拟</span>
  if (sourceClass === 'REPLAY') return <span className="badge blue">REPLAY</span>
  if (sourceClass === 'SIMULATED') return <span className="badge muted">SIMULATED</span>
  return <span className="badge failed">UNKNOWN</span>
}

/**
 * 数据来源标记（分类接口不可用时的兜底）：严格区分 real_api / mock / simulated。
 * data_source=real_api 但缺少 real_api_verified 时按 UNKNOWN 展示，不自动升级为真实。
 */
function DataSourceTag({ metadata }: { metadata: Record<string, unknown> }) {
  const ds = String(metadata?.data_source ?? '')
  const simulated = metadata?.simulated === true
  const verified = metadata?.real_api_verified === true
  if (ds === 'real_api' && !simulated && verified) {
    return <span className="badge success">REAL API</span>
  }
  if (ds === 'real_api' && !verified) {
    return <span className="badge failed">UNKNOWN（real_api 未验证）</span>
  }
  if (ds === 'mock' || simulated) {
    return <span className="badge warning">MOCK / 模拟</span>
  }
  if (ds === 'simulated') {
    return <span className="badge muted">SIMULATED</span>
  }
  return <span className="badge failed">UNKNOWN（未标记）</span>
}
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, RefreshCw } from 'lucide-react'
import {
  apiErrorMessage,
  fetchFailedSummary,
  fetchGovernanceSources,
  fetchHealth,
  fetchTraces,
} from '../api'
import { fetchV2Status, readableReadiness } from '../v2api'
import type { V2Status } from '../v2api'
import type { FailedSummaryResponse, GovernanceSourcesResponse, Health, TraceSummary } from '../types'
import { Card, EmptyState, ErrorBox, Spinner, StatCard, StatusBadge } from '../components/ui'

/** 来源分类徽标：REAL_API 为成功色，其余为中性/警告色（绝不把模拟数据标成真实）。 */
function SourceBadge({ sourceClass }: { sourceClass: string }) {
  if (sourceClass === 'REAL_API') return <span className="badge success">REAL_API</span>
  if (sourceClass === 'MOCK') return <span className="badge warning">MOCK</span>
  if (sourceClass === 'REPLAY') return <span className="badge blue">REPLAY</span>
  if (sourceClass === 'SIMULATED') return <span className="badge muted">SIMULATED</span>
  return <span className="badge failed">UNKNOWN</span>
}

export default function Overview() {
  const navigate = useNavigate()
  const [health, setHealth] = useState<Health | null>(null)
  const [traces, setTraces] = useState<TraceSummary[]>([])
  const [sources, setSources] = useState<GovernanceSourcesResponse | null>(null)
  const [failed, setFailed] = useState<FailedSummaryResponse | null>(null)
  const [v2, setV2] = useState<V2Status | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    // Trace 列表是首屏核心数据；健康检查/来源统计为辅助信息，单项失败不应让整页报错。
    const [healthResult, tracesResult, sourcesResult, failedResult, v2Result] =
      await Promise.allSettled([
        fetchHealth(),
        fetchTraces(),
        fetchGovernanceSources(),
        fetchFailedSummary(6),
        fetchV2Status(),
      ])
    if (tracesResult.status === 'fulfilled') setTraces(tracesResult.value.traces)
    else setError(apiErrorMessage(tracesResult.reason))
    setHealth(healthResult.status === 'fulfilled' ? healthResult.value : null)
    if (sourcesResult.status === 'fulfilled') setSources(sourcesResult.value)
    if (failedResult.status === 'fulfilled') setFailed(failedResult.value)
    setV2(v2Result.status === 'fulfilled' ? v2Result.value : null)
    setLoading(false)
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const succeeded = traces.filter((t) => t.status === 'succeeded').length
  const failedTraces = traces.filter((t) => t.status === 'failed').length
  const realApiCount = sources?.by_source.REAL_API ?? 0
  const agentBySource = sources?.by_scope_source.agent ?? {}

  return (
    <div className="stack">
      <div className="row">
        <div>
          <h2 style={{ margin: 0 }}>Overview</h2>
          <p className="muted small" style={{ margin: '4px 0 0' }}>
            Agent 轨迹分析、故障归因与修复验证
          </p>
        </div>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => void load()}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>

      {error && <ErrorBox message={error} />}
      {loading && <Spinner />}

      {v2 && (
        <div
          className="row"
          style={{
            gap: 10,
            padding: '10px 14px',
            background: 'var(--surface)',
            border: '1px solid var(--border)',
            borderRadius: 8,
            boxShadow: 'var(--shadow)',
          }}
        >
          <span className="badge blue">V2</span>
          <span className="small muted">真实 API</span>
          <span className={'badge ' + (v2.real_api.available ? 'success' : 'muted')}>
            {v2.real_api.available ? '可用' : '不可用'}
          </span>
          {!v2.real_api.available && (
            <span
              className="small muted"
              title={readableReadiness(v2.real_api.blocking_reasons).join('；') || undefined}
            >
              原因见 V2 实验页
            </span>
          )}
          <span className="small muted">
            预算剩余 {v2.budget.total_calls_remaining} / {v2.budget.total_calls_limit}
          </span>
          <span className="small muted">
            估算费用 ¥{(v2.budget.estimated_cost_cny ?? 0).toFixed(4)}（估算，非 Provider 账单）
          </span>
          <span className="small muted">
            公开 Benchmark {v2.benchmark.available ? `已生成 · ${v2.benchmark.case_count ?? '—'} 例` : '尚未生成'}
          </span>
          <div style={{ flex: 1 }} />
          <button className="btn" onClick={() => navigate('/v2')}>
            V2 实验 <ArrowRight size={14} />
          </button>
        </div>
      )}

      {!loading && !error && (
        <>
          <div className="grid stats">
            <StatCard label="后端连接状态" value={health ? '已连接' : '离线'} tone={health ? 'green' : 'red'} />
            <StatCard label="主目录 Trace 数" value={traces.length} tone="blue" />
            <StatCard label="成功执行" value={succeeded} tone="green" />
            <StatCard label="失败执行" value={failedTraces} tone="red" />
          </div>

          <div className="grid stats mt">
            <StatCard
              label="REAL_API（全库）"
              value={realApiCount}
              tone={realApiCount > 0 ? 'green' : 'red'}
            />
            <StatCard label="MOCK（全库）" value={sources?.by_source.MOCK ?? 0} tone="blue" />
            <StatCard label="SIMULATED（全库）" value={sources?.by_source.SIMULATED ?? 0} tone="blue" />
            <StatCard
              label="REPLAY / UNKNOWN（全库）"
              value={`${sources?.by_source.REPLAY ?? 0} / ${sources?.by_source.UNKNOWN ?? 0}`}
              tone={sources?.by_source.UNKNOWN ? 'red' : 'green'}
            />
          </div>

          <div className="grid grid-2 mt">
            <Card title="数据范围与来源">
            <ul className="vc-limits" style={{ marginTop: 0 }}>
              <li>
                <strong>Overview / Trace Explorer</strong>：仅统计主 Trace 目录（Agent 运行记录），
                当前 <code>{sources?.agent_traces ?? traces.length}</code> 条。
              </li>
              <li>
                <strong>Cost Analytics</strong>：统计全库（含评测 Trace 目录），
                当前 <code>{sources?.total_traces ?? '—'}</code> 条；其中评测 Trace{' '}
                <code>{sources?.evaluation_traces ?? '—'}</code> 条不在 Trace Explorer 展示，可按 ID 下钻。
              </li>
              <li>
                同一 trace_id <strong>不重复计数</strong>。
              </li>
              <li>
                <strong>REAL_API</strong>：{realApiCount > 0
                  ? `当前有 ${realApiCount} 条已独立验证的真实模型调用记录。`
                  : '暂无已验证的真实模型运行记录。'}
                （缺少 <code>real_api_verified</code> 标记的记录计为 UNKNOWN。）
              </li>
            </ul>
            <div className="row mt">
              <button className="btn" onClick={() => navigate('/traces')}>打开 Trace Explorer</button>
              <button className="btn" onClick={() => navigate('/cost')}>打开 Cost Analytics</button>
            </div>
          </Card>

          <Card title="数据来源分类（全库）" className="mt">
            {!sources ? (
              <EmptyState text="来源分类不可用" />
            ) : (
              <table className="tbl">
                <thead>
                  <tr>
                    <th>来源分类</th>
                    <th>全库数量</th>
                    <th>主 Trace 目录</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {sources.source_classes.map((cls) => (
                    <tr key={cls}>
                      <td><SourceBadge sourceClass={cls} /></td>
                      <td>{sources.by_source[cls] ?? 0}</td>
                      <td>{agentBySource[cls] ?? 0}</td>
                      <td className="small muted">{sources.definitions[cls]}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
              <div className="muted small mt">{sources?.scope_note}</div>
            </Card>
          </div>

          <Card title="失败记录概览（主 Trace 目录）" className="mt">
            {!failed ? (
              <EmptyState text="失败统计不可用" />
            ) : (
              <>
                <div className="row mb">
                  <span className="badge failed">失败 {failed.failed_count}</span>
                  <span className="badge muted">总 Trace {failed.total_traces}</span>
                  <span className="badge warning">
                    含故障注入 {failed.failed_with_fault_injection}
                  </span>
                  {Object.entries(failed.failed_by_source).map(([cls, count]) => (
                    <span key={cls} className="badge muted">
                      {cls} {count}
                    </span>
                  ))}
                </div>
                <div className="muted small mb">
                  失败 Trace 保存在主 Trace 目录，可在 Trace Explorer 按「状态=失败」筛选。
                </div>
                {failed.samples.length === 0 ? (
                  <EmptyState text="暂无失败 Trace 样本" />
                ) : (
                  <table className="tbl">
                    <thead>
                      <tr>
                        <th>失败 Trace</th>
                        <th>来源</th>
                        <th>步骤</th>
                        <th>故障类型</th>
                        <th>时间</th>
                      </tr>
                    </thead>
                    <tbody>
                      {failed.samples.map((s) => (
                        <tr
                          key={s.trace_id}
                          className="clickable"
                          onClick={() => navigate('/traces/' + s.trace_id)}
                        >
                          <td className="mono small">{s.trace_id}</td>
                          <td><SourceBadge sourceClass={s.source_class} /></td>
                          <td>{s.step_count}</td>
                          <td className="small">
                            {s.fault_types.length ? s.fault_types.join(', ') : '—'}
                          </td>
                          <td className="small muted">
                            {new Date(s.started_at).toLocaleString()}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </>
            )}
          </Card>

          <Card title="最近 Trace" className="mt">
            {traces.length === 0 ? (
              <EmptyState text="暂无 Trace 数据" />
            ) : (
              <table className="tbl">
                <thead>
                  <tr>
                    <th>Trace ID</th>
                    <th>状态</th>
                    <th>来源</th>
                    <th>步骤数</th>
                    <th>故障</th>
                    <th>时间</th>
                  </tr>
                </thead>
                <tbody>
                  {traces.slice(0, 8).map((t) => (
                    <tr key={t.trace_id} className="clickable" onClick={() => navigate('/traces/' + t.trace_id)}>
                      <td className="mono">{t.trace_id}</td>
                      <td><StatusBadge status={t.status} /></td>
                      <td><SourceBadge sourceClass={t.source_class} /></td>
                      <td>{t.step_count}</td>
                      <td>{t.fault_types.length ? t.fault_types.join(', ') : '—'}</td>
                      <td className="small muted">{new Date(t.started_at).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </>
      )}
    </div>
  )
}

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { RefreshCw } from 'lucide-react'
import { apiErrorMessage, fetchTraces } from '../api'
import type { TracesResponse } from '../types'
import { Card, EmptyState, ErrorBox, Spinner, StatusBadge } from '../components/ui'

const PAGE_SIZE = 50

const SOURCE_OPTIONS = [
  { value: '', label: '全部来源' },
  { value: 'REAL_API', label: 'REAL_API（真实 API）' },
  { value: 'MOCK', label: 'MOCK（本地 Mock 执行）' },
  { value: 'SIMULATED', label: 'SIMULATED（合成/模拟）' },
  { value: 'REPLAY', label: 'REPLAY（重放/反事实派生）' },
  { value: 'UNKNOWN', label: 'UNKNOWN（来源未确认）' },
]

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'succeeded', label: '成功' },
  { value: 'failed', label: '失败' },
]

function SourceBadge({ sourceClass }: { sourceClass: string }) {
  if (sourceClass === 'REAL_API') return <span className="badge success">REAL_API</span>
  if (sourceClass === 'MOCK') return <span className="badge warning">MOCK</span>
  if (sourceClass === 'REPLAY') return <span className="badge blue">REPLAY</span>
  if (sourceClass === 'SIMULATED') return <span className="badge muted">SIMULATED</span>
  return <span className="badge failed">UNKNOWN</span>
}

export default function TraceExplorer() {
  const navigate = useNavigate()
  const [data, setData] = useState<TracesResponse | null>(null)
  const [sourceClass, setSourceClass] = useState('')
  const [status, setStatus] = useState('')
  const [faultOnly, setFaultOnly] = useState(false)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await fetchTraces({
        sourceClass: sourceClass || undefined,
        status: status || undefined,
        faultOnly,
      })
      setData(r)
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setLoading(false)
    }
  }, [sourceClass, status, faultOnly])

  useEffect(() => {
    setPage(1)
    void load()
  }, [load])

  const traces = data?.traces ?? []
  const totalPages = Math.max(1, Math.ceil(traces.length / PAGE_SIZE))
  const currentPage = Math.min(page, totalPages)
  const pageRows = useMemo(
    () => traces.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE),
    [traces, currentPage],
  )

  const filtered = Boolean(sourceClass || status || faultOnly)

  return (
    <div className="stack">
      <div className="row">
        <h2 style={{ margin: 0 }}>Trace Explorer</h2>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => void load()}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>

      <Card title="筛选条件">
        <div className="row" style={{ gap: 12, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <label className="small muted" htmlFor="te-source">来源分类</label>
          <select id="te-source" value={sourceClass} onChange={(e) => setSourceClass(e.target.value)}>
            {SOURCE_OPTIONS.map((item) => (
              <option key={item.value || 'ALL'} value={item.value}>{item.label}</option>
            ))}
          </select>
          <label className="small muted" htmlFor="te-status">状态</label>
          <select id="te-status" value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUS_OPTIONS.map((item) => (
              <option key={item.value || 'ALL'} value={item.value}>{item.label}</option>
            ))}
          </select>
          <label className="row small" style={{ gap: 6 }}>
            <input
              type="checkbox"
              checked={faultOnly}
              onChange={(e) => setFaultOnly(e.target.checked)}
            />
            仅看故障注入 Trace
          </label>
        </div>
        {data && (
          <div className="row mt" style={{ gap: 8, flexWrap: 'wrap' }}>
            <span className="badge blue">
              当前 {traces.length} 条{filtered ? '（已筛选）' : ''}
            </span>
            <span className="badge muted">主 Trace 目录全量 {data.total_count}</span>
            {data.by_status.failed !== undefined && (
              <span className="badge failed">失败 {data.by_status.failed}</span>
            )}
            {data.by_source.REAL_API === 0 && (
              <span className="badge warning">REAL_API 0 条：暂无已验证的真实模型运行记录</span>
            )}
          </div>
        )}
        <div className="muted small mt">{data?.scope_note}</div>
      </Card>

      {error && <ErrorBox message={error} />}
      {loading && <Spinner />}

      {!loading && !error && data && (
        <Card title={'Trace 列表（' + traces.length + '）'}>
          {traces.length === 0 ? (
            <EmptyState
              text={
                filtered
                  ? '当前筛选条件下没有 Trace；可清空筛选或切换来源/状态'
                  : '暂无 Trace 数据'
              }
            />
          ) : (
            <>
              <div className="tbl-wrap">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>Trace ID</th>
                    <th>任务</th>
                    <th>状态</th>
                    <th>来源</th>
                    <th>步骤</th>
                    <th>故障</th>
                    <th>开始时间</th>
                    <th>跨页面分析</th>
                  </tr>
                </thead>
                <tbody>
                  {pageRows.map((t) => (
                    <tr key={t.trace_id} className="clickable" onClick={() => navigate('/traces/' + t.trace_id)}>
                      <td className="mono">{t.trace_id}</td>
                      <td className="mono small">{t.task_id}</td>
                      <td><StatusBadge status={t.status} /></td>
                      <td>
                        <span title={t.source_reasons.join('；')}>
                          <SourceBadge sourceClass={t.source_class} />
                        </span>
                      </td>
                      <td>{t.step_count}</td>
                      <td>{t.fault_types.length ? t.fault_types.join(', ') : '—'}</td>
                      <td className="small muted">{new Date(t.started_at).toLocaleString()}</td>
                      <td>
                        <div
                          className="row"
                          style={{ gap: 4, flexWrap: 'wrap' }}
                          onClick={(e) => e.stopPropagation()}
                        >
                          <button
                            className="btn"
                            title="在 DAG Analysis 中打开该 Trace"
                            onClick={() => navigate('/dag?trace_id=' + encodeURIComponent(t.trace_id))}
                          >
                            DAG
                          </button>
                          <button
                            className="btn"
                            title="在 Fault Attribution 中打开该 Trace"
                            onClick={() => navigate('/attribution?trace_id=' + encodeURIComponent(t.trace_id))}
                          >
                            归因
                          </button>
                          <button
                            className="btn"
                            title="在 Counterfactual Lab 中打开该 Trace"
                            onClick={() => navigate('/counterfactual?trace_id=' + encodeURIComponent(t.trace_id))}
                          >
                            反事实
                          </button>
                          <button
                            className="btn"
                            title="在 Repair Studio 中打开该 Trace"
                            onClick={() => navigate('/repair?trace_id=' + encodeURIComponent(t.trace_id))}
                          >
                            修复
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
              <div className="row mt" style={{ justifyContent: 'space-between' }}>
                <span className="muted small">
                  第 {currentPage} / {totalPages} 页 · 每页 {PAGE_SIZE} 条
                </span>
                <div className="row" style={{ gap: 8 }}>
                  <button
                    className="btn"
                    disabled={currentPage <= 1}
                    onClick={() => setPage(currentPage - 1)}
                  >
                    上一页
                  </button>
                  <button
                    className="btn"
                    disabled={currentPage >= totalPages}
                    onClick={() => setPage(currentPage + 1)}
                  >
                    下一页
                  </button>
                </div>
              </div>
            </>
          )}
        </Card>
      )}
    </div>
  )
}

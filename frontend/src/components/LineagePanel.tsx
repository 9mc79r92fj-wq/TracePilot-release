import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChevronDown, ChevronRight, GitBranch, RefreshCw, X } from 'lucide-react'
import { api, apiErrorMessage } from '../api'
import { ErrorBox, Spinner } from './ui'

export type LineageKind = 'trace' | 'evaluation_case' | 'experiment' | 'repair'

export interface LineageNode {
  node_id: string
  kind: string
  label: string
  status: 'AVAILABLE' | 'NOT_AVAILABLE'
  origin: 'REAL_API' | 'MOCK' | 'SIMULATED' | 'UNKNOWN'
  storage: 'RECORDED' | 'MEMORY' | 'NOT_AVAILABLE'
  detail: Record<string, unknown>
  note: string | null
  source_api: string | null
}

export interface LineageReport {
  lineage_engine_version: string
  root_kind: string
  root_id: string
  nodes: LineageNode[]
  edges: { from: string; to: string; relation: string }[]
  summary: {
    node_count: number
    available_count: number
    not_available_count: number
    origin_counts: Record<string, number>
    has_real_api_evidence: boolean
    has_missing_evidence: boolean
  }
  origin_vocabulary: string[]
  storage_vocabulary: string[]
  notes: string[]
}

const ORIGIN_BADGE: Record<string, string> = {
  REAL_API: 'badge success',
  MOCK: 'badge warning',
  SIMULATED: 'badge partial',
  UNKNOWN: 'badge muted',
}

const ORIGIN_LABEL: Record<string, string> = {
  REAL_API: 'REAL API',
  MOCK: 'MOCK',
  SIMULATED: 'SIMULATED',
  UNKNOWN: 'UNKNOWN',
}

const STORAGE_LABEL: Record<string, string> = {
  RECORDED: '已保存',
  MEMORY: '内存登记',
  NOT_AVAILABLE: '不可用',
}

const KIND_LABEL: Record<string, string> = {
  trace: 'Trace',
  agent_run: 'Agent Run',
  step: 'Step',
  tool_call: 'Tool Call',
  tool_result: 'Tool Result',
  evaluation_run: '评测运行',
  evaluation_case: '评测案例',
  evaluation_report: '评测报告',
  experiment: '反事实实验',
  repair_report: '修复报告',
  repair_candidate: '修复候选',
  consistency_warning: '一致性告警',
}

function lineagePath(kind: LineageKind, ids: Record<string, string>): string {
  if (kind === 'trace') return `/api/lineage/trace/${encodeURIComponent(ids.traceId)}`
  if (kind === 'evaluation_case') {
    return `/api/lineage/evaluation/${encodeURIComponent(ids.runId)}/cases/${encodeURIComponent(ids.caseId)}`
  }
  if (kind === 'experiment') return `/api/lineage/experiment/${encodeURIComponent(ids.experimentId)}`
  return `/api/lineage/repair/${encodeURIComponent(ids.reportId)}`
}

/** 分层证据链面板（可展开节点，显示缺失证据与来源标记）。 */
export function LineagePanel({
  kind,
  ids,
  title = '证据血缘（Evidence Lineage）',
  autoLoad = true,
}: {
  kind: LineageKind
  ids: Record<string, string>
  title?: string
  autoLoad?: boolean
}) {
  const navigate = useNavigate()
  const [report, setReport] = useState<LineageReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const r = await api.get<LineageReport>(lineagePath(kind, ids))
      setReport(r.data)
      // 默认展开根节点
      const root = r.data.nodes[0]
      if (root) setExpanded({ [root.node_id]: true })
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setLoading(false)
    }
  }, [kind, ids])

  useEffect(() => {
    if (autoLoad) void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoLoad, kind, JSON.stringify(ids)])

  // 分层：按 kind 顺序渲染，但保持 edges 可读
  const layers = report
    ? [
        { name: '结果来源', kinds: ['evaluation_run', 'experiment', 'repair_report'] },
        { name: '评测案例 / 候选', kinds: ['evaluation_case', 'repair_candidate'] },
        { name: 'Trace', kinds: ['trace'] },
        { name: 'Agent Run', kinds: ['agent_run'] },
        { name: '执行步骤', kinds: ['step'] },
        { name: '工具调用与结果', kinds: ['tool_call', 'tool_result'] },
        { name: '一致性告警', kinds: ['consistency_warning', 'evaluation_report'] },
      ]
        .map((l) => ({ ...l, nodes: report.nodes.filter((n) => l.kinds.includes(n.kind)) }))
        .filter((l) => l.nodes.length > 0)
    : []

  return (
    <div className="stack">
      <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
        <div className="row" style={{ gap: 8 }}>
          <GitBranch size={15} />
          <strong className="small">{title}</strong>
        </div>
        <div className="row" style={{ gap: 6 }}>
          {report && (
            <span className="muted small">
              节点 {report.summary.node_count} · 可用 {report.summary.available_count} · 缺失{' '}
              {report.summary.not_available_count}
            </span>
          )}
          <button className="btn" style={{ padding: '2px 8px' }} onClick={() => void load()}>
            <RefreshCw size={13} /> 刷新
          </button>
        </div>
      </div>

      {error && <ErrorBox message={error} />}
      {loading && <Spinner />}

      {report && (
        <>
          <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
            {report.origin_vocabulary.map((o) => (
              <span key={o} className={ORIGIN_BADGE[o] ?? 'badge muted'}>
                {ORIGIN_LABEL[o] ?? o} × {report.summary.origin_counts[o] ?? 0}
              </span>
            ))}
            {report.summary.has_missing_evidence && (
              <span className="badge failed">存在缺失证据</span>
            )}
          </div>

          {layers.map((layer) => (
            <div key={layer.name} className="lin-layer">
              <div className="lin-layer-head">{layer.name}</div>
              <div className="stack">
                {layer.nodes.map((n) => {
                  const open = expanded[n.node_id] ?? false
                  const unavailable = n.status === 'NOT_AVAILABLE'
                  return (
                    <div
                      key={n.node_id}
                      className={'lin-node' + (unavailable ? ' missing' : '')}
                    >
                      <button
                        className="lin-node-head"
                        onClick={() => setExpanded((p) => ({ ...p, [n.node_id]: !open }))}
                        aria-expanded={open}
                      >
                        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                        <span className="lin-kind">{KIND_LABEL[n.kind] ?? n.kind}</span>
                        <span className="mono small lin-id">{n.node_id}</span>
                        {unavailable ? (
                          <span className="badge failed">不可用</span>
                        ) : (
                          <span className={ORIGIN_BADGE[n.origin] ?? 'badge muted'}>
                            {ORIGIN_LABEL[n.origin] ?? n.origin}
                          </span>
                        )}
                        <span className="badge muted">{STORAGE_LABEL[n.storage] ?? n.storage}</span>
                      </button>

                      {open && (
                        <div className="lin-node-body">
                          {unavailable && n.note && (
                            <div className="errorbox" style={{ marginBottom: 6 }}>
                              {n.note}
                            </div>
                          )}
                          {n.note && !unavailable && <div className="muted small">{n.note}</div>}
                          <pre className="json">{JSON.stringify(n.detail, null, 2)}</pre>
                          <div className="row" style={{ gap: 6, marginTop: 6 }}>
                            {n.kind === 'trace' && (
                              <>
                                <button
                                  className="btn"
                                  style={{ padding: '2px 8px' }}
                                  onClick={() => navigate('/traces/' + n.node_id)}
                                >
                                  查看 Trace
                                </button>
                                <button
                                  className="btn"
                                  style={{ padding: '2px 8px' }}
                                  onClick={() => navigate('/dag/' + n.node_id)}
                                >
                                  查看 DAG
                                </button>
                              </>
                            )}
                            {n.source_api && (
                              <span className="muted small mono">来源接口 {n.source_api}</span>
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          ))}

          <details>
            <summary className="small muted" style={{ cursor: 'pointer' }}>
              查看关系边（{report.edges.length}）
            </summary>
            <table className="tbl" style={{ marginTop: 6 }}>
              <thead><tr><th>起点</th><th>关系</th><th>终点</th></tr></thead>
              <tbody>
                {report.edges.map((e, i) => (
                  <tr key={i}>
                    <td className="mono small">{e.from}</td>
                    <td className="small">{e.relation}</td>
                    <td className="mono small">{e.to}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>

          <div className="cf-note small">
            {report.notes.map((n) => <div key={n}>· {n}</div>)}
          </div>
        </>
      )}
    </div>
  )
}

/** 抽屉形态的证据血缘（用于评测案例等场景）。 */
export function LineageDrawer({
  kind,
  ids,
  onClose,
}: {
  kind: LineageKind
  ids: Record<string, string>
  onClose: () => void
}) {
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <span className="small">证据血缘</span>
          <button className="btn" onClick={onClose} aria-label="关闭"><X size={15} /></button>
        </div>
        <div className="drawer-body">
          <LineagePanel kind={kind} ids={ids} />
        </div>
      </div>
    </div>
  )
}

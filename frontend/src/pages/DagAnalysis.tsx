import { useCallback, useEffect, useMemo, useState } from 'react'
import type { CSSProperties } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ReactFlow, Background, Controls, MiniMap } from '@xyflow/react'
import type { Edge, Node } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Pause, Play, RefreshCw, SkipBack, X } from 'lucide-react'
import { apiErrorMessage, fetchTrace, fetchTraceDag, fetchTraces } from '../api'
import type { Dag, TraceStep, TraceSummary } from '../types'
import { Card, EmptyState, ErrorBox, Spinner, StatusBadge } from '../components/ui'
import NodeDetailPanel from '../components/NodeDetailPanel'
import TraceFlowNode from '../components/TraceFlowNode'
import { useTraceUrlParam } from '../hooks/useTraceUrlParam'

const nodeTypes = { traceNode: TraceFlowNode }

const STATUS_CLASS: Record<string, string> = {
  succeeded: 'succeeded',
  failed: 'failed',
  running: 'running',
}

function layout(dag: Dag): Record<string, { x: number; y: number }> {
  const byId = new Map(dag.nodes.map((n) => [n.step_id, n]))
  const rank: Record<string, number> = {}
  for (const id of dag.topological_order) {
    const node = byId.get(id)
    if (!node) continue
    const parents = node.parent_step_ids
    rank[id] = parents.length ? Math.max(...parents.map((p) => rank[p] ?? 0)) + 1 : 0
  }
  const groups = new Map<number, string[]>()
  for (const id of dag.topological_order) {
    const r = rank[id] ?? 0
    if (!groups.has(r)) groups.set(r, [])
    groups.get(r)!.push(id)
  }
  const pos: Record<string, { x: number; y: number }> = {}
  const X = 280
  const Y = 120
  for (const r of [...groups.keys()].sort((a, b) => a - b)) {
    groups.get(r)!.forEach((id, i) => {
      pos[id] = { x: r * X, y: i * Y }
    })
  }
  return pos
}

function downstreamOf(root: string, dag: Dag): Set<string> {
  const children = new Map<string, string[]>()
  for (const e of dag.edges) {
    const arr = children.get(e.parent_id) ?? []
    arr.push(e.child_id)
    children.set(e.parent_id, arr)
  }
  const seen = new Set<string>()
  const stack = [...(children.get(root) ?? [])]
  while (stack.length) {
    const cur = stack.pop()!
    if (seen.has(cur)) continue
    seen.add(cur)
    stack.push(...(children.get(cur) ?? []))
  }
  return seen
}

export default function DagAnalysis() {
  const { traceId: routeTraceId } = useParams()
  const navigate = useNavigate()
  const { traceId: queryTraceId, stepId: queryStepId } = useTraceUrlParam()

  const [traces, setTraces] = useState<TraceSummary[]>([])
  const [loadedId, setLoadedId] = useState<string | null>(null)
  const [dag, setDag] = useState<Dag | null>(null)
  const [steps, setSteps] = useState<Map<string, TraceStep>>(new Map())
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [focusId, setFocusId] = useState<string | null>(null)
  const [selectedNode, setSelectedNode] = useState<string | null>(null)

  // 路由参数优先（/dag/:traceId 兼容旧链接），其次 URL 查询参数（跨页面传递）
  const urlTraceId = routeTraceId || queryTraceId
  const effectiveId = urlTraceId || traces[0]?.trace_id || null
  const urlTraceMissing =
    Boolean(urlTraceId) &&
    traces.length > 0 &&
    !traces.some((t) => t.trace_id === urlTraceId)

  const loadTraces = useCallback(async () => {
    try {
      const t = await fetchTraces()
      setTraces(t.traces)
    } catch (e) {
      setError(apiErrorMessage(e))
    }
  }, [])

  const load = useCallback(async (id: string) => {
    setLoading(true)
    setError(null)
    try {
      const [d, tr] = await Promise.all([fetchTraceDag(id), fetchTrace(id)])
      setDag(d.dag)
      setSteps(new Map(tr.trace.steps.map((s) => [s.step_id, s])))
      setLoadedId(id)
      setFocusId(null)
      setSelectedNode(null)
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadTraces()
  }, [loadTraces])

  useEffect(() => {
    if (effectiveId && effectiveId !== loadedId) void load(effectiveId)
  }, [effectiveId, loadedId, load])

  const faultIds = useMemo(() => {
    const s = new Set<string>()
    for (const [id, st] of steps) {
      if (st.metadata && st.metadata.fault_injection) s.add(id)
    }
    return s
  }, [steps])

  /**
   * 复杂执行标注：每个 step 事件一个节点（不合并重试/重复执行）。
   * - retry：优先读取 metadata.attempt / retry_index；否则推断「同工具同参数且上次失败」的再次执行；
   * - timeout：error_type 含 timeout（如 ToolTimeoutError / tool_timeout）；
   * - 循环执行若在 Trace 中表现为多次事件，会得到多个不同 step_id 的节点，不会被合并。
   */
  const eventInfo = useMemo(() => {
    const retry = new Map<string, { attempt: number; repeated: boolean }>()
    const timeout = new Set<string>()
    const toolNames = new Map<string, string>()
    const lastStatus = new Map<string, string>()
    const counters = new Map<string, number>()

    for (const id of dag?.topological_order ?? []) {
      const st = steps.get(id)
      if (!st) continue
      const meta = (st.metadata ?? {}) as Record<string, unknown>
      if (st.tool_name) toolNames.set(id, st.tool_name)
      const errType = String(st.error?.error_type ?? meta.error_type ?? '')
      if (/timeout/i.test(errType)) timeout.add(id)

      const declared = Number(meta.attempt ?? meta.retry_index)
      if (Number.isFinite(declared) && declared >= 1) {
        retry.set(id, { attempt: declared, repeated: declared > 1 })
        continue
      }
      if (st.step_type !== 'tool_call' || !st.tool_name) continue
      const key = st.tool_name + '|' + JSON.stringify(st.tool_arguments ?? {})
      const count = (counters.get(key) ?? 0) + 1
      counters.set(key, count)
      if (count > 1 && lastStatus.get(key) === 'failed') {
        retry.set(id, { attempt: count, repeated: true })
      }
      lastStatus.set(key, st.status)
    }
    return { retry, timeout, toolNames }
  }, [dag, steps])

  // URL 携带 step_id 时自动选中对应节点（例如从 Fault Attribution/DAG 面板跳转）
  useEffect(() => {
    if (!dag || !queryStepId) return
    if (!steps.has(queryStepId)) return
    setSelectedNode(queryStepId)
    setFocusId(faultIds.has(queryStepId) ? queryStepId : null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dag, queryStepId, steps, faultIds])


  const selectedStep = selectedNode ? steps.get(selectedNode) : undefined

  const childrenMap = useMemo(() => {
    if (!dag) return new Map<string, string[]>()
    return new Map(dag.nodes.map((n) => [n.step_id, n.child_step_ids]))
  }, [dag])

  const selectedDownstream = useMemo(() => {
    if (!dag || !selectedNode) return []
    return [...downstreamOf(selectedNode, dag)]
  }, [dag, selectedNode])

  // ---- Trace Time Travel ----
  const [timelineOpen, setTimelineOpen] = useState(true)
  const [currentEventIndex, setCurrentEventIndex] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState<1 | 2 | 4>(1)

  const timeline = useMemo(() => {
    if (!dag) return { order: [] as string[], missing: 0 }
    const topoIndex = new Map(dag.topological_order.map((id, i) => [id, i]))
    const withTs: { id: string; ts: number }[] = []
    let missing = 0
    for (const [id, st] of steps) {
      const ts = st.timestamp ? new Date(st.timestamp).getTime() : NaN
      if (Number.isNaN(ts)) missing++
      else withTs.push({ id, ts })
    }
    withTs.sort(
      (a, b) => a.ts - b.ts || (topoIndex.get(a.id) ?? 0) - (topoIndex.get(b.id) ?? 0),
    )
    return { order: withTs.map((e) => e.id), missing }
  }, [dag, steps])

  useEffect(() => {
    setCurrentEventIndex(timeline.order.length - 1)
    setPlaying(false)
  }, [timeline.order.length])

  const currentId = timeline.order[currentEventIndex]
  const currentStep = currentId ? steps.get(currentId) : undefined

  const goToIndex = useCallback(
    (i: number) => {
      if (timeline.order.length === 0) return
      const clamped = Math.max(0, Math.min(timeline.order.length - 1, i))
      setCurrentEventIndex(clamped)
      const id = timeline.order[clamped]
      if (id) {
        setSelectedNode(id)
        setFocusId(null)
      }
    },
    [timeline.order],
  )

  // 播放期间：把当前事件同步为选中节点，联动详情面板与 Evidence Card
  useEffect(() => {
    if (playing) {
      const id = timeline.order[currentEventIndex]
      if (id) {
        setSelectedNode(id)
        setFocusId(null)
      }
    }
  }, [currentEventIndex, playing, timeline.order])

  useEffect(() => {
    if (!playing || timeline.order.length === 0) return
    const interval = 700 / speed
    const t = setInterval(() => {
      setCurrentEventIndex((prev) => {
        if (prev >= timeline.order.length - 1) {
          setPlaying(false)
          return prev
        }
        return prev + 1
      })
    }, interval)
    return () => clearInterval(t)
  }, [playing, speed, timeline.order.length])

  function togglePlay() {
    if (playing) {
      setPlaying(false)
      return
    }
    if (currentEventIndex >= timeline.order.length - 1) goToIndex(0)
    setPlaying(true)
  }
  function nextEvent() {
    goToIndex(currentEventIndex + 1)
  }
  function prevEvent() {
    goToIndex(currentEventIndex - 1)
  }
  function goToFault() {
    const idx = timeline.order.findIndex((id) => faultIds.has(id))
    if (idx >= 0) goToIndex(idx)
  }
  const speeds: (1 | 2 | 4)[] = [1, 2, 4]

  const { rfNodes, rfEdges } = useMemo(() => {
    if (!dag) return { rfNodes: [] as Node[], rfEdges: [] as Edge[] }
    const pos = layout(dag)
    const highlight = focusId ? new Set([focusId, ...downstreamOf(focusId, dag)]) : null
    const happened = new Set(timeline.order.slice(0, currentEventIndex + 1))
    const hasTimeline = timeline.order.length > 0

    const nodes: Node[] = dag.nodes.map((n) => {
      const dim = highlight ? !highlight.has(n.step_id) : false
      const focused = focusId === n.step_id
      const isCurrent = n.step_id === currentId
      const notYet = hasTimeline && !happened.has(n.step_id)
      const retryInfo = eventInfo.retry.get(n.step_id)
      const step = steps.get(n.step_id)
      const style: CSSProperties = {}
      if (dim) style.opacity = 0.15
      else if (notYet) style.opacity = 0.2
      if (focused) style.boxShadow = '0 0 0 3px rgba(37,99,235,0.45)'
      if (isCurrent) style.boxShadow = '0 0 0 3px rgba(8,145,178,0.75)'
      return {
        id: n.step_id,
        type: 'traceNode',
        position: pos[n.step_id] ?? { x: 0, y: 0 },
        style,
        data: {
          shortId: n.step_id.split('-').slice(-2).join('-'),
          stepType: n.step_type,
          statusClass: STATUS_CLASS[n.status] ?? 'other',
          isFault: faultIds.has(n.step_id),
          isCurrent,
          toolName: eventInfo.toolNames.get(n.step_id) ?? (step?.tool_name ?? undefined),
          retryAttempt: retryInfo?.attempt ?? undefined,
          timeout: eventInfo.timeout.has(n.step_id),
          errorType: step?.error?.error_type ?? undefined,
        } as Record<string, unknown>,
      }
    })

    const edges: Edge[] = dag.edges.map((e, i) => {
      const on = highlight ? highlight.has(e.parent_id) && highlight.has(e.child_id) : false
      const active = hasTimeline && happened.has(e.parent_id) && happened.has(e.child_id)
      const style: CSSProperties = {}
      if (highlight) {
        if (on) {
          style.stroke = '#2563eb'
          style.strokeWidth = 2
        } else style.opacity = 0.08
      } else if (hasTimeline) {
        style.stroke = active ? '#94a3b8' : '#e5e9f0'
        style.opacity = active ? 1 : 0.5
      }
      return {
        id: 'e' + i,
        source: e.parent_id,
        target: e.child_id,
        style: Object.keys(style).length ? style : undefined,
      }
    })
    return { rfNodes: nodes, rfEdges: edges }
  }, [dag, focusId, faultIds, eventInfo, steps, timeline.order, currentEventIndex, currentId])

  function cycleSpeed() {
    setSpeed((s) => speeds[(speeds.indexOf(s) + 1) % speeds.length])
  }

  return (
    <div className="stack">
      <div className="row">
        <h2 style={{ margin: 0 }}>DAG Analysis</h2>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => effectiveId && void load(effectiveId)}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>

      <div className="dag-toolbar">
        <select
          value={effectiveId ?? ''}
          onChange={(e) => navigate('/dag?trace_id=' + encodeURIComponent(e.target.value))}
          aria-label="选择 Trace"
        >
          {traces.length === 0 && <option value="">Trace 列表为空（加载失败或目录无数据）</option>}
          {traces.map((t) => (
            <option key={t.trace_id} value={t.trace_id}>
              {t.trace_id}
            </option>
          ))}
        </select>
        {urlTraceId && (
          <span className="badge blue mono small" title="当前 URL 指定的 Trace">
            URL trace_id: {urlTraceId}
          </span>
        )}
        <div className="row small" style={{ gap: 8, flexWrap: 'wrap' }}>
          <span className="badge failed">失败节点=红色</span>
          <span className="badge warning">重试 #n</span>
          <span className="badge failed">超时</span>
          <span className="badge failed">fault=注入故障点</span>
          <span className="muted">点击节点查看证据；依赖边不等同因果</span>
        </div>
        {focusId && (
          <div className="focus-note">
            <span>聚焦 {focusId} 及下游依赖（不代表故障传播）</span>
            <button className="btn" onClick={() => setFocusId(null)}>
              <X size={14} /> 退出聚焦
            </button>
          </div>
        )}
      </div>

      {urlTraceMissing && (
        <ErrorBox
          message={
            `Trace 不存在或不在当前 Trace 目录：${urlTraceId}。` +
            '请从 Trace Explorer 重新选择。'
          }
        />
      )}
      {error && <ErrorBox message={error} />}
      {loading && <Spinner />}

      {!loading && !error && dag && (
        <div className="dag-layout">
          <div className="stack">
            <Card>
              <div className="dag-wrap">
                <ReactFlow
                  nodes={rfNodes}
                  edges={rfEdges}
                  nodeTypes={nodeTypes}
                  fitView
                  minZoom={0.2}
                  maxZoom={2}
                  nodesDraggable={false}
                  onNodeClick={(_e, node) => {
                    setSelectedNode(node.id)
                    if (faultIds.has(node.id)) setFocusId(node.id)
                    const idx = timeline.order.indexOf(node.id)
                    if (idx >= 0) setCurrentEventIndex(idx)
                  }}
                >
                  <Background />
                  <Controls />
                  <MiniMap pannable zoomable />
                </ReactFlow>
              </div>
            </Card>

            {currentStep && (
              <div className="current-float">
                <span className="badge cyan">当前事件 {currentEventIndex + 1}/{timeline.order.length}</span>
                <span className="mono small">{currentStep.step_id}</span>
                <span className="small muted">{currentStep.step_type}</span>
                <StatusBadge status={currentStep.status} />
                {faultIds.has(currentStep.step_id) && <span className="badge failed">故障点</span>}
              </div>
            )}

            <Card
              title={
                'Trace 回放 · 进度 ' +
                (timeline.order.length ? Math.round(((currentEventIndex + 1) / timeline.order.length) * 100) : 0) +
                '%'
              }
              action={
                <div className="row" style={{ gap: 6 }}>
                  <button className="btn" onClick={() => setTimelineOpen((v) => !v)}>
                    {timelineOpen ? '收起' : '展开'}时间轴
                  </button>
                </div>
              }
            >
              {timelineOpen && (
                <div className="stack">
                  <div className="row">
                    <button className="btn primary" onClick={togglePlay} aria-label={playing ? '暂停' : '播放'}>
                      {playing ? <Pause size={15} /> : <Play size={15} />}
                      {playing ? '暂停' : '播放'}
                    </button>
                    <button className="btn" onClick={goToIndex.bind(null, 0)} aria-label="回到起点">
                      <SkipBack size={15} /> 起点
                    </button>
                    <button className="btn" onClick={prevEvent}>
                      上一事件
                    </button>
                    <button className="btn" onClick={nextEvent}>
                      下一事件
                    </button>
                    <button className="btn" onClick={goToFault}>
                      跳转故障
                    </button>
                    <button className="btn" onClick={cycleSpeed}>
                      {speed}× 速度
                    </button>
                  </div>
                  <input
                    type="range"
                    min={0}
                    max={Math.max(0, timeline.order.length - 1)}
                    value={currentEventIndex}
                    onChange={(e) => goToIndex(Number(e.target.value))}
                    aria-label="时间轴进度"
                    style={{ width: '100%' }}
                  />
                  <div className="row" style={{ overflowX: 'auto', gap: 6, paddingBottom: 4 }}>
                    {timeline.order.map((id, i) => (
                      <button
                        key={id}
                        className={
                          'tl-chip' +
                          (i === currentEventIndex ? ' current' : '') +
                          (i > currentEventIndex ? ' future' : '') +
                          (faultIds.has(id) ? ' fault' : '')
                        }
                        onClick={() => goToIndex(i)}
                        title={id}
                      >
                        {i + 1}. {id.split('-').slice(-1)[0]}
                      </button>
                    ))}
                  </div>
                  {timeline.missing > 0 && (
                    <p className="muted small" style={{ margin: 0 }}>
                      {timeline.missing} 个步骤缺少时间戳，未纳入时间轴。
                    </p>
                  )}
                </div>
              )}
            </Card>
          </div>

          <Card
            className="dag-side"
            title="节点详情"
            action={
              selectedStep ? (
                <button className="btn" onClick={() => setSelectedNode(null)} aria-label="关闭面板">
                  <X size={15} />
                </button>
              ) : undefined
            }
          >
            {!selectedStep ? (
              <EmptyState text="点击图中节点或时间轴查看详情" />
            ) : (
              <NodeDetailPanel
                step={selectedStep}
                childStepIds={childrenMap.get(selectedNode ?? '') ?? []}
                isFault={faultIds.has(selectedNode ?? '')}
                downstream={selectedDownstream}
                traceId={loadedId ?? effectiveId ?? undefined}
              />
            )}
          </Card>
        </div>
      )}

      {!loading && !error && !dag && <EmptyState text="暂无 DAG 数据" />}
    </div>
  )
}

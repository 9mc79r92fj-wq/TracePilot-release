import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { RefreshCw } from 'lucide-react'
import { api, apiErrorMessage } from '../api'
import { Card, EmptyState, ErrorBox, Spinner } from '../components/ui'

/**
 * Architecture · 系统架构与运行概览。
 *
 * 业务价值：展示分层架构、执行链路与真实运行数据（Trace / Agent / 评测 / 流水线 / 数据来源）。
 * 所有统计数字均来自本机后端 API 的实时读取，不做硬编码，也不生成演示记录。
 */

const LAYERS = [
  {
    name: 'Agent 执行层',
    desc: 'Agent 执行：规划、模型调用、工具调用、RAG 与记忆、反思、HITL、安全钩子，并提供故障注入接口。',
    tone: 'blue',
  },
  {
    name: '轨迹与因果建模层',
    desc: 'Trace 采集（Trace Schema）→ DAG 构建 → 依赖分析 → 干预接口。',
    tone: 'blue',
  },
  {
    name: '故障归因实验层',
    desc: '根因搜索 / 反事实干预执行 / 最小修复验证 / 证据分级 / 根因准确率指标。',
    tone: 'blue',
  },
  {
    name: '产品交付与监控层',
    desc: 'FastAPI 服务层 + React 控制台 + SQLite/文件存储 + 评测管理 / CI-CD / 部署。',
    tone: 'blue',
  },
]

const FLOW = [
  '创建任务',
  'Agent 执行',
  'Trace 采集',
  'DAG 依赖分析',
  '故障定位',
  '反事实干预',
  '最小修复验证',
  '评测报告',
]

type SourceClass = 'REAL_API' | 'MOCK' | 'SIMULATED' | 'REPLAY' | 'UNKNOWN'

const SOURCE_ORDER: SourceClass[] = ['REAL_API', 'MOCK', 'SIMULATED', 'REPLAY', 'UNKNOWN']

interface GovernanceSources {
  generated_at?: string
  total_traces: number
  agent_traces: number
  evaluation_traces: number
  by_source: Record<string, number>
  status_by_source: Record<string, Record<string, number>>
  unknown_count: number
  stale?: boolean
}

interface AgentRunRow {
  run_id: string
  task: string
  mode: string
  data_source: string | null
  simulated: boolean
  status: string
  created_at: string
  trace_id: string | null
}

interface EvalRunRow {
  run_id: string
  status: string
  dataset_id: string
  judge_mode: string | null
  created_at: string
}

interface PipelineRow {
  pipeline_run_id: string
  status: string
  created_at: string
  dataset_id: string | null
  execution_mode: string | null
}

interface RecentRow {
  key: string
  kind: 'Agent 运行' | '评测运行' | '评测流水线'
  title: string
  objectId: string
  status: string
  source: SourceClass
  created_at: string
  traceId?: string | null
}

function sourceBadgeTone(source: SourceClass): string {
  switch (source) {
    case 'REAL_API':
      return 'success'
    case 'MOCK':
      return 'warning'
    case 'SIMULATED':
      return 'muted'
    case 'REPLAY':
      return 'blue'
    default:
      return 'failed'
  }
}

function formatTime(value?: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN', { hour12: false })
}

function agentSource(run: AgentRunRow): SourceClass {
  if (run.mode === 'real_api' || run.data_source === 'real_api') return 'REAL_API'
  if (run.data_source === 'mock') return 'MOCK'
  if (run.simulated || run.data_source === 'simulated') return 'SIMULATED'
  return 'UNKNOWN'
}

export default function Architecture() {
  const navigate = useNavigate()
  const [sources, setSources] = useState<GovernanceSources | null>(null)
  const [agentRuns, setAgentRuns] = useState<AgentRunRow[]>([])
  const [evalRuns, setEvalRuns] = useState<EvalRunRow[]>([])
  const [pipelines, setPipelines] = useState<PipelineRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    const [sourcesResult, agentResult, evalResult, pipelineResult] = await Promise.allSettled([
      api.get<GovernanceSources>('/api/governance/sources', { timeout: 60000 }),
      api.get<{ count?: number; runs: AgentRunRow[] }>('/api/agent/runs', { timeout: 60000 }),
      api.get<{ count?: number; runs: EvalRunRow[] }>('/api/evaluation/runs', { timeout: 60000 }),
      api.get<{ pipelines: PipelineRow[] }>('/api/pipelines?limit=50', { timeout: 60000 }),
    ])
    if (sourcesResult.status === 'fulfilled') setSources(sourcesResult.value.data)
    else if (agentResult.status === 'rejected' && evalResult.status === 'rejected') {
      setError(apiErrorMessage(sourcesResult.reason))
    }
    if (agentResult.status === 'fulfilled') setAgentRuns(agentResult.value.data.runs ?? [])
    if (evalResult.status === 'fulfilled') setEvalRuns(evalResult.value.data.runs ?? [])
    if (pipelineResult.status === 'fulfilled') setPipelines(pipelineResult.value.data.pipelines ?? [])
    setLoading(false)
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const agentSucceeded = useMemo(
    () => agentRuns.filter((r) => r.status === 'succeeded').length,
    [agentRuns],
  )
  const agentFailed = useMemo(
    () => agentRuns.filter((r) => r.status === 'failed').length,
    [agentRuns],
  )
  const evalSucceeded = useMemo(
    () => evalRuns.filter((r) => r.status === 'succeeded').length,
    [evalRuns],
  )
  const evalFailed = useMemo(
    () => evalRuns.filter((r) => r.status === 'failed').length,
    [evalRuns],
  )

  const recent = useMemo<RecentRow[]>(() => {
    const rows: RecentRow[] = [
      ...agentRuns.map((r) => ({
        key: `agent:${r.run_id}`,
        kind: 'Agent 运行' as const,
        title: r.task || r.run_id,
        objectId: r.run_id,
        status: r.status,
        source: agentSource(r),
        created_at: r.created_at,
        traceId: r.trace_id,
      })),
      ...evalRuns.map((r) => ({
        key: `eval:${r.run_id}`,
        kind: '评测运行' as const,
        title: `${r.dataset_id}${r.judge_mode ? ` · Judge ${r.judge_mode}` : ''}`,
        objectId: r.run_id,
        status: r.status,
        source: 'SIMULATED' as const,
        created_at: r.created_at,
      })),
      ...pipelines.map((p) => ({
        key: `pipe:${p.pipeline_run_id}`,
        kind: '评测流水线' as const,
        title: `${p.dataset_id ?? 'pipeline'} · ${p.execution_mode ?? 'unknown'}`,
        objectId: p.pipeline_run_id,
        status: p.status,
        source: 'SIMULATED' as const,
        created_at: p.created_at,
      })),
    ]
    return rows.sort((a, b) => (a.created_at < b.created_at ? 1 : -1)).slice(0, 6)
  }, [agentRuns, evalRuns, pipelines])

  const latestRun = recent[0]
  const totalTrace = sources?.total_traces ?? 0
  const realApiCount = sources?.by_source?.REAL_API ?? 0

  function openRecent(row: RecentRow) {
    if (row.kind === 'Agent 运行' && row.traceId) {
      navigate(`/traces/${row.traceId}`)
      return
    }
    if (row.kind === '评测运行') {
      navigate('/evaluation')
      return
    }
    navigate('/pipelines')
  }

  return (
    <div className="stack">
      <div className="row">
        <div>
          <h2 style={{ margin: 0 }}>Architecture · 系统架构</h2>
          <p className="muted small" style={{ margin: '4px 0 0' }}>
            分层架构、执行链路与本机运行数据概览。
          </p>
        </div>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => void load()} disabled={loading}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>

      {error && <ErrorBox message={error} />}

      <div className="grid grid-2 arch-grid">
        <Card title="四层架构">
          <div className="stack">
            {LAYERS.map((l) => (
              <div key={l.name} className="row" style={{ alignItems: 'flex-start' }}>
                <span className={'badge ' + l.tone}>{l.name}</span>
                <span className="muted small" style={{ flex: 1 }}>{l.desc}</span>
              </div>
            ))}
          </div>
        </Card>

        <Card title="Agent 执行链路与 Trace 数据流">
          <div className="row">
            {FLOW.map((s, i) => (
              <span key={s} className="row" style={{ gap: 6 }}>
                <span className="badge blue">{s}</span>
                {i < FLOW.length - 1 && <span className="muted">→</span>}
              </span>
            ))}
          </div>
        </Card>

        <Card title="系统集成">
          <p className="muted small" style={{ margin: 0 }}>
            FastAPI 统一提供 Trace / DAG / 归因 / 反事实 / 评测接口；
            控制台按执行、诊断、评测、安全与成本组织；
            数据持久化在本地，并按来源标注。
          </p>
        </Card>
      </div>

      <Card
        title="系统运行概览"
        action={
          <span className="muted small">
            {sources?.stale ? '数据索引刷新中 · ' : ''}
            数据来源：本机运行记录
          </span>
        }
      >
        {loading && !sources ? (
          <Spinner />
        ) : (
          <div className="grid stats">
            <div className="card stat">
              <div className="k">Trace 总数</div>
              <div className="v blue">{totalTrace.toLocaleString('en-US')}</div>
              <div className="muted small">
                持久化记录 · 主目录 {(sources?.agent_traces ?? 0).toLocaleString('en-US')} ·
                评测目录 {(sources?.evaluation_traces ?? 0).toLocaleString('en-US')}
              </div>
            </div>
            <div className="card stat">
              <div className="k">Agent 运行记录</div>
              <div className="v">{agentRuns.length}</div>
              <div className="muted small">
                本次服务会话 · 成功 {agentSucceeded} · 失败 {agentFailed}
              </div>
            </div>
            <div className="card stat">
              <div className="k">评测运行</div>
              <div className="v">{evalRuns.length}</div>
              <div className="muted small">
                本次服务会话 · 成功 {evalSucceeded} · 失败 {evalFailed} · 流水线 {pipelines.length} 条
              </div>
            </div>
            <div className="card stat">
              <div className="k">评测失败</div>
              <div className={'v' + (evalFailed > 0 ? ' red' : '')}>{evalFailed}</div>
              <div className="muted small">
                {evalFailed > 0 ? '存在失败评测，请到评测中心查看' : '本次会话暂无失败评测'}
              </div>
            </div>
            <div className="card stat">
              <div className="k">真实 API Trace</div>
              <div className={'v' + (realApiCount > 0 ? ' green' : '')}>{realApiCount}</div>
              <div className="muted small">
                {realApiCount > 0 ? '已通过独立验证的真实模型调用' : '暂无已验证的真实 API 运行'}
              </div>
            </div>
            <div className="card stat">
              <div className="k">最近运行时间</div>
              <div className="v" style={{ fontSize: 18 }}>
                {formatTime(latestRun?.created_at)}
              </div>
              <div className="muted small">
                {latestRun ? `${latestRun.kind} · ${latestRun.status}` : '暂无运行记录'}
              </div>
            </div>
          </div>
        )}
        <div className="muted small mt">
          Trace 与来源分布取自持久化存储；运行统计为当前会话内记录。
        </div>
      </Card>

      <Card title="数据来源分布" action={<span className="muted small">全库 Trace 统计</span>}>
        {!sources ? (
          <EmptyState text="暂无来源统计数据" />
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>数据来源</th>
                <th>数量</th>
                <th>占比</th>
                <th>成功</th>
                <th>失败</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              {SOURCE_ORDER.map((source) => {
                const count = sources.by_source?.[source] ?? 0
                const status = sources.status_by_source?.[source] ?? {}
                const pct = totalTrace > 0 ? (count / totalTrace) * 100 : 0
                return (
                  <tr key={source}>
                    <td>
                      <span className={'badge ' + sourceBadgeTone(source)}>{source}</span>
                    </td>
                    <td>{count.toLocaleString('en-US')}</td>
                    <td style={{ minWidth: 120 }}>
                      <div className="arch-bar">
                        <span style={{ width: `${Math.max(pct, count > 0 ? 1 : 0)}%` }} />
                      </div>
                      <span className="muted small">{pct.toFixed(1)}%</span>
                    </td>
                    <td>{status.succeeded ?? 0}</td>
                    <td>{status.failed ?? 0}</td>
                    <td className="small muted">
                      {source === 'REAL_API'
                        ? '真实模型 API 调用，需独立验证标记'
                        : source === 'MOCK'
                          ? '本地 Mock Agent 实际执行'
                          : source === 'SIMULATED'
                            ? '评测/合成或模拟执行数据'
                            : source === 'REPLAY'
                              ? '重放、反事实或修复验证派生记录'
                              : '来源无法确认，不计入真实统计'}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
        <div className="muted small mt">
          REAL_API 仅统计具备独立验证证据的真实模型调用；Mock / 模拟 / 派生数据不视为真实生产验证。
        </div>
      </Card>

      <Card title="最近运行记录" action={<span className="muted small">最多显示最近 6 条</span>}>
        {recent.length === 0 ? (
          <EmptyState text="暂无运行记录" />
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>类型</th>
                <th>对象</th>
                <th>状态</th>
                <th>数据来源</th>
                <th>时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {recent.map((row) => (
                <tr key={row.key}>
                  <td className="small">{row.kind}</td>
                  <td className="mono small" title={row.title}>
                    {row.objectId}
                    <div className="muted small" style={{ fontFamily: 'inherit' }}>
                      {row.title}
                    </div>
                  </td>
                  <td>
                    <span className={`badge ${row.status === 'failed' ? 'failed' : row.status === 'succeeded' || row.status === 'SUCCEEDED' ? 'success' : 'muted'}`}>
                      {row.status}
                    </span>
                  </td>
                  <td>
                    <span className={'badge ' + sourceBadgeTone(row.source)}>{row.source}</span>
                  </td>
                  <td className="small muted">{formatTime(row.created_at)}</td>
                  <td>
                    <button className="btn" onClick={() => openRecent(row)}>
                      {row.kind === 'Agent 运行' && row.traceId ? '查看 Trace' : '进入模块'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="muted small mt">
          评测与流水线为模拟执行，成本与 Token 为模拟值。
        </div>
      </Card>
    </div>
  )
}

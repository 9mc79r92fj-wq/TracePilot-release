import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, CheckCircle2, Clock3, Copy, Play, RefreshCw, ShieldCheck, Terminal } from 'lucide-react'
import { api, apiErrorMessage } from '../api'
import DocumentPanel from '../components/DocumentPanel'
import { Card, EmptyState, ErrorBox, Spinner, StatusBadge } from '../components/ui'

interface AgentStatus {
  modes: {
    mode: string
    available: boolean
    simulated: boolean | null
    note: string
    blocking_reasons?: string[]
    warnings?: string[]
    api_key_configured?: boolean
  }[]
  allowed_models: string[]
  configured_model: string | null
  configured_model_valid: boolean | null
  default_real_model: string
  real_api_ready?: boolean
  real_api_blocking_reasons?: string[]
  real_api_warnings?: string[]
  api_key_configured?: boolean
  tool_whitelist: string[]
  limits?: {
    profile?: string
    agent_max_total_tokens?: number
    agent_max_output_tokens?: number
    agent_max_model_calls?: number
    agent_max_tool_calls?: number
    agent_max_tool_rounds?: number
    agent_max_wall_seconds?: number
    agent_max_document_refs?: number
    document_context_max_chars?: number
    document_per_file_max_chars?: number
    frontend_agent_timeout_ms?: number
  }
  data_sources: string[]
  paid_api_called: boolean
}

/** 把服务端就绪原因中的环境变量名转成面向访客的描述（不改变语义）。 */
function readableReadiness(items: string[]): string[] {
  return items.map((item) => item
    .replace(/服务端未设置 TRACEPILOT_ALLOW_REAL_API=1/g, '服务端未开启真实 API（需显式授权）')
    .replace(/未配置 DEEPSEEK_API_KEY/g, '服务端未配置模型 API Key')
    .replace(/TRACEPILOT_ALLOW_REAL_API=1/g, '真实 API 开关')
    .replace(/DEEPSEEK_API_KEY/g, '模型 API Key'))
}

interface ToolCall {
  name: string
  arguments: unknown
  result: unknown
}

interface RunRecord {
  run_id: string
  task: string
  mode: string
  data_source: string
  simulated: boolean
  status: string
  created_at: string
  limits: Record<string, number>
  model: string | null
  trace_id: string | null
  trace_path: string | null
  final_answer: string | null
  plan: string[]
  tool_calls: ToolCall[]
  token_usage: Record<string, unknown> | null
  elapsed_ms: number | null
  error: string | null
  limit_violations: string[]
  model_selection: Record<string, unknown> | null
  limitations: string[]
  links?: { trace: string; dag: string }
}

function DataSourceBadge({ source, simulated }: { source: string; simulated: boolean }) {
  if (source === 'real_api') {
    return <span className="badge success">REAL API · 真实调用</span>
  }
  if (source === 'mock') {
    return <span className="badge warning">MOCK · 模拟模型</span>
  }
  return <span className="badge muted">{source.toUpperCase()}{simulated ? ' · 模拟' : ''}</span>
}

export default function AgentRuns() {
  const navigate = useNavigate()
  const [status, setStatus] = useState<AgentStatus | null>(null)
  const [task, setTask] = useState('计算125乘以48')
  const [documentIds, setDocumentIds] = useState<string[]>([])
  const [mode, setMode] = useState<'mock' | 'real_api'>('mock')
  const [confirmReal, setConfirmReal] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [run, setRun] = useState<RunRecord | null>(null)
  const [history, setHistory] = useState<RunRecord[]>([])

  const load = useCallback(async () => {
    setError(null)
    try {
      const [st, runs] = await Promise.all([
        api.get<AgentStatus>('/api/agent/status'),
        api.get<{ runs: RunRecord[] }>('/api/agent/runs'),
      ])
      setStatus(st.data)
      setHistory(runs.data.runs)
      const real = st.data.modes.find((m) => m.mode === 'real_api')
      if (!real?.available) setMode('mock')
    } catch (e) {
      setError(apiErrorMessage(e))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const realAvailable = status?.modes.find((m) => m.mode === 'real_api')?.available ?? false
  const realMode = status?.modes.find((m) => m.mode === 'real_api')
  const realBlockers = readableReadiness(realMode?.blocking_reasons ?? status?.real_api_blocking_reasons ?? [])
  const realWarnings = readableReadiness(realMode?.warnings ?? status?.real_api_warnings ?? [])
  const limits = status?.limits
  const agentTimeoutMs = limits?.frontend_agent_timeout_ms ?? 210000
  const maxDocumentRefs = limits?.agent_max_document_refs ?? 5
  const [copiedId, setCopiedId] = useState<string | null>(null)
  async function copyId(id: string) {
    try {
      await navigator.clipboard.writeText(id)
      setCopiedId(id)
      window.setTimeout(() => setCopiedId((v) => v === id ? null : v), 1600)
    } catch {
      setError('复制失败，请手动选择并复制 ID')
    }
  }
  const shortId = (id: string) => id.length > 18 ? `${id.slice(0, 12)}…${id.slice(-5)}` : id

  async function execute() {
    if (!task.trim()) return
    // 任务明确提到文件/附件但未勾选引用文件时，提前提示，避免模型声称读取了文件
    if (
      documentIds.length === 0 &&
      /(上传|附件|文件|文档|表格|file|document|excel|csv|pdf|xlsx|docx)/i.test(task)
    ) {
      setError('任务提到文件，但未勾选任何已上传文件。请先在“文件资料工作台”勾选要引用的文件，否则模型不会收到文件内容。')
      return
    }
    setBusy(true)
    setError(null)
    setRun(null)
    try {
      const r = await api.post<{ run: RunRecord }>('/api/agent/runs', {
        task: task.trim(),
        document_ids: documentIds,
        mode,
        confirm_real: mode === 'real_api' ? confirmReal : false,
      }, { timeout: agentTimeoutMs })
      setRun(r.data.run)
      const runs = await api.get<{ runs: RunRecord[] }>('/api/agent/runs')
      setHistory(runs.data.runs)
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="stack">
      <div className="row">
        <h2 style={{ margin: 0 }}>Agent Runs</h2>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => void load()}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>
      <p className="muted small" style={{ margin: 0 }}>
        提交任务后由后端执行（模型推理 + 工具调用）并采集 Trace。
        默认为 <strong>Mock 模式</strong>（零成本）；真实 DeepSeek API 需服务端开关与请求确认。
      </p >

      {error && <ErrorBox message={error} />}

      <section className="ar-workspace" aria-label="Agent任务执行工作台">
        <div className="ar-task-card card">
          <div className="ar-panel-heading">
            <div>
              <div className="ar-eyebrow">EXECUTION WORKSPACE</div>
              <h3>创建 Agent 任务</h3>
              <p>输入任务并运行，执行过程会记录为 Trace。</p >
            </div>
            <span className="ar-mode-pill"><span className="ar-dot" />{mode === 'mock' ? 'Mock 环境' : 'Real API 环境'}</span>
          </div>
          <div className="ar-task-body">
            <label className="ar-field-label" htmlFor="agent-task">任务描述</label>
            <textarea
              id="agent-task"
              className="cf-textarea ar-task-input"
              rows={4}
              value={task}
              onChange={(e) => setTask(e.target.value)}
              placeholder="例如：计算125乘以48"
            />
            <div className="ar-execution-bar">
              <div className="ar-mode-control">
                <label className="ar-field-label" htmlFor="mode">执行模式</label>
                <select
                  id="mode"
                  value={mode}
                  onChange={(e) => setMode(e.target.value as 'mock' | 'real_api')}
                >
                  <option value="mock">Mock · 本地受控执行</option>
                  <option value="real_api" disabled={!realAvailable}>
                    Real API · {realAvailable ? '需授权' : (realBlockers[0] ?? '服务端未开启')}
                  </option>
                </select>
                {!realAvailable && realBlockers.length > 0 && (
                  <div className="muted small" style={{ marginTop: 6 }}>
                    Real API 不可用：{realBlockers.join('；')}
                  </div>
                )}
                {realAvailable && realWarnings.length > 0 && (
                  <div className="muted small" style={{ marginTop: 6 }}>
                    注意：{realWarnings.join('；')}
                  </div>
                )}
              </div>
              <button
                className="btn primary ar-run-btn"
                onClick={() => void execute()}
                disabled={busy || !task.trim() || (mode === 'real_api' && (!realAvailable || !confirmReal))}
              >
                <Play size={16} /> {busy ? '执行中…' : '运行任务'}
              </button>
            </div>
            {mode === 'real_api' && (
              <label className="ar-confirm small">
                <input type="checkbox" checked={confirmReal} onChange={(e) => setConfirmReal(e.target.checked)} />
                我确认本次真实模型调用可能产生费用
              </label>
            )}
            <div className="ar-workspace-foot">
              <span><CheckCircle2 size={14} /> Mock 默认不产生模型 API 费用</span>
              <span><Clock3 size={14} /> 执行完成后自动保存运行记录</span>
            </div>
          </div>
        </div>
         <aside className="ar-safety-card card" aria-label="运行环境与安全限制">
          <div className="ar-panel-heading ar-safety-heading">
            <div>
              <div className="ar-eyebrow">RUNTIME & SAFETY</div>
              <h3><ShieldCheck size={18} /> 安全与运行环境</h3>
            </div>
          </div>
          <div className="ar-safety-body">
            <div className="ar-safety-status">
              <span>本地 Mock</span><span className="badge success">可用</span>
            </div>
            <div className="ar-safety-status">
              <span>真实模型 API</span>
              <span className={'badge ' + (realAvailable ? 'success' : 'muted')}>{realAvailable ? '已开启 · 需确认' : '未开启'}</span>
            </div>
            <div className="ar-safety-divider" />
            <div className="ar-safety-subtitle">执行边界</div>
            <ul className="ar-safety-list">
              <li>真实 API 必须明确授权；失败不自动重试</li>
              <li>
                模型调用 ≤{limits?.agent_max_model_calls ?? 9} 次 · 工具调用 ≤{limits?.agent_max_tool_calls ?? 8} 次
              </li>
              <li>
                单次输出 ≤{limits?.agent_max_output_tokens ?? 2048} Token · 总 Token ≤{limits?.agent_max_total_tokens ?? 12000}
              </li>
              <li>
                工具轮次 ≤{limits?.agent_max_tool_rounds ?? 6} · 文件引用 ≤{maxDocumentRefs} 个 · 总时长 ≤
                {limits?.agent_max_wall_seconds ?? 180} 秒
              </li>
              <li>仅允许白名单内的本地工具</li>
            </ul>
            <details className="ar-runtime-details">
              <summary>查看模型与工具配置</summary>
              {status ? (
                <div className="ar-runtime-text">
                  <div>已配置模型：<span className="mono">{status.configured_model ?? '未配置'}</span></div>
                  {status.configured_model_valid === false && <div className="ar-warn">模型 ID 未通过校验</div>}
                  <div>允许模型：{status.allowed_models.join(' / ') || '未提供'}</div>
                  <div>工具白名单：{status.tool_whitelist.join(' / ') || '未提供'}</div>
                  <div>数据来源：{status.data_sources.join(' / ') || '未提供'}</div>
                </div>
              ) : <p className="muted small">运行环境信息尚未加载</p >}
            </details>
          </div>
        </aside>
      </section>

      <DocumentPanel selected={documentIds} onSelected={setDocumentIds} maxSelected={maxDocumentRefs} />

      {busy && <Spinner />}

      {run && (
        <Card title="执行结果">
          <div className="stack">
            <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}>
              <DataSourceBadge source={run.data_source} simulated={run.simulated} />
              <StatusBadge status={run.status === 'succeeded' ? 'succeeded' : run.status === 'limit_exceeded' ? 'partial' : 'failed'} />
              <span className="mono small">{run.run_id}</span>
              <span className="muted small">{run.elapsed_ms ?? '?'} ms</span>
            </div>

            {run.error && <ErrorBox message={run.error} />}

            <div className="detail-grid">
              <div className="field">
                <div className="k">模型</div>
                <div className="mono small">{run.model ?? '未记录'}</div>
              </div>
              <div className="field">
                <div className="k">Trace ID</div>
                <div className="mono small">{run.trace_id ?? '未记录'}</div>
              </div>
              <div className="field">
                <div className="k">Token</div>
                <div className="mono small">
                  {run.token_usage
                    ? `in=${run.token_usage.input_tokens} out=${run.token_usage.output_tokens} total=${run.token_usage.total_tokens}`
                    : '未记录'}
                </div>
              </div>
              <div className="field">
                <div className="k">数据来源</div>
                <div className="mono small">{run.data_source}</div>
              </div>
            </div>

            {run.model_selection && (
              <div className="cf-note small">
                <strong>模型选择</strong>
                <pre className="json" style={{ marginTop: 4 }}>
                  {JSON.stringify(run.model_selection, null, 2)}
                </pre>
              </div>
            )}

            <div className="field">
              <div className="k">执行计划</div>
              {run.plan.length ? (
                <ol style={{ margin: '4px 0 0 18px' }} className="small">
                  {run.plan.map((p, i) => <li key={i}>{p}</li>)}
                </ol>
              ) : <span className="muted small">未记录</span>}
            </div>

            <div className="field">
              <div className="k">工具调用（{run.tool_calls.length}）</div>
              {run.tool_calls.length === 0 ? (
                <span className="muted small">本次未调用工具</span>
              ) : (
                <div className="stack">
                  {run.tool_calls.map((t, i) => (
                    <div key={i} className="ev-box">
                      <div className="ev-title">{t.name}</div>
                      <div className="small mono">参数：{JSON.stringify(t.arguments)}</div>
                      <div className="small mono">结果：{JSON.stringify(t.result)}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="field">
              <div className="k">最终回答</div>
              <div className="cf-info">{run.final_answer ?? '未记录'}</div>
            </div>

            {run.limit_violations.length > 0 && (
              <div className="errorbox">
                超出自设上限：{run.limit_violations.join('；')}
              </div>
            )}

            {run.limitations.length > 0 && (
              <div className="muted small">
                <strong>限制说明</strong>
                <ul style={{ margin: '2px 0 0 16px' }}>
                  {run.limitations.map((l) => <li key={l}>{l}</li>)}
                </ul>
              </div>
            )}

            <div className="row">
              {run.trace_id && (
                <>
                  <button className="btn" onClick={() => navigate('/traces/' + run.trace_id)}>
                    查看 Trace <ArrowRight size={14} />
                  </button>
                  <button className="btn" onClick={() => navigate('/dag/' + run.trace_id)}>
                    查看 DAG <ArrowRight size={14} />
                  </button>
                </>
              )}
            </div>
          </div>
        </Card>
      )}

      <section className="ar-history card" aria-label="运行历史">
        <div className="ar-history-heading">
          <div>
            <div className="ar-eyebrow">RECENT ACTIVITY</div>
            <h3>运行历史 <span className="ar-count">{history.length}</span></h3>
          </div>
          <button className="btn" onClick={() => void load()} disabled={busy}><RefreshCw size={14} /> 刷新列表</button>
        </div>
        {history.length === 0 ? (
          <div className="ar-history-empty"><EmptyState text="还没有运行记录。运行上方任务后，结果会显示在这里。" /></div>
        ) : (
          <div className="ar-history-list">
            {[...history].reverse().map((h) => (
              <div className="ar-history-row" key={h.run_id}>
                <div className="ar-history-state">
                  <StatusBadge status={h.status === 'succeeded' ? 'succeeded' : h.status === 'limit_exceeded' ? 'partial' : 'failed'} />
                </div>
                <div className="ar-history-task" title={h.task}>{h.task}</div>
                <div className="ar-history-source"><DataSourceBadge source={h.data_source} simulated={h.simulated} /></div>
                <div className="ar-history-id">
                  <span className="mono" title={h.run_id}>{shortId(h.run_id)}</span>
                  <button className="ar-icon-btn" aria-label="复制运行ID" title="复制完整运行ID" onClick={() => void copyId(h.run_id)}>
                    {copiedId === h.run_id ? <CheckCircle2 size={14} /> : <Copy size={14} />}
                  </button>
                </div>
                <div className="ar-history-time"><Clock3 size={13} /> {h.elapsed_ms == null ? '未知' : `${h.elapsed_ms} ms`}</div>
                <div className="ar-history-actions">
                  <button className="btn ar-small-btn" onClick={() => setRun(h)}>详情</button>
                  {h.trace_id && (
                    <button className="btn ar-small-btn ar-trace-btn" onClick={() => navigate('/traces/' + encodeURIComponent(h.trace_id!))}>
                      Trace <ArrowRight size={13} />
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <details className="ar-provenance card">
        <summary>数据来源与真实性说明</summary>
        <div className="stack small ar-provenance-body">
          <div>
            <DataSourceBadge source="real_api" simulated={false} /> 真实调用 DeepSeek API，
            Token 来自 API 响应；metadata.simulated = false。
          </div>
          <div>
            <DataSourceBadge source="mock" simulated={true} /> 确定性脚本化模型 + 真实本地工具；
            Token 记为 0 并明确标注，metadata.simulated = true。
          </div>
          <div>
            <DataSourceBadge source="simulated" simulated={true} /> 纯合成轨迹（评测/故障注入产物）。
          </div>
          <div className="muted">
            <Terminal size={13} /> 模拟数据不会标记为真实数据。
          </div>
        </div>
      </details>
    </div>
  )
}

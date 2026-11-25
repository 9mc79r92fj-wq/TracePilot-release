import { useCallback, useEffect, useState } from 'react'
import { Play, RefreshCw } from 'lucide-react'
import { api, apiErrorMessage, fetchTraces } from '../api'
import type { TraceSummary } from '../types'
import { Card, EmptyState, ErrorBox, Spinner, StatusBadge } from '../components/ui'

interface Scenario {
  key: string
  label: string
  tool: string
}

interface RecordedEvent {
  index: number
  step_id: string
  step_type: string
  status: string
  is_fault: boolean
  fault_type: string | null
  error_type: string | null
  error_message: string | null
  duration_ms: number | null
}

interface RecordedReplay {
  mode: string
  trace_id: string
  step_count: number
  events: RecordedEvent[]
  fault_event_indices: number[]
  note: string
}

interface ControlledResult {
  mode: string
  scenario: string
  old_trace_id: string
  new_trace_id: string
  repeatable_faults: string[]
  inconsistent_behaviors: string[]
  called_paid_api: boolean
  executed_real_tools: string[]
  trace: { steps: { step_id: string; step_type: string; status: string; tool_name?: string | null }[] }
  comparison: {
    differences: Record<string, unknown>
    diff_nodes: unknown[]
    unmatched: { reference_only: string[]; target_only: string[] }
  }
  note: string
}

export default function FaultLab() {
  const [traces, setTraces] = useState<TraceSummary[]>([])
  const [scenarios, setScenarios] = useState<Scenario[]>([])
  const [traceId, setTraceId] = useState('')
  const [scenario, setScenario] = useState('')
  const [mode, setMode] = useState<'recorded' | 'controlled'>('recorded')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [recorded, setRecorded] = useState<RecordedReplay | null>(null)
  const [controlled, setControlled] = useState<ControlledResult | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      const [t, s] = await Promise.all([
        fetchTraces(),
        api.get<{ scenarios: Scenario[] }>('/api/replay/scenarios'),
      ])
      setTraces(t.traces)
      setScenarios(s.data.scenarios)
      const faultTrace = t.traces.find((x) => x.fault_types.length > 0) ?? t.traces[0]
      if (faultTrace) setTraceId(faultTrace.trace_id)
      if (s.data.scenarios.length) setScenario(s.data.scenarios[0].key)
    } catch (e) {
      setError(apiErrorMessage(e))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  async function startReplay() {
    if (!traceId) return
    setBusy(true)
    setError(null)
    setRecorded(null)
    setControlled(null)
    try {
      if (mode === 'recorded') {
        const r = await api.post<{ replay: RecordedReplay }>('/api/replay/recorded', { trace_id: traceId })
        setRecorded(r.data.replay)
      } else {
        const r = await api.post<{ replay: ControlledResult }>('/api/replay/controlled', {
          trace_id: traceId,
          scenario: scenario || undefined,
        })
        setControlled(r.data.replay)
      }
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  const selected = traces.find((t) => t.trace_id === traceId)

  return (
    <div className="stack">
      <div className="row">
        <h2 style={{ margin: 0 }}>故障重放</h2>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => void load()}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>

      {error && <ErrorBox message={error} />}

      <div className="split-config">
        <div className="stack">
      <Card title="重放配置">
        <div className="stack">
          <div className="row">
            <label className="small muted" htmlFor="mode">重放模式</label>
            <select id="mode" value={mode} onChange={(e) => setMode(e.target.value as 'recorded' | 'controlled')}>
              <option value="recorded">Recorded Replay（只读重放）</option>
              <option value="controlled">Controlled Re-run（受控重跑）</option>
            </select>
          </div>
          <div className="row">
            <label className="small muted" htmlFor="trace">原始 Trace</label>
            <select id="trace" value={traceId} onChange={(e) => setTraceId(e.target.value)}>
              {traces.length === 0 && <option value="">无可用 Trace</option>}
              {traces.map((t) => (
                <option key={t.trace_id} value={t.trace_id}>
                  {t.trace_id}{t.fault_types.length ? ' · 故障: ' + t.fault_types.join('/') : ''}
                </option>
              ))}
            </select>
          </div>
          {mode === 'controlled' && (
            <div className="row">
              <label className="small muted" htmlFor="scenario">故障场景（白名单）</label>
              <select id="scenario" value={scenario} onChange={(e) => setScenario(e.target.value)}>
                {scenarios.map((s) => (
                  <option key={s.key} value={s.key}>
                    {s.label}（{s.tool}）
                  </option>
                ))}
              </select>
            </div>
          )}
          {selected && (
            <p className="muted small" style={{ margin: 0 }}>
              原始 Trace：状态 {selected.status} · 步骤 {selected.step_count} · 故障{' '}
              {selected.fault_types.length ? selected.fault_types.join(', ') : '无'}
            </p>
          )}
          <div className="row">
            <button className="btn primary" onClick={() => void startReplay()} disabled={busy || !traceId}>
              <Play size={15} /> 开始重放
            </button>
            {busy && <span className="muted small">运行中…</span>}
          </div>
          <p className="muted small" style={{ margin: 0 }}>
            默认使用模拟模型与白名单本地工具：不调用付费 API、不执行系统命令、不修改原始 Trace。
          </p>
        </div>
      </Card>
        </div>

        <div className="stack">
      {busy && <Spinner />}

      {mode === 'recorded' && recorded && (
        <Card title={'重放结果（' + recorded.step_count + ' 事件）'}>
          <div className="stack">
            <p className="muted small" style={{ margin: 0 }}>{recorded.note}</p>
            <table className="tbl">
              <thead>
                <tr><th>#</th><th>Step</th><th>类型</th><th>状态</th><th>故障</th><th>错误</th></tr>
              </thead>
              <tbody>
                {recorded.events.map((e) => (
                  <tr key={e.step_id} style={e.is_fault ? { background: '#fff5f5' } : undefined}>
                    <td>{e.index + 1}</td>
                    <td className="mono small">{e.step_id}</td>
                    <td className="small">{e.step_type}</td>
                    <td><StatusBadge status={e.status} /></td>
                    <td>{e.is_fault ? <span className="badge failed">{e.fault_type}</span> : '—'}</td>
                    <td className="small">{e.error_type ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {mode === 'controlled' && controlled && (
        <>
          <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))' }}>
            <Card title="原始 Trace">
              <div className="stack small">
                <div className="mono">{controlled.old_trace_id}</div>
                <div>场景：{controlled.scenario}</div>
              </div>
            </Card>
            <Card title="重放 Trace（新 ID）">
              <div className="stack small">
                <div className="mono">{controlled.new_trace_id}</div>
                <div>步骤数：{controlled.trace.steps.length}</div>
              </div>
            </Card>
          </div>

          <Card title="结果摘要">
            <div className="stack small">
              <div>
                可重复故障：
                {controlled.repeatable_faults.length ? (
                  <span className="row" style={{ display: 'inline-flex', marginLeft: 6 }}>
                    {controlled.repeatable_faults.map((f) => <span key={f} className="badge success">{f}</span>)}
                  </span>
                ) : '无'}
              </div>
              <div>
                不一致行为：
                {controlled.inconsistent_behaviors.length ? (
                  <ul style={{ margin: '4px 0 0 16px' }}>
                    {controlled.inconsistent_behaviors.map((b) => <li key={b}>{b}</li>)}
                  </ul>
                ) : '无'}
              </div>
              <div>实际执行工具：{controlled.executed_real_tools.join(', ') || '无'}</div>
              <div>调用付费 API：{controlled.called_paid_api ? '是' : '否'}</div>
            </div>
          </Card>

          <Card title={'两次执行差异（差异节点 ' + controlled.comparison.diff_nodes.length + '）'}>
            <div className="stack small">
              <div>未匹配步骤：原始独有 {controlled.comparison.unmatched.reference_only.length}，重放独有 {controlled.comparison.unmatched.target_only.length}</div>
              <pre className="json">{JSON.stringify(controlled.comparison.differences, null, 2)}</pre>
            </div>
          </Card>
        </>
      )}

      {!busy && !recorded && !controlled && !error && (
        <EmptyState text="选择模式与 Trace 后点击「开始重放」" />
      )}
        </div>
      </div>
    </div>
  )
}

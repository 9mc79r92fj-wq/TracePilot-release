import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Play, RefreshCw, ShieldAlert, ShieldCheck } from 'lucide-react'
import { api, apiErrorMessage } from '../api'
import { Card, EmptyState, ErrorBox, Spinner } from '../components/ui'

interface AttackType { key: string; label: string }

interface TypesResp {
  attack_types: AttackType[]
  difficulties: string[]
  safety_constraints: string[]
  execution_environment: {
    mode: string
    tools_whitelist: string[]
    permissions: string
    network: string
    real_credentials: string
    paid_api_called: boolean
  }
  metrics_note: string
}

interface SafetyVerdict {
  verdict: 'SAFE' | 'UNSAFE' | 'INCONCLUSIVE'
  satisfied: string[]
  violations: string[]
  checked: { constraint: string; satisfied: boolean; detail: string }[]
  note: string
}

interface TaskVerdict {
  verdict: 'SUCCESS' | 'FAILURE'
  tool_succeeded: boolean
  reason: string
  note: string
}

interface CaseRow {
  case_id: string
  attack_id: string
  attack_type: string
  title: string
  difficulty: string
  template_id: string
  variant_index: number
  mutation_rules: Record<string, string>
  status: string
  trace_id: string | null
  safety: SafetyVerdict | null
  task: TaskVerdict | null
  is_canary: boolean
  is_bad_case: boolean
  lineage: { template_id: string; variant_index: number; trace_id: string }
}

interface Metrics {
  safety_pass_rate: number | null
  safe_count: number
  unsafe_count: number
  task_success_rate: number | null
  task_success_count: number
  task_failure_count: number
  execution_error_count: number
  canary_count: number
  canary_detected_count: number
  canary_missed_count: number
  detector_verified: boolean
  by_attack_type: Record<string, Record<string, number>>
  note: string
}

interface RunDetail {
  run_id: string
  case_count: number
  metrics: Metrics
  report: {
    dataset: { dataset_id: string; total_cases: number; executed_cases: number; generation: Record<string, unknown> }
    cases: CaseRow[]
    unsafe_cases: { case_id: string; attack_type: string; violations: string[]; trace_id: string }[]
    execution_errors: { case_id: string; error: string }[]
    limitations: string[]
  }
  paid_api_called: boolean
}

const TYPE_BADGE: Record<string, string> = {
  prompt_injection: 'badge failed',
  tool_misuse: 'badge partial',
  permission_boundary: 'badge warning',
  invalid_arguments: 'badge blue',
  conflicting_instructions: 'badge cyan',
  fault_recovery: 'badge muted',
}

const TYPE_LABEL: Record<string, string> = {
  prompt_injection: '提示注入',
  tool_misuse: '工具误用',
  permission_boundary: '权限边界',
  invalid_arguments: '非法参数',
  conflicting_instructions: '指令冲突',
  fault_recovery: '故障恢复',
}

export default function RedTeamLab() {
  const navigate = useNavigate()
  const [types, setTypes] = useState<TypesResp | null>(null)
  const [selectedTypes, setSelectedTypes] = useState<string[]>([])
  const [variations, setVariations] = useState(2)
  const [seed, setSeed] = useState(42)
  const [includeCanary, setIncludeCanary] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [run, setRun] = useState<RunDetail | null>(null)
  const [filter, setFilter] = useState<'all' | 'unsafe' | 'canary'>('all')
  const [active, setActive] = useState<CaseRow | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      const r = await api.get<TypesResp>('/api/redteam/types')
      setTypes(r.data)
      setSelectedTypes(r.data.attack_types.map((t) => t.key))
    } catch (e) {
      setError(apiErrorMessage(e))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  async function runRedTeam() {
    setBusy(true)
    setError(null)
    setRun(null)
    setActive(null)
    try {
      const created = await api.post<{ run_id: string }>('/api/redteam/runs', {
        regenerate: true,
        attack_types: selectedTypes.length ? selectedTypes : null,
        variations,
        seed,
        include_canary: includeCanary,
      })
      const detail = await api.get<RunDetail>(`/api/redteam/runs/${created.data.run_id}`)
      setRun(detail.data)
      const first = detail.data.report.cases[0]
      if (first) setActive(first)
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  function toggleType(key: string) {
    setSelectedTypes((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    )
  }

  const rows = run
    ? run.report.cases.filter((c) => {
        if (filter === 'unsafe') return c.safety?.verdict === 'UNSAFE'
        if (filter === 'canary') return c.is_canary
        return true
      })
    : []

  return (
    <div className="stack">
      <div className="row">
        <h2 style={{ margin: 0 }}>Red Team Lab</h2>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => void load()}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>
      <p className="muted small" style={{ margin: 0 }}>
        生成六类受控安全测试案例并做确定性安全判定。
        <strong> 安全通过率与任务成功率独立统计</strong>；金丝雀用例用于验证检测器有效性。
      </p>

      {error && <ErrorBox message={error} />}

      <div className="split-config">
        <div className="stack">
      <Card title="生成配置">
        <div className="stack">
          <div className="field">
            <div className="k">攻击类型</div>
            <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
              {(types?.attack_types ?? []).map((t) => (
                <label key={t.key} className="row small" style={{ gap: 6 }}>
                  <input
                    type="checkbox"
                    checked={selectedTypes.includes(t.key)}
                    onChange={() => toggleType(t.key)}
                  />
                  <span className={TYPE_BADGE[t.key] ?? 'badge muted'}>{TYPE_LABEL[t.key] ?? t.key}</span>
                  <span className="muted">{t.label}</span>
                </label>
              ))}
            </div>
          </div>

          <div className="row" style={{ gap: 12, flexWrap: 'wrap' }}>
            <label className="row small" htmlFor="rt-var">每模板变体数</label>
            <input
              id="rt-var"
              type="number"
              min={1}
              max={4}
              value={variations}
              onChange={(e) => setVariations(Number(e.target.value))}
              style={{ width: 80 }}
            />
            <label className="row small" htmlFor="rt-seed">随机种子（可复现）</label>
            <input
              id="rt-seed"
              type="number"
              min={0}
              value={seed}
              onChange={(e) => setSeed(Number(e.target.value))}
              style={{ width: 100 }}
            />
            <label className="row small" style={{ gap: 6 }}>
              <input
                type="checkbox"
                checked={includeCanary}
                onChange={(e) => setIncludeCanary(e.target.checked)}
              />
              包含金丝雀负控
            </label>
            <button className="btn primary" onClick={() => void runRedTeam()} disabled={busy}>
              <Play size={15} /> 生成并运行
            </button>
            {busy && <span className="muted small">生成/执行中…</span>}
          </div>

          {types && (
            <div className="cf-info small">
              <div>
                <span className="muted">执行环境</span> {types.execution_environment.mode} · 权限系统{' '}
                {types.execution_environment.permissions} · 网络 {types.execution_environment.network}
              </div>
              <div>
                <span className="muted">工具白名单</span> {types.execution_environment.tools_whitelist.join(' / ')}
              </div>
              <div>
                <span className="muted">真实凭据</span> {types.execution_environment.real_credentials} · 付费 API{' '}
                {types.execution_environment.paid_api_called ? '已调用' : '未调用'}
              </div>
              <div className="muted">{types.metrics_note}</div>
            </div>
          )}
        </div>
      </Card>
        </div>

        <div className="stack">
      {busy && <Spinner />}
      {!busy && !run && !error && <EmptyState text="选择攻击类型后点击「生成并运行」" />}

      {run && (
        <>
          <div className="grid stats">
            <div className="card stat">
              <div className="k">安全通过率</div>
              <div className="v green">
                {run.metrics.safety_pass_rate === null
                  ? 'N/A'
                  : (run.metrics.safety_pass_rate * 100).toFixed(1) + '%'}
              </div>
              <div className="muted small">
                {run.metrics.safe_count} SAFE / {run.metrics.unsafe_count} UNSAFE（攻击案例，不含金丝雀）
              </div>
            </div>
            <div className="card stat">
              <div className="k">任务成功率（独立指标）</div>
              <div className="v">
                {run.metrics.task_success_rate === null
                  ? 'N/A'
                  : (run.metrics.task_success_rate * 100).toFixed(1) + '%'}
              </div>
              <div className="muted small">
                {run.metrics.task_success_count} 成功 / {run.metrics.task_failure_count} 失败
              </div>
            </div>
            <div className="card stat">
              <div className="k">金丝雀检测器</div>
              <div className="v">{run.metrics.detector_verified ? '已验证' : '失效'}</div>
              <div className="muted small">
                检出 {run.metrics.canary_detected_count}/{run.metrics.canary_count} · 漏检{' '}
                {run.metrics.canary_missed_count}
              </div>
            </div>
            <div className="card stat">
              <div className="k">案例 / 执行错误</div>
              <div className="v">
                {run.case_count} / {run.metrics.execution_error_count}
              </div>
              <div className="muted small">
                seed {String(run.report.dataset.generation?.seed ?? '—')} · 变体{' '}
                {String(run.report.dataset.generation?.variations ?? '—')}
              </div>
            </div>
          </div>

          <Card title="按攻击类型统计">
            <table className="tbl">
              <thead>
                <tr><th>攻击类型</th><th>执行</th><th>SAFE</th><th>UNSAFE</th><th>任务成功</th><th>任务失败</th></tr>
              </thead>
              <tbody>
                {Object.entries(run.metrics.by_attack_type).map(([k, v]) => (
                  <tr key={k}>
                    <td><span className={TYPE_BADGE[k] ?? 'badge muted'}>{TYPE_LABEL[k] ?? k}</span></td>
                    <td>{v.executed}</td>
                    <td>{v.safe}</td>
                    <td>{v.unsafe}</td>
                    <td>{v.task_success}</td>
                    <td>{v.task_failure}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          <Card
            title={'案例结果（' + rows.length + '）'}
            action={
              <select value={filter} onChange={(e) => setFilter(e.target.value as typeof filter)}>
                <option value="all">全部</option>
                <option value="unsafe">仅 UNSAFE</option>
                <option value="canary">仅金丝雀</option>
              </select>
            }
          >
            <table className="tbl">
              <thead>
                <tr><th>案例</th><th>类型</th><th>难度</th><th>安全</th><th>任务</th><th></th></tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <tr key={c.case_id}>
                    <td className="mono small">
                      {c.attack_id}
                      {c.is_canary && <span className="badge warning" style={{ marginLeft: 6 }}>金丝雀</span>}
                    </td>
                    <td><span className={TYPE_BADGE[c.attack_type] ?? 'badge muted'}>{TYPE_LABEL[c.attack_type] ?? c.attack_type}</span></td>
                    <td className="small">{c.difficulty}</td>
                    <td>
                      {c.safety?.verdict === 'SAFE' ? (
                        <span className="badge success">SAFE</span>
                      ) : (
                        <span className="badge failed">{c.safety?.verdict ?? '—'}</span>
                      )}
                    </td>
                    <td>
                      <span className={c.task?.verdict === 'SUCCESS' ? 'badge success' : 'badge muted'}>
                        {c.task?.verdict ?? '—'}
                      </span>
                    </td>
                    <td>
                      <button className="btn" style={{ padding: '2px 8px' }} onClick={() => setActive(c)}>
                        详情
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          {active && (
            <Card title="案例详情">
              <div className="stack small">
                <div className="mono small">{active.attack_id}</div>
                <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
                  <span className={TYPE_BADGE[active.attack_type] ?? 'badge muted'}>{TYPE_LABEL[active.attack_type] ?? active.attack_type}</span>
                  <span className="badge muted">模板 {active.template_id}</span>
                  <span className="badge muted">变体 v{active.variant_index}</span>
                  <span className="badge muted">{JSON.stringify(active.mutation_rules)}</span>
                </div>

                <div>
                  <strong>安全判定</strong>
                  <div className="row" style={{ gap: 8, marginTop: 4 }}>
                    {active.safety?.verdict === 'SAFE' ? (
                      <span className="badge success"><ShieldCheck size={12} /> SAFE</span>
                    ) : (
                      <span className="badge failed"><ShieldAlert size={12} /> {active.safety?.verdict}</span>
                    )}
                    <span className="muted">已检查 {active.safety?.checked.length ?? 0} 条约束</span>
                  </div>
                  <table className="tbl" style={{ marginTop: 6 }}>
                    <thead><tr><th>约束</th><th>结果</th><th>说明</th></tr></thead>
                    <tbody>
                      {(active.safety?.checked ?? []).map((ch) => (
                        <tr key={ch.constraint}>
                          <td className="mono small">{ch.constraint}</td>
                          <td>{ch.satisfied ? <span className="badge success">满足</span> : <span className="badge failed">违反</span>}</td>
                          <td className="small muted">{ch.detail}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                <div>
                  <strong>任务判定（与安全分离）</strong>
                  <div className="muted">{active.task?.reason}</div>
                </div>

                <div>
                  <strong>关联 Trace</strong>
                  <div className="row" style={{ gap: 6, marginTop: 4 }}>
                    <span className="mono small">{active.trace_id ?? '不可用'}</span>
                    {active.trace_id && (
                      <>
                        <button className="btn" style={{ padding: '2px 8px' }} onClick={() => navigate('/traces/' + active.trace_id)}>
                          查看 Trace
                        </button>
                        <button className="btn" style={{ padding: '2px 8px' }} onClick={() => navigate('/dag/' + active.trace_id)}>
                          查看 DAG
                        </button>
                      </>
                    )}
                  </div>
                </div>

                <div>
                  <strong>预期安全行为</strong>
                  <div className="muted">{active.title}</div>
                </div>
              </div>
            </Card>
          )}

          <Card title="限制说明">
            <ul style={{ margin: 0, paddingLeft: 16 }} className="small">
              {run.report.limitations.map((l) => <li key={l}>{l}</li>)}
            </ul>
            <div className="muted small" style={{ marginTop: 6 }}>
              付费 API 调用：{run.paid_api_called ? '是' : '否'} · 数据来源 {run.report.dataset.dataset_id}
            </div>
          </Card>
        </>
      )}
        </div>
      </div>
    </div>
  )
}

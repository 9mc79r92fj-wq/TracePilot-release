import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Play, RefreshCw, ShieldAlert, ShieldCheck, Target } from 'lucide-react'
import { api, apiErrorMessage } from '../api'
import { Card, EmptyState, ErrorBox, Spinner } from '../components/ui'

interface AttackType { key: string; label: string }

interface LabTypes {
  attack_types: AttackType[]
  difficulties: string[]
  safety_constraints: string[]
  limits: Record<string, number | string | boolean>
  data_sources: string[]
  metrics_note: string
}

interface GenCase {
  case_id: string
  attack_id: string
  attack_type: string
  difficulty: string
  title: string
  template_id: string
  variant_index: number
  mutation_rules: Record<string, string>
  input_payload: string
  expected_safe_behavior: string
  safety_constraints: string[]
  data_source: string
  is_canary: boolean
}

interface GenResponse {
  dataset: {
    dataset_id: string
    dataset_version: string
    data_source: string
    total_cases: number
    canary_count: number
    generation: Record<string, unknown>
    by_attack_type: Record<string, number>
    by_difficulty: Record<string, number>
  }
  cases: GenCase[]
  limits: Record<string, unknown>
}

interface SafetyVerdict {
  verdict: 'SAFE' | 'UNSAFE' | 'INCONCLUSIVE'
  violations: string[]
  checked: { constraint: string; satisfied: boolean; detail: string }[]
}

interface CaseRow {
  case_id: string
  attack_id: string
  attack_type: string
  difficulty: string
  title: string
  status: string
  trace_id: string | null
  safety: SafetyVerdict | null
  task: { verdict: string; reason: string } | null
  is_canary: boolean
  is_bad_case: boolean
}

interface CoverageEntry {
  attack_type: string
  generated_cases: number
  executed_cases: number
  not_executed_cases: number
  safety_passed: number
  safety_failed: number
  unknown_cases: number
  coverage_status: 'FULL' | 'PARTIAL' | 'NOT_COVERED'
  canary_cases: number
}

interface Coverage {
  entries: CoverageEntry[]
  totals: {
    generated_cases: number
    executed_cases: number
    not_executed_cases: number
    safety_passed: number
    safety_failed: number
    unknown_cases: number
    attack_type_count: number
  }
  coverage_status: string
  definitions: Record<string, string>
  note: string
}

interface RunRecord {
  run_id: string
  status: string
  case_count: number
  attack_case_count: number
  canary_case_count: number
  dataset_id: string
  data_source: string
  paid_api_called: boolean
  metrics: {
    safety_pass_rate: number | null
    safe_count: number
    unsafe_count: number
    task_success_rate: number | null
    canary_detected_count: number
    canary_count: number
    detector_verified: boolean
    execution_error_count: number
    note: string
  }
  coverage: Coverage
  report: {
    cases: CaseRow[]
    dataset: { generation: Record<string, unknown>; total_cases: number; selected_cases: number; executed_cases: number }
    limitations: string[]
  }
  limits: Record<string, unknown>
}

const TYPE_BADGE: Record<string, string> = {
  prompt_injection: 'badge failed',
  tool_misuse: 'badge partial',
  permission_boundary: 'badge warning',
  invalid_arguments: 'badge blue',
  conflicting_instructions: 'badge cyan',
  fault_recovery: 'badge muted',
}

const COV_BADGE: Record<string, string> = {
  FULL: 'badge success',
  PARTIAL: 'badge warning',
  NOT_COVERED: 'badge failed',
}

const TYPE_LABEL: Record<string, string> = {
  prompt_injection: '提示注入',
  tool_misuse: '工具误用',
  permission_boundary: '权限边界',
  invalid_arguments: '非法参数',
  conflicting_instructions: '指令冲突',
  fault_recovery: '故障恢复',
}

export default function AdversarialLab() {
  const navigate = useNavigate()
  const [types, setTypes] = useState<LabTypes | null>(null)
  const [selectedTypes, setSelectedTypes] = useState<string[]>([])
  const [selectedDiffs, setSelectedDiffs] = useState<string[]>([])
  const [variations, setVariations] = useState(2)
  const [seed, setSeed] = useState(42)
  const [includeCanary, setIncludeCanary] = useState(true)

  const [gen, setGen] = useState<GenResponse | null>(null)
  const [selectedCases, setSelectedCases] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [run, setRun] = useState<RunRecord | null>(null)
  const [active, setActive] = useState<CaseRow | null>(null)

  // Bad Case Explorer 筛选
  const [fType, setFType] = useState('')
  const [fStatus, setFStatus] = useState('')
  const [fSafety, setFSafety] = useState('')
  const [fReason, setFReason] = useState('')

  const load = useCallback(async () => {
    setError(null)
    try {
      const r = await api.get<LabTypes>('/api/red-team/types')
      setTypes(r.data)
      setSelectedTypes(r.data.attack_types.map((t) => t.key))
      setSelectedDiffs([...r.data.difficulties])
    } catch (e) {
      setError(apiErrorMessage(e))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  async function generate() {
    setBusy(true)
    setError(null)
    setGen(null)
    setRun(null)
    setActive(null)
    setSelectedCases([])
    try {
      const r = await api.post<GenResponse>('/api/red-team/generate', {
        attack_types: selectedTypes.length ? selectedTypes : null,
        difficulties: selectedDiffs.length ? selectedDiffs : null,
        variations,
        seed,
        include_canary: includeCanary,
      })
      setGen(r.data)
      setSelectedCases(r.data.cases.map((c) => c.case_id))
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  async function runSelected() {
    if (selectedCases.length === 0) return
    setRunning(true)
    setError(null)
    setRun(null)
    setActive(null)
    try {
      const r = await api.post<RunRecord>('/api/red-team/runs', {
        regenerate: true,
        attack_types: selectedTypes.length ? selectedTypes : null,
        difficulties: selectedDiffs.length ? selectedDiffs : null,
        variations,
        seed,
        include_canary: includeCanary,
        case_ids: selectedCases,
        max_cases: Math.min(selectedCases.length, 60),
        persist_traces: true,
      })
      setRun(r.data)
      const first = r.data.report.cases[0]
      if (first) setActive(first)
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setRunning(false)
    }
  }

  const badCases = useMemo(() => {
    if (!run) return []
    return run.report.cases.filter((c) => c.safety?.verdict === 'UNSAFE')
  }, [run])

  const filteredBad = useMemo(() => {
    return badCases.filter((c) => {
      if (fType && c.attack_type !== fType) return false
      if (fStatus && c.status !== fStatus) return false
      if (fSafety && (c.safety?.verdict ?? '') !== fSafety) return false
      if (fReason && !(c.safety?.violations ?? []).some((v) => v.includes(fReason))) return false
      return true
    })
  }, [badCases, fType, fStatus, fSafety, fReason])

  function toggle(list: string[], v: string): string[] {
    return list.includes(v) ? list.filter((x) => x !== v) : [...list, v]
  }

  const coverage = run?.coverage

  return (
    <div className="stack">
      <div className="row">
        <h2 style={{ margin: 0 }}>Adversarial Evaluation Lab</h2>
        <div style={{ flex: 1 }} />
        <button className="btn" onClick={() => void load()}>
          <RefreshCw size={15} /> 刷新
        </button>
      </div>
      <p className="muted small" style={{ margin: 0 }}>
        受控对抗评测：生成攻击案例 → 执行 → 安全约束判定 → 覆盖地图 → Bad Case 列表。
        <strong> 安全通过率与任务成功率独立统计</strong>。
      </p>

      {error && <ErrorBox message={error} />}

      <div className="split-config">
        <div className="stack">
      <Card title="生成配置">
        <div className="stack">
          <div className="field">
            <div className="k">测试类型</div>
            <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
              {(types?.attack_types ?? []).map((t) => (
                <label key={t.key} className="row small" style={{ gap: 6 }}>
                  <input
                    type="checkbox"
                    checked={selectedTypes.includes(t.key)}
                    onChange={() => setSelectedTypes((p) => toggle(p, t.key))}
                  />
                  <span className={TYPE_BADGE[t.key] ?? 'badge muted'}>{TYPE_LABEL[t.key] ?? t.key}</span>
                  <span className="muted">{t.label}</span>
                </label>
              ))}
            </div>
          </div>

          <div className="field">
            <div className="k">难度</div>
            <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
              {(types?.difficulties ?? []).map((d) => (
                <label key={d} className="row small" style={{ gap: 6 }}>
                  <input
                    type="checkbox"
                    checked={selectedDiffs.includes(d)}
                    onChange={() => setSelectedDiffs((p) => toggle(p, d))}
                  />
                  <span className="badge muted">{d}</span>
                </label>
              ))}
            </div>
          </div>

          <div className="row" style={{ gap: 12, flexWrap: 'wrap' }}>
            <label className="row small" htmlFor="adv-var">变体数量</label>
            <input
              id="adv-var"
              type="number"
              min={1}
              max={4}
              value={variations}
              onChange={(e) => setVariations(Number(e.target.value))}
              style={{ width: 80 }}
            />
            <label className="row small" htmlFor="adv-seed">随机种子</label>
            <input
              id="adv-seed"
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
            <button className="btn primary" onClick={() => void generate()} disabled={busy || running}>
              <Target size={15} /> 生成案例
            </button>
            {busy && <span className="muted small">生成中…</span>}
          </div>

          {gen && (
            <div className="cf-info small">
              <div>
                <span className="muted">数据集</span> {gen.dataset.dataset_id} v{gen.dataset.dataset_version} ·
                共 {gen.dataset.total_cases} 例（金丝雀 {gen.dataset.canary_count}）
              </div>
              <div>
                <span className="muted">难度分布</span>{' '}
                {Object.entries(gen.dataset.by_difficulty).map(([k, v]) => `${k}:${v}`).join(' / ')}
              </div>
              <div>
                <span className="muted">数据来源</span>{' '}
                <span className="badge muted">{gen.dataset.data_source}</span> · 付费 API 未调用
              </div>
              {types && (
                <div className="muted">
                  限制：生成 ≤{String(types.limits.max_generated_cases)} · 执行 ≤
                  {String(types.limits.max_execute_cases)} · 并发 {String(types.limits.max_concurrent_runs)} ·
                  变体 ≤{String(types.limits.max_variants)}
                </div>
              )}
            </div>
          )}
        </div>
      </Card>
        </div>

        <div className="stack">
      {busy && <Spinner />}

      {gen && (
        <Card
          title={'生成结果（' + gen.cases.length + ' 例）'}
          action={
            <div className="row" style={{ gap: 6 }}>
              <button className="btn" onClick={() => setSelectedCases(gen.cases.map((c) => c.case_id))}>
                全选
              </button>
              <button className="btn" onClick={() => setSelectedCases([])}>清空</button>
              <button
                className="btn primary"
                onClick={() => void runSelected()}
                disabled={running || selectedCases.length === 0}
              >
                <Play size={15} /> 运行所选（{selectedCases.length}）
              </button>
            </div>
          }
        >
          <div className="stack">
            {running && (
              <div className="cf-note small">
                <Spinner /> 正在受控沙盒中执行 {selectedCases.length} 个案例（不调用付费模型）…
              </div>
            )}
            <table className="tbl">
              <thead>
                <tr><th></th><th>案例</th><th>类型</th><th>难度</th><th>变体</th><th>安全约束</th></tr>
              </thead>
              <tbody>
                {gen.cases.map((c) => (
                  <tr key={c.case_id}>
                    <td>
                      <input
                        type="checkbox"
                        checked={selectedCases.includes(c.case_id)}
                        onChange={() => setSelectedCases((p) => toggle(p, c.case_id))}
                        aria-label={`选择案例 ${c.attack_id}`}
                      />
                    </td>
                    <td className="mono small">
                      {c.attack_id}
                      {c.is_canary && <span className="badge warning" style={{ marginLeft: 6 }}>金丝雀</span>}
                    </td>
                    <td><span className={TYPE_BADGE[c.attack_type] ?? 'badge muted'}>{TYPE_LABEL[c.attack_type] ?? c.attack_type}</span></td>
                    <td className="small">{c.difficulty}</td>
                    <td className="small">v{c.variant_index}</td>
                    <td className="small">
                      {c.safety_constraints.map((s) => (
                        <span key={s} className="badge muted" style={{ marginRight: 4 }}>{s}</span>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <details>
              <summary className="small muted" style={{ cursor: 'pointer' }}>查看案例载荷与预期安全行为</summary>
              <div className="stack" style={{ marginTop: 8 }}>
                {gen.cases.slice(0, 8).map((c) => (
                  <div key={c.case_id} className="ev-box">
                    <div className="ev-title mono small">{c.case_id}</div>
                    <div className="small"><span className="muted">载荷：</span>{c.input_payload || '（空）'}</div>
                    <div className="small"><span className="muted">预期安全行为：</span>{c.expected_safe_behavior}</div>
                  </div>
                ))}
              </div>
            </details>
          </div>
        </Card>
      )}

      {run && coverage && (
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
                攻击案例 {run.metrics.safe_count} SAFE / {run.metrics.unsafe_count} UNSAFE
              </div>
            </div>
            <div className="card stat">
              <div className="k">任务成功率（独立）</div>
              <div className="v">
                {run.metrics.task_success_rate === null
                  ? 'N/A'
                  : (run.metrics.task_success_rate * 100).toFixed(1) + '%'}
              </div>
              <div className="muted small">与安全判定分开统计</div>
            </div>
            <div className="card stat">
              <div className="k">执行 / 生成</div>
              <div className="v">
                {coverage.totals.executed_cases} / {coverage.totals.generated_cases}
              </div>
              <div className="muted small">未执行 {coverage.totals.not_executed_cases}（不计入通过）</div>
            </div>
            <div className="card stat">
              <div className="k">金丝雀检测器</div>
              <div className="v">{run.metrics.detector_verified ? '已验证' : '失效'}</div>
              <div className="muted small">
                检出 {run.metrics.canary_detected_count}/{run.metrics.canary_count} · 执行错误{' '}
                {run.metrics.execution_error_count}
              </div>
            </div>
          </div>

          <Card title="攻击覆盖图">
            <div className="stack">
              <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                <span className={COV_BADGE[coverage.coverage_status] ?? 'badge muted'}>
                  总体覆盖 {coverage.coverage_status}
                </span>
                <span className="muted small">
                  生成 {coverage.totals.generated_cases} · 执行 {coverage.totals.executed_cases} · 未执行{' '}
                  {coverage.totals.not_executed_cases} · 通过 {coverage.totals.safety_passed} · 失败{' '}
                  {coverage.totals.safety_failed} · UNKNOWN {coverage.totals.unknown_cases}
                </span>
              </div>

              <div className="cov-heat">
                {coverage.entries.map((e) => {
                  const pct = e.generated_cases ? e.executed_cases / e.generated_cases : 0
                  const failPct = e.executed_cases ? e.safety_failed / e.executed_cases : 0
                  return (
                    <div key={e.attack_type} className="cov-cell" title={JSON.stringify(e)}>
                      <div className="cov-cell-head">
                        <span className={TYPE_BADGE[e.attack_type] ?? 'badge muted'}>{TYPE_LABEL[e.attack_type] ?? e.attack_type}</span>
                        <span className={COV_BADGE[e.coverage_status] ?? 'badge muted'}>
                          {e.coverage_status}
                        </span>
                      </div>
                      <div className="cov-bar" aria-label={`执行覆盖 ${Math.round(pct * 100)}%`}>
                        <span style={{ width: `${Math.round(pct * 100)}%` }} />
                      </div>
                      <div className="cov-cell-foot small">
                        生成 {e.generated_cases} · 执行 {e.executed_cases} · 未执行 {e.not_executed_cases}
                      </div>
                      <div className="cov-cell-foot small">
                        通过 {e.safety_passed} · 失败 {e.safety_failed} · UNKNOWN {e.unknown_cases}
                        {failPct > 0 && <span className="badge failed" style={{ marginLeft: 6 }}>有失败</span>}
                      </div>
                    </div>
                  )
                })}
              </div>

              <table className="tbl">
                <thead>
                  <tr>
                    <th>攻击类型</th><th>生成</th><th>执行</th><th>未执行</th>
                    <th>通过</th><th>失败</th><th>未知</th><th>覆盖状态</th>
                  </tr>
                </thead>
                <tbody>
                  {coverage.entries.map((e) => (
                    <tr key={e.attack_type}>
                      <td className="small">{TYPE_LABEL[e.attack_type] ?? e.attack_type}</td>
                      <td>{e.generated_cases}</td>
                      <td>{e.executed_cases}</td>
                      <td>{e.not_executed_cases}</td>
                      <td>{e.safety_passed}</td>
                      <td>{e.safety_failed}</td>
                      <td>{e.unknown_cases}</td>
                      <td><span className={COV_BADGE[e.coverage_status] ?? 'badge muted'}>{e.coverage_status}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="muted small">{coverage.note}</div>
            </div>
          </Card>

          <Card
            title={'Bad Case（' + filteredBad.length + '/' + badCases.length + '）'}
            action={
              <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
                <select value={fType} onChange={(e) => setFType(e.target.value)} aria-label="按风险类型筛选">
                  <option value="">全部风险类型</option>
                  {Object.keys(TYPE_LABEL).map((k) => (
                    <option key={k} value={k}>{TYPE_LABEL[k] ?? k}</option>
                  ))}
                </select>
                <select value={fStatus} onChange={(e) => setFStatus(e.target.value)} aria-label="按执行状态筛选">
                  <option value="">全部执行状态</option>
                  <option value="EXECUTED">EXECUTED</option>
                  <option value="EXECUTION_ERROR">EXECUTION_ERROR</option>
                </select>
                <select value={fSafety} onChange={(e) => setFSafety(e.target.value)} aria-label="按安全验证筛选">
                  <option value="">全部安全状态</option>
                  <option value="UNSAFE">UNSAFE</option>
                  <option value="SAFE">SAFE</option>
                  <option value="INCONCLUSIVE">INCONCLUSIVE</option>
                </select>
                <input
                  placeholder="失败原因关键字"
                  value={fReason}
                  onChange={(e) => setFReason(e.target.value)}
                  style={{ maxWidth: 200 }}
                  aria-label="按失败原因筛选"
                />
              </div>
            }
          >
            {filteredBad.length === 0 ? (
              <EmptyState text={badCases.length === 0 ? '本次运行无 Bad Case（全部 SAFE）' : '无匹配的 Bad Case'} />
            ) : (
              <table className="tbl">
                <thead>
                  <tr><th>案例</th><th>风险类型</th><th>失败原因</th><th>Trace</th><th></th></tr>
                </thead>
                <tbody>
                  {filteredBad.map((c) => (
                    <tr key={c.case_id}>
                      <td className="mono small">
                        {c.attack_id}
                        {c.is_canary && <span className="badge warning" style={{ marginLeft: 6 }}>金丝雀</span>}
                      </td>
                      <td><span className={TYPE_BADGE[c.attack_type] ?? 'badge muted'}>{TYPE_LABEL[c.attack_type] ?? c.attack_type}</span></td>
                      <td className="small">
                        {(c.safety?.violations ?? []).map((v) => (
                          <div key={v} className="mono small">{v.slice(0, 90)}</div>
                        ))}
                      </td>
                      <td className="mono small">{c.trace_id ? c.trace_id.slice(0, 22) + '…' : '不可用'}</td>
                      <td>
                        <div className="row" style={{ gap: 4 }}>
                          <button className="btn" style={{ padding: '2px 8px' }} onClick={() => setActive(c)}>
                            证据
                          </button>
                          {c.trace_id && (
                            <>
                              <button
                                className="btn"
                                style={{ padding: '2px 8px' }}
                                onClick={() => navigate('/traces/' + c.trace_id)}
                              >
                                Trace
                              </button>
                              <button
                                className="btn"
                                style={{ padding: '2px 8px' }}
                                onClick={() => navigate('/dag/' + c.trace_id)}
                              >
                                DAG
                              </button>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>

          {active && (
            <Card title="案例证据">
              <div className="stack small">
                <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                  <span className="mono small">{active.attack_id}</span>
                  <span className={TYPE_BADGE[active.attack_type] ?? 'badge muted'}>{TYPE_LABEL[active.attack_type] ?? active.attack_type}</span>
                  <span className="badge muted">{active.difficulty}</span>
                  {active.safety?.verdict === 'SAFE' ? (
                    <span className="badge success"><ShieldCheck size={12} /> SAFE</span>
                  ) : (
                    <span className="badge failed"><ShieldAlert size={12} /> {active.safety?.verdict}</span>
                  )}
                  <span className={active.task?.verdict === 'SUCCESS' ? 'badge success' : 'badge muted'}>
                    任务 {active.task?.verdict ?? '—'}
                  </span>
                </div>

                <div>
                  <strong>安全约束逐条结果</strong>
                  <table className="tbl" style={{ marginTop: 4 }}>
                    <thead><tr><th>约束</th><th>结果</th><th>说明</th></tr></thead>
                    <tbody>
                      {(active.safety?.checked ?? []).map((ch) => (
                        <tr key={ch.constraint}>
                          <td className="mono small">{ch.constraint}</td>
                          <td>
                            {ch.satisfied
                              ? <span className="badge success">满足</span>
                              : <span className="badge failed">违反</span>}
                          </td>
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
                  <strong>关联证据</strong>
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
              </div>
            </Card>
          )}

          <Card title="限制与来源">
            <ul style={{ margin: 0, paddingLeft: 16 }} className="small">
              {run.report.limitations.map((l) => <li key={l}>{l}</li>)}
            </ul>
            <div className="muted small" style={{ marginTop: 6 }}>
              数据来源 <span className="badge muted">{run.data_source}</span> · 付费 API{' '}
              {run.paid_api_called ? '已调用' : '未调用'} · 运行 {run.run_id}
            </div>
          </Card>
        </>
      )}
        </div>
      </div>
    </div>
  )
}

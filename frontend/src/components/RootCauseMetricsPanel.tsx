import { useState } from 'react'
import { Target } from 'lucide-react'
import { api, apiErrorMessage } from '../api'

/**
 * 根因准确率指标（第 55 步审计修复，原始需求创新点 6）。
 *
 * 用受控故障评测集的标注金标准对比 Bad Case Attribution 输出，展示：
 * Top-1 准确率 / Top-3 覆盖率 / 步骤级准确率 / 误归因率 / 平均成本 / 基线。
 * 诚实性：金标准为合成故障集；模拟数据不代表真实模型性能；成本为价格表估算，
 * PRICE_UNKNOWN 时显示「—」而不是 0；修复成功率未验证时显示「未验证」。
 */

interface Top3Case {
  case_id: string
  gold_cause_type: string
  first_candidate_type: string
  top3_candidate_types: string[]
  type_top1_hit?: boolean
  type_top3_hit?: boolean
  step_top1_hit?: boolean
}

interface MetricsReport {
  metrics_id: string
  evaluation_run_id: string
  dataset_id: string
  dataset_version: string
  evaluated_cases: number
  attributed_cases: number
  metrics: {
    labeled_cases: number
    unlabeled_cases: number
    type_top1_accuracy: number | null
    type_top3_coverage: number | null
    step_top1_accuracy: number | null
    step_top3_coverage: number | null
    misattribution_rate: number | null
    unknown_first_rate: number | null
    average_diagnosis_ms: number | null
    repair_attempted_cases: number
    repair_success_rate: number | null
    repair_note: string | null
    average_cost: number | null
    cost_currency: string | null
    cost_status: string | null
    baselines: {
      majority_cause_type: string | null
      majority_type_baseline_top1: number | null
      random_uniform_expected_top1: number | null
    }
  }
  cases: Top3Case[]
  limitations: string[]
  paid_api_called: boolean
}

function pct(value: number | null | undefined): string {
  return value == null ? '—' : `${(value * 100).toFixed(1)}%`
}

export function RootCauseMetricsPanel({ runId }: { runId: string }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [report, setReport] = useState<MetricsReport | null>(null)

  async function compute() {
    setBusy(true)
    setError(null)
    try {
      const response = await api.post<MetricsReport>(
        '/api/attribution/metrics',
        { evaluation_run_id: runId, max_cases: 50, include_repair: false },
        { timeout: 180000 },
      )
      setReport(response.data)
    } catch (e) {
      setError(apiErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="stack small">
      <div className="row" style={{ justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
        <span className="row" style={{ gap: 6 }}>
          <Target size={14} />
          <strong>根因准确率指标（Top-1 / Top-3）</strong>
        </span>
        <button className="btn" onClick={() => void compute()} disabled={busy}>
          {busy ? '计算中…' : '计算指标'}
        </button>
      </div>
      <div className="muted small">
        金标准来自合成故障评测集；指标不代表真实生产故障分布。
      </div>
      {error && <div className="errorbox">{error}</div>}
      {report && (
        <>
          <div className="rca-kpis">
            <div className="card stat">
              <div className="k">Top-1 准确率</div>
              <div className="v">{pct(report.metrics.type_top1_accuracy)}</div>
              <div className="muted small">类型命中 / 有金标准</div>
            </div>
            <div className="card stat">
              <div className="k">Top-3 覆盖率</div>
              <div className="v">{pct(report.metrics.type_top3_coverage)}</div>
              <div className="muted small">前 3 候选含金标准</div>
            </div>
            <div className="card stat">
              <div className="k">步骤 Top-1</div>
              <div className="v">{pct(report.metrics.step_top1_accuracy)}</div>
              <div className="muted small">真实故障注入步骤命中</div>
            </div>
            <div className="card stat">
              <div className="k">误归因率</div>
              <div className="v">{pct(report.metrics.misattribution_rate)}</div>
              <div className="muted small">首候选错误且非 UNKNOWN</div>
            </div>
            <div className="card stat">
              <div className="k">平均诊断耗时</div>
              <div className="v">
                {report.metrics.average_diagnosis_ms == null
                  ? '—'
                  : `${Math.round(report.metrics.average_diagnosis_ms)} ms`}
              </div>
              <div className="muted small">本机执行时间，非生产 SLA</div>
            </div>
            <div className="card stat">
              <div className="k">平均成本</div>
              <div className="v">
                {report.metrics.average_cost == null
                  ? '—'
                  : `${report.metrics.cost_currency ?? ''} ${report.metrics.average_cost}`.trim()}
              </div>
              <div className="muted small">
                {report.metrics.cost_status ?? '未知'}
                {report.metrics.cost_status === 'SIMULATED' ? '（模拟执行，非真实计费）' : ''}
              </div>
            </div>
          </div>
          <div className="muted small">
            修复成功率：
            {report.metrics.repair_success_rate == null
              ? '未验证（不按 0 或 100% 处理）'
              : pct(report.metrics.repair_success_rate)}
            {report.metrics.repair_note ? ` · ${report.metrics.repair_note}` : ''}
          </div>
          <div className="muted small">
            基线：多数类（{report.metrics.baselines.majority_cause_type ?? '—'}）Top-1{' '}
            {pct(report.metrics.baselines.majority_type_baseline_top1)}；随机基线期望{' '}
            {pct(report.metrics.baselines.random_uniform_expected_top1)}。基线仅反映类别分布。
          </div>
          <div className="rca-cases">
            {report.cases.map((item) => (
              <div key={item.case_id} className="rca-case-row">
                <span className="mono small">{item.case_id}</span>
                <span className="small">
                  金标准 <strong>{item.gold_cause_type}</strong> · 首候选{' '}
                  <strong>{item.first_candidate_type}</strong>
                </span>
                <span className={`badge ${item.type_top1_hit ? 'success' : 'failed'}`}>
                  Top-1 {item.type_top1_hit ? '命中' : '未命中'}
                </span>
                <span className={`badge ${item.type_top3_hit ? 'partial' : 'muted'}`}>
                  Top-3 {item.type_top3_hit ? '命中' : '未命中'}
                </span>
              </div>
            ))}
          </div>
          <div className="muted small">
            {report.limitations.join('；')}
          </div>
          <div className="muted small mono">
            {report.metrics_id} · {report.dataset_id} v{report.dataset_version} ·
            已归因 {report.attributed_cases}/{report.evaluated_cases}
          </div>
        </>
      )}
    </div>
  )
}

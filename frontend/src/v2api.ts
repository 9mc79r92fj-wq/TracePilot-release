// V2 实验 / 真实 API 预算 / 管理员会话 / 跨框架 Trace 导入的 API 封装。
//
// 全部走 api.ts 里已配置好 baseURL 的 axios 实例（同源），不新增第二套 HTTP 客户端。
// 本模块只做「读状态 + 受控操作」的薄封装，不在前端保存任何管理员令牌。

import axios from 'axios'
import { api, apiErrorMessage } from './api'

/** 可读的真实 API 拦截原因（把环境变量名换成面向访客的描述）。 */
export function readableReadiness(items: string[] | null | undefined): string[] {
  return (items ?? []).map((item) =>
    item
      .replace(/服务端未设置 TRACEPILOT_ALLOW_REAL_API=1/g, '服务端未开启真实 API（需显式授权）')
      .replace(/未配置 DEEPSEEK_API_KEY/g, '服务端未配置模型 API Key')
      .replace(/TRACEPILOT_ALLOW_REAL_API=1/g, '真实 API 开关')
      .replace(/DEEPSEEK_API_KEY/g, '模型 API Key')
      .replace(/TRACEPILOT_REAL_TOTAL_CALL_LIMIT/g, '真实调用总预算上限'),
  )
}

/** 统一错误文案：兼顾 FastAPI 字符串 detail 与 V2 的对象 detail（含 blocking_reasons）。 */
export function v2ErrorMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const status = err.response?.status
    const data = err.response?.data as
      | { detail?: unknown; message?: string; error?: string }
      | undefined
    const detail = data?.detail
    if (typeof detail === 'string' && detail.trim()) return detail
    if (detail && typeof detail === 'object') {
      const obj = detail as { message?: string; blocking_reasons?: string[]; code?: string }
      if (obj.message) {
        const reasons = readableReadiness(obj.blocking_reasons)
        return reasons.length ? `${obj.message}：${reasons.join('；')}` : obj.message
      }
    }
    if (status === 401) return '需要管理员登录（或管理员会话已过期）'
    if (status === 403) return '服务端拒绝：真实 API 未启用或预算未配置'
    if (status === 404) return '服务端尚未生成该数据（HTTP 404）'
    if (status === 409) return 'Trace ID 冲突：已存在同名 Trace，未覆盖'
    if (status === 422) return '请求校验失败：粘贴的 JSON 不符合 QueryForge Trace v1 协议'
    if (status === 429) return '请求过于频繁或预算不足，请稍后再试'
    if (!err.response) return '无法连接后端服务，请确认已启动'
  }
  return apiErrorMessage(err)
}

// ---------------------------------------------------------------------------
// 类型（与后端 GET /api/v2/status 等响应字段对齐；缺失字段按 null 处理）
// ---------------------------------------------------------------------------

export interface V2Budget {
  enabled: boolean
  total_calls_limit: number
  total_calls_used: number
  total_calls_remaining: number
  visitor_daily_limit: number
  visitor_day?: string | null
  visitor_calls_used?: number | null
  visitor_calls_remaining?: number | null
  total_tokens?: { input?: number | null; output?: number | null } | null
  estimated_cost_cny?: number | null
  price_estimate_note?: string | null
  updated_at?: string | null
  error?: string | null
  code?: string | null
}

export interface V2RealApiStatus {
  available: boolean
  models: string[]
  blocking_reasons: string[]
  public_real_enabled: boolean
}

export interface BenchmarkMethodMetrics {
  cases?: number | null
  predictions?: number | null
  top1_accuracy?: number | null
  top3_hit_rate?: number | null
  failure_step_accuracy?: number | null
  category_accuracy?: number | null
  category_macro_f1?: number | null
  llm_calls?: number | null
  total_tokens?: number | null
  avg_latency_ms?: number | null
  sample_size_definition?: string | null
}

export interface V2BenchmarkSummary {
  available: boolean
  dataset: string | null
  case_count: number | null
  metrics: Record<string, BenchmarkMethodMetrics> | null
  limitations: string[] | null
  file: string
}

export interface JudgeInfo {
  provider_kind?: string | null
  model_name?: string | null
  is_mock?: boolean | null
  simulated?: boolean | null
  rubric_id?: string | null
  rubric_version?: string | null
  [key: string]: unknown
}

export interface JudgeCalibrationMetrics {
  evaluable_cases?: number | null
  exact_agreement?: number | null
  exact_agreement_count?: number | null
  mae?: number | null
  within_1_agreement?: number | null
  within_1_agreement_count?: number | null
  [key: string]: unknown
}

export interface V2JudgeCalibration {
  available: boolean
  judge: JudgeInfo | null
  metrics: JudgeCalibrationMetrics | null
  evaluated_cases: number | null
  file: string
}

export interface V2Status {
  version: string
  real_api: V2RealApiStatus
  budget: V2Budget
  benchmark: V2BenchmarkSummary
  judge_calibration: V2JudgeCalibration
  paid_api_called_note?: string
}

export interface AdminSession {
  configured: boolean
  required: boolean
  authenticated: boolean
  session_ttl_seconds: number
  cookie_name: string
  note: string
}

export interface QueryforgeImportResult {
  trace_id: string
  source?: string
  schema_version?: string
  warnings?: string[]
  saved?: boolean
  step_count?: number
  requires_admin?: boolean
}

export interface QueryforgeSchemaInfo {
  schema: unknown
  protocol_version: string
  source: string
  note: string
}

export type RealInterventionType =
  | 'remove_fault'
  | 'adjust_timeout_retry'
  | 'correct_arguments'
  | 'context_fix'

export interface RealIntervention {
  type: RealInterventionType
  max_retries?: number
  correction?: string
  context?: string
}

export interface RealCounterfactualOutcome {
  trace_id: string
  status: string
  run_id?: string
  is_real_api?: boolean
  total_tokens?: number | null
  elapsed_ms?: number | null
}

export interface RealCounterfactualReport {
  experiment_id: string
  engine_version?: string
  execution_mode: string
  simulated?: boolean
  source_trace_id: string
  task?: string
  intervention?: Record<string, unknown>
  replays_requested?: number
  replays_completed?: number
  elapsed_ms?: number | null
  observed_outcome?: { trace_id: string; status: string; note?: string }
  intervention_outcomes?: RealCounterfactualOutcome[]
  replay_stability?: {
    replays?: number
    statuses?: string[]
    status_stable?: boolean
    note?: string
  }
  errors?: string[]
  causal_evidence_level?: string
  causal_evidence_note?: string
  control_conditions?: string[]
  confounders?: string[]
  limitations?: string[]
  original_trace_modified?: boolean
  paid_api_called?: boolean
}

export interface RealRepairCandidateResult {
  repair_id: string
  repair_type: string
  status: string
  experiment_id?: string
  outcome?: RealCounterfactualOutcome[]
  causal_evidence_level?: string
  reason?: string
}

export interface RealRepairValidation {
  trace_id: string
  candidate_count: number
  executed_count: number
  candidates: RealRepairCandidateResult[]
  execution_mode: string
  paid_api_called: boolean
  limitations: string[]
}

// ---------------------------------------------------------------------------
// 只读状态接口
// ---------------------------------------------------------------------------

/** V2 运营状态汇总（只读，不产生真实调用）。 */
export async function fetchV2Status(): Promise<V2Status> {
  const r = await api.get<V2Status>('/api/v2/status', { timeout: 30000 })
  return r.data
}

/**
 * 完整公开 Benchmark 报告（含逐案例结果、方法说明、限制）。
 * 404 表示服务端尚未生成结果文件：返回 null，由页面显示「尚未生成实验结果」。
 */
export async function fetchBenchmarkRootcause(): Promise<Record<string, unknown> | null> {
  try {
    const r = await api.get<Record<string, unknown>>('/api/v2/benchmark/rootcause', {
      timeout: 30000,
    })
    return r.data
  } catch (err) {
    if (axios.isAxiosError(err) && err.response?.status === 404) return null
    throw err
  }
}

/** 公开的真实调用预算余量（不含密钥）。 */
export async function fetchRealBudget(): Promise<{ budget: V2Budget; paid_api_called: boolean }> {
  const r = await api.get<{ budget: V2Budget; paid_api_called: boolean }>('/api/real-api/budget', {
    timeout: 30000,
  })
  return r.data
}

/** QueryForge Trace v1 JSON Schema（公开只读）。 */
export async function fetchQueryforgeSchema(): Promise<QueryforgeSchemaInfo> {
  const r = await api.get<QueryforgeSchemaInfo>('/api/interop/queryforge/schema', {
    timeout: 30000,
  })
  return r.data
}

// ---------------------------------------------------------------------------
// 管理员会话（令牌只在登录请求中传递；服务端返回 HttpOnly Cookie）
// ---------------------------------------------------------------------------

export async function fetchAdminSession(): Promise<AdminSession> {
  const r = await api.get<AdminSession>('/api/admin/session', { timeout: 20000 })
  return r.data
}

export async function adminLogin(token: string): Promise<{ authenticated: boolean }> {
  const r = await api.post<{ authenticated: boolean }>(
    '/api/admin/login',
    { token },
    { timeout: 20000 },
  )
  return r.data
}

export async function adminLogout(): Promise<{ authenticated: boolean }> {
  const r = await api.post<{ authenticated: boolean }>('/api/admin/logout', undefined, {
    timeout: 20000,
  })
  return r.data
}

// ---------------------------------------------------------------------------
// 真实调用（会消耗真实模型预算；仅管理员会话可用）
// ---------------------------------------------------------------------------

/** 真实 DeepSeek 受控反事实实验（单次重放；受服务端持久预算限制）。 */
export async function runRealCounterfactual(
  traceId: string,
  intervention: RealIntervention,
  replays = 1,
): Promise<RealCounterfactualReport> {
  const r = await api.post<{ experiment: RealCounterfactualReport }>(
    '/api/counterfactual/real-experiments',
    { trace_id: traceId, intervention, replays },
    // 单次真实任务最长 90 秒；留足模型网关抖动余量
    { timeout: 180000 },
  )
  return r.data.experiment
}

/** 用真实 API 重放验证修复候选（最多 3 个候选，每个 1 次真实重放）。 */
export async function validateRealRepairs(
  traceId: string,
  maxCandidates: number,
): Promise<RealRepairValidation> {
  const r = await api.post<RealRepairValidation>(
    '/api/repairs/validate-real',
    { trace_id: traceId, max_candidates: maxCandidates },
    { timeout: 300000 },
  )
  return r.data
}

/** 导入一条 QueryForge Trace v1（需管理员会话；按 trace_id 不覆盖已有 Trace）。 */
export async function importQueryforgeTrace(payload: unknown): Promise<QueryforgeImportResult> {
  const r = await api.post<QueryforgeImportResult>('/api/interop/queryforge/import', payload, {
    timeout: 120000,
  })
  return r.data
}

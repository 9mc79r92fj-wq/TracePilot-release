// 统一 API 请求封装：基于 axios，从环境变量读取后端地址（默认 127.0.0.1:8000）

import axios from 'axios'
import type {
  CompareResponse,
  DagResponse,
  FailedSummaryResponse,
  GovernanceSourcesResponse,
  Health,
  PropagationResponse,
  TraceClassificationResponse,
  TraceEligibility,
  TraceResponse,
  TracesResponse,
} from './types'

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'

export const api = axios.create({
  baseURL: BASE_URL,
  timeout: 15000,
})

/** 从任意异常中提取可读错误信息（统一错误处理）。 */
export function apiErrorMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    if (err.code === 'ECONNABORTED') return '请求超时'
    if (!err.response) return '无法连接后端服务，请确认已启动'
    const data = err.response.data as
      | { message?: string; error?: string; detail?: unknown }
      | undefined
    // FastAPI 的 HTTPException 把可读原因放在 detail；422 校验错误是数组，忽略
    const detail = typeof data?.detail === 'string' ? data.detail : undefined
    return (
      data?.message ??
      data?.error ??
      detail ??
      `请求失败（HTTP ${err.response.status}）`
    )
  }
  if (err instanceof Error) return err.message
  return String(err)
}

export async function fetchHealth(): Promise<Health> {
  const r = await api.get<Health>('/api/health')
  return r.data
}

export interface TraceListParams {
  sourceClass?: string
  status?: string
  faultOnly?: boolean
}

export async function fetchTraces(params?: TraceListParams): Promise<TracesResponse> {
  // Trace 数量增长后列表接口可能需要数秒；显式放宽超时，避免全站并发验收时误报
  const r = await api.get<TracesResponse>('/api/traces', {
    timeout: 60000,
    params: params
      ? {
          source_class: params.sourceClass || undefined,
          status: params.status || undefined,
          fault_only: params.faultOnly ? true : undefined,
        }
      : undefined,
  })
  return r.data
}

/** 全库数据来源分类汇总（只读；不使用真实 API）。 */
export async function fetchGovernanceSources(): Promise<GovernanceSourcesResponse> {
  const r = await api.get<GovernanceSourcesResponse>('/api/governance/sources', {
    timeout: 60000,
  })
  return r.data
}

/** 失败 Trace 口径汇总（主 Trace 目录）。 */
export async function fetchFailedSummary(limit = 10): Promise<FailedSummaryResponse> {
  const r = await api.get<FailedSummaryResponse>('/api/governance/failed-summary', {
    params: { limit },
    timeout: 60000,
  })
  return r.data
}

/** 单条 Trace 的来源分类（只读派生，不修改 Trace）。 */
export async function fetchTraceClassification(
  traceId: string,
): Promise<TraceClassificationResponse> {
  const r = await api.get<TraceClassificationResponse>(
    `/api/governance/traces/${encodeURIComponent(traceId)}`,
    { timeout: 60000 },
  )
  return r.data
}

/** 单条 Trace 的四类受控操作资格与具体原因。 */
export async function fetchTraceEligibility(traceId: string): Promise<TraceEligibility> {
  const r = await api.get<{ eligibility: TraceEligibility }>(
    `/api/governance/traces/${encodeURIComponent(traceId)}/eligibility`,
    { timeout: 60000 },
  )
  return r.data.eligibility
}

export async function fetchTrace(traceId: string): Promise<TraceResponse> {
  const r = await api.get<TraceResponse>(`/api/traces/${encodeURIComponent(traceId)}`)
  return r.data
}

export async function fetchTraceDag(traceId: string): Promise<DagResponse> {
  const r = await api.get<DagResponse>(`/api/traces/${encodeURIComponent(traceId)}/dag`)
  return r.data
}

export async function fetchPropagation(
  traceId: string,
  baselineTraceId?: string,
): Promise<PropagationResponse> {
  const r = await api.get<PropagationResponse>(
    `/api/traces/${encodeURIComponent(traceId)}/propagation`,
    { params: baselineTraceId ? { baseline_trace_id: baselineTraceId } : undefined },
  )
  return r.data
}

export async function fetchCompare(
  referenceTraceId: string,
  targetTraceId: string,
): Promise<CompareResponse> {
  const r = await api.post<CompareResponse>('/api/traces/compare', {
    reference_trace_id: referenceTraceId,
    target_trace_id: targetTraceId,
  })
  return r.data
}

// TracePilot 前端类型定义（与后端 JSON 响应对齐）

export interface Health {
  status: string
  service: string
  trace_count: number
}

/** 数据来源分类（与后端 data_governance.SOURCE_CLASSES 对齐）。 */
export type SourceClass = 'REAL_API' | 'MOCK' | 'SIMULATED' | 'REPLAY' | 'UNKNOWN'

export interface TraceSummary {
  trace_id: string
  task_id: string
  run_id: string
  status: string
  step_count: number
  fault_types: string[]
  started_at: string
  ended_at: string | null
  data_source: string | null
  simulated: boolean
  derivation: string | null
  source_class: SourceClass
  source_reasons: string[]
}

export interface TracesResponse {
  count: number
  total_count: number
  scope: string
  scope_note: string
  filters: {
    source_class: string | null
    status: string | null
    fault_only: boolean
  }
  by_source: Record<SourceClass, number>
  by_status: Record<string, number>
  traces: TraceSummary[]
}

/** 全库数据来源分类汇总（GET /api/governance/sources）。 */
export interface GovernanceSourcesResponse {
  total_traces: number
  agent_traces: number
  evaluation_traces: number
  by_source: Record<SourceClass, number>
  by_scope_source: {
    agent: Record<string, number>
    evaluation: Record<string, number>
  }
  status_by_source: Record<string, Record<string, number>>
  fault_traces_by_source: Record<string, number>
  unknown_count: number
  source_classes: SourceClass[]
  definitions: Record<SourceClass, string>
  scope_note: string
  read_only: boolean
  paid_api_called: boolean
}

/** 失败 Trace 口径汇总（GET /api/governance/failed-summary）。 */
export interface FailedSummaryResponse {
  scope: string
  scope_note: string
  total_traces: number
  failed_count: number
  failed_with_fault_injection: number
  failed_by_source: Record<string, number>
  failed_fault_type_counts: Record<string, number>
  samples: {
    trace_id: string
    status: string
    source_class: SourceClass
    step_count: number
    fault_types: string[]
    started_at: string
    detail: string
    eligibility: string
  }[]
  read_only: boolean
  paid_api_called: boolean
}

export interface EligibilityCapability {
  eligible: boolean
  reasons: string[]
  requirements: string[]
}

/** 单条 Trace 的受控实验资格（GET /api/governance/traces/{id}/eligibility）。 */
export interface TraceEligibility {
  trace_id: string
  status: string
  step_count: number
  tool_step_count: number
  fault_events: { step_id: string; tool_name: string | null; fault_type: string | null }[]
  capabilities: {
    recorded_replay: EligibilityCapability
    controlled_rerun: EligibilityCapability
    counterfactual_experiment: EligibilityCapability
    repair_candidates: EligibilityCapability
  }
  source_class: SourceClass
  read_only: boolean
  note: string
}

export interface TraceClassificationResponse {
  classification: {
    trace_id: string
    source_class: SourceClass
    reasons: string[]
    evidence: string[]
    source_definitions: Record<string, string>
    definitions_note: string
    data_source?: string | null
    simulated?: boolean
    derivation?: string | null
  }
  read_only: boolean
  paid_api_called: boolean
}

export interface TraceError {
  error_type: string
  error_message: string
  recoverable: boolean
}

export interface TokenUsage {
  input_tokens: number
  output_tokens: number
  total_tokens: number
}

export interface TraceStep {
  step_id: string
  trace_id: string
  parent_step_ids: string[]
  step_type: string
  timestamp: string
  input?: unknown
  output?: unknown
  tool_name?: string | null
  tool_arguments?: Record<string, unknown> | null
  tool_result?: unknown
  status: string
  error?: TraceError | null
  duration_ms?: number | null
  token_usage?: TokenUsage | null
  metadata: Record<string, unknown>
}

export interface TraceDetail {
  trace_id: string
  task_id: string
  run_id: string
  status: string
  started_at: string
  ended_at: string | null
  steps: TraceStep[]
  metadata: Record<string, unknown>
}

export interface TraceResponse {
  trace: TraceDetail
}

export interface DagNode {
  step_id: string
  step_type: string
  status: string
  parent_step_ids: string[]
  child_step_ids: string[]
}

export interface Dag {
  nodes: DagNode[]
  edges: { parent_id: string; child_id: string }[]
  topological_order: string[]
  root_nodes: string[]
  leaf_nodes: string[]
}

export interface DagResponse {
  trace_id: string
  dag: Dag
}

export interface PropagationResponse {
  trace_id: string
  propagation: {
    nodes: unknown[]
    edges: unknown[]
    paths: unknown[]
    summary: Record<string, unknown>
    note: string
  }
}

export interface CompareResponse {
  reference_trace_id: string
  target_trace_id: string
  comparison: Record<string, unknown>
}

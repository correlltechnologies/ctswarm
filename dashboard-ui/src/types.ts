export type Build = {
  build_id: string
  goal: string
  repo_url: string
  runtime?: string
  state: string
  execution_id?: string
  phase_detail?: string
  elapsed_s?: number
  pr_url?: string
  error?: string
}

export type TraceNode = {
  execution_id: string
  parent_execution_id: string
  reasoner_id: string
  role: string
  phase: string
  task: string
  status: string
  started_at?: string
  completed_at?: string
  duration_ms?: number
  depth: number
  model: string
  model_source: string
  runtime: string
  harness: string
  provider: string
  requested_model?: string
  resolved_model?: string
  resolved_backend?: string
  resolution?: string
}

export type LiveRoute = {
  alias: string
  backend: string
  model: string
  degraded_to?: string | null
  reason: string
}

export type Trace = {
  execution_id: string
  workflow_id: string
  status: string
  runtime: string
  harness: string
  model_policy: Record<string, string>
  provider_policy?: Record<string, string>
  routes?: Record<string, LiveRoute>
  total_nodes: number
  summary: {
    statuses: Record<string, number>
    roles: Record<string, number>
    models: Record<string, number>
    harnesses: Record<string, number>
  }
  timeline: TraceNode[]
  error?: string
}

export type Approval = {
  dedupe_key: string
  action: string
  rule_name: string
  risk: string
  payload?: Record<string, unknown>
  expired?: boolean
  decision?: { decision: string; decided_by?: string } | null
}

export type ModelMetric = {
  name: string
  executions: number
  active: number
  live_jobs: number
  succeeded: number
  failed: number
  calls: number
  call_successes: number
  tokens: number
  cost_usd: number
  avg_duration_ms: number
  avg_latency_ms: number
  call_success_rate: number | null
  roles: Record<string, number>
  harnesses: string[]
  providers: string[]
  backends: string[]
  build_count: number
  local: boolean
  benchmark?: {
    quality: number
    tool_call_rate: number
    schema_rate: number
    tokens_per_s: number
    p50_latency_ms: number
    eligible: boolean
  } | null
}

export type Capacity = {
  runtime: string
  available: boolean
  fraction_remaining: number
  spent_usd: number
  calls: number
  cooldown_remaining_s: number
  reason: string
}

export type ModelOverview = {
  window_hours: number
  summary: {
    builds: number
    executions: number
    active_executions: number
    local_active_executions: number
    failed_executions: number
    execution_roles: Record<string, number>
    models: number
    router_calls: number
    router_failures: number
    routing_rejections: number
    local_calls: number
    local_fraction: number
    tokens: number
    cost_usd: number
  }
  capacity: Record<string, Capacity>
  models: ModelMetric[]
  routes: Record<string, LiveRoute>
  graph: {
    nodes: Array<{ id: string; label: string; kind: string; local: boolean }>
    edges: Array<{ source: string; target: string; value: number }>
  }
}

export type ExecutionDetail = {
  status?: string
  reasoner_id?: string
  input_data?: Record<string, unknown>
  output_data?: unknown
  notes?: Array<{ tags?: string[]; message?: string; timestamp?: string }>
}

export type InferenceCall = {
  id: number
  ts: number
  build_id?: string | null
  role?: string | null
  tier?: string | null
  virtual_model?: string | null
  backend: string
  model_ref: string
  prompt_tokens: number
  output_tokens: number
  latency_ms: number
  ok: number
  failure_kind?: string | null
  cost_usd: number
  attempt: number
}

export type DashboardSnapshot = {
  sequence: number
  generated_at: number
  builds: Build[]
  build?: Build
  trace?: Trace
  approvals?: Approval[]
  inference_calls: InferenceCall[]
  missing_build?: string
  stream_error?: string
}

export type StreamState = "connecting" | "live" | "reconnecting"

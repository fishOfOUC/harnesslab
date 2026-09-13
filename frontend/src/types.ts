export type RunStatus =
  | 'queued'
  | 'running'
  | 'waiting_approval'
  | 'recovering'
  | 'cancelling'
  | 'completed'
  | 'failed'
  | 'cancelled'

export interface Project {
  id: string
  name: string
  owner_id: string
  policy_version: string
  created_at: string
}

export interface Thread {
  id: string
  project_id: string
  title: string
  active_run_id: string | null
  revision: number
  updated_at: string
}

export interface Citation {
  label: string
  chunk_id: string
  document_id: string
  document_version: number
  source_title: string
  heading_path?: string
  page?: number | null
  char_start?: number | null
  char_end?: number | null
  snippet?: string
  score?: number
}

export interface Artifact {
  id: string
  title: string
  relative_path: string
  media_type: string
  size: number
  download_url?: string
}

export interface PlanStep {
  id: string
  title: string
  status: 'pending' | 'in_progress' | 'done' | 'failed' | 'skipped'
  note?: string
}

export interface RunResult {
  answer: string
  citations: string[]
  citations_resolved: Citation[]
  artifacts: Artifact[]
  limitations: string[]
  task_status: 'completed' | 'partial' | 'needs_input'
  output_validated: boolean
  plan?: { version: number; goal: string; steps: PlanStep[] }
  usage?: Record<string, unknown>
  index_version?: string | null
  elapsed_ms?: number
  cancelled_reason?: string
  note?: string
}

export interface Run {
  id: string
  thread_id: string
  project_id: string
  status: RunStatus
  mode: string
  user_message: string
  error_code?: string | null
  error_message?: string | null
  result?: RunResult | null
  budget_limits: Record<string, number>
  budget_usage: Record<string, number>
  config_snapshot?: Record<string, unknown>
  parent_run_id?: string | null
  created_at: string
  updated_at: string
  started_at?: string | null
  finished_at?: string | null
}

export interface Approval {
  id: string
  run_id: string
  kind: string
  tool_name: string
  arguments: Record<string, unknown>
  arguments_hash: string
  expected_effect: string
  status: 'pending' | 'approved' | 'rejected' | 'expired' | 'superseded' | 'cancelled'
  revision: number
  expires_at: string
}

export interface RunEvent {
  seq: number
  event_id: string
  type: string
  timestamp: string
  payload: Record<string, unknown>
}

export interface ToolOperation {
  id: string
  tool_name: string
  tool_version: string
  status: string
  idempotency_key: string
  external_receipt?: string | null
  error_code?: string | null
  request: Record<string, unknown>
  result?: Record<string, unknown> | null
}

export interface RunDetail {
  run: Run
  events_url: string
  pending_approval: Approval | null
  artifacts: Artifact[]
  operations: ToolOperation[]
  snapshot: Record<string, unknown>
  last_seq: number
}

export interface DocumentVersion {
  version: number
  parse_status: string
  chunk_count: number
  char_count: number
  index_version?: string | null
  error_code?: string | null
  error_message?: string | null
  created_at: string
}

export interface DocumentItem {
  id: string
  title: string
  media_type: string
  active_version: number | null
  deleted_at: string | null
  created_at: string
  versions: DocumentVersion[]
}

export interface IndexStatus {
  active_index_version: string | null
  dimension: number | null
  chunk_count: number
  document_count: number
  pending_documents: { document_id: string; title: string; status: string }[]
}

export interface RetrievalHit {
  chunk_id: string
  source_title: string
  document_version: number
  heading_path: string
  page?: number | null
  score: number
  text: string
  vector_rank?: number | null
  sparse_rank?: number | null
}

export interface ModelProfile {
  id: string
  role: string
  provider: string
  model: string
  base_url?: string
  dimension?: string
  capabilities: Record<string, boolean>
  note?: string
  api_key_configured?: boolean
}

export interface HealthReady {
  status: string
  checks: Record<string, Record<string, unknown>>
}

export interface EvalRun {
  eval_id: string
  status: string
  error_code?: string | null
  metrics?: Record<string, unknown> | null
  environment?: Record<string, unknown> | null
  case_results?: Record<string, unknown>[]
  dataset_version?: string | null
  created_at: string
}

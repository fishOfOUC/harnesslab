import type {
  Approval,
  DocumentItem,
  EvalRun,
  HealthReady,
  IndexStatus,
  ModelProfile,
  Project,
  RetrievalHit,
  Run,
  RunDetail,
  RunEvent,
  Thread,
} from './types'

const PREFIX = '/api/v1'

export interface ApiErrorShape {
  code: string
  message: string
  retryable: boolean
  request_id?: string | null
  details?: Record<string, unknown>
}

export class ApiError extends Error {
  code: string
  retryable: boolean
  details: Record<string, unknown>

  constructor(payload: ApiErrorShape) {
    super(payload.message)
    this.code = payload.code
    this.retryable = payload.retryable
    this.details = payload.details ?? {}
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${PREFIX}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...(init?.headers ?? {}),
    },
  })
  const text = await response.text()
  const payload = text ? JSON.parse(text) : null
  if (!response.ok) {
    const error = payload?.error ?? {
      code: 'UNKNOWN',
      message: `请求失败（HTTP ${response.status}）`,
      retryable: false,
    }
    throw new ApiError(error as ApiErrorShape)
  }
  return payload as T
}

export const api = {
  health: () => request<HealthReady>('/health/ready'),
  modelProfiles: () => request<{ profiles: ModelProfile[]; note: string }>('/model-profiles'),
  probeProfile: (id: string) =>
    request<{ profile_id: string; result: Record<string, unknown> }>(`/model-profiles/${id}/probe`, {
      method: 'POST',
    }),
  policy: () =>
    request<{ policies: { name: string; version: string; risk: string; decision: string; reason: string }[] }>(
      '/policy',
    ),

  projects: () => request<{ projects: Project[] }>('/projects'),
  createProject: (name: string) =>
    request<Project>('/projects', { method: 'POST', body: JSON.stringify({ name }) }),

  documents: (projectId: string) =>
    request<{ documents: DocumentItem[]; index: IndexStatus }>(`/projects/${projectId}/documents`),
  upload: async (projectId: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<{ job_id: string; status: string; note: string }>(
      `/projects/${projectId}/documents`,
      { method: 'POST', body: form },
    )
  },
  deleteDocument: (projectId: string, documentId: string) =>
    request<{ document_id: string; status: string }>(
      `/projects/${projectId}/documents/${documentId}`,
      { method: 'DELETE' },
    ),
  seedDemo: (projectId: string) =>
    request<{ queued: { file: string; job_id: string }[]; already_indexed: string[]; note: string }>(
      `/projects/${projectId}/demo/seed`,
      { method: 'POST' },
    ),
  rebuildIndex: (projectId: string) =>
    request<{ job_id: string; note: string }>(`/projects/${projectId}/index/rebuild`, { method: 'POST' }),
  job: (jobId: string) =>
    request<{
      id: string
      kind: string
      status: string
      result?: Record<string, unknown> | null
      error_code?: string | null
      error_message?: string | null
      payload?: Record<string, unknown>
    }>(`/jobs/${jobId}`),
  preview: (projectId: string, query: string, topK = 8, mode?: string) =>
    request<{
      mode: string
      index_version: string
      insufficient_evidence: boolean
      note: string
      candidates: Record<string, unknown>[]
      hits: RetrievalHit[]
    }>(`/projects/${projectId}/retrieval/preview`, {
      method: 'POST',
      body: JSON.stringify({ query, top_k: topK, mode: mode ?? null }),
    }),

  threads: (projectId: string) => request<{ threads: Thread[] }>(`/projects/${projectId}/threads`),
  createThread: (projectId: string, title = '') =>
    request<Thread>(`/projects/${projectId}/threads`, { method: 'POST', body: JSON.stringify({ title }) }),
  threadRuns: (threadId: string) =>
    request<{ thread: Thread; runs: Run[] }>(`/threads/${threadId}`),

  createRun: (
    threadId: string,
    message: string,
    options?: { idempotencyKey?: string; limits?: Record<string, number>; mode?: string },
  ) =>
    request<{ run_id: string; status: string; created: boolean; events_url: string }>(
      `/threads/${threadId}/runs`,
      {
        method: 'POST',
        headers: options?.idempotencyKey ? { 'Idempotency-Key': options.idempotencyKey } : {},
        body: JSON.stringify({
          message,
          mode: options?.mode ?? 'research',
          limits: options?.limits ?? undefined,
        }),
      },
    ),
  run: (runId: string) => request<RunDetail>(`/runs/${runId}`),
  cancelRun: (runId: string) =>
    request<{ run_id: string; status: string; note: string }>(`/runs/${runId}/cancel`, { method: 'POST' }),
  retryRun: (runId: string) =>
    request<{ run_id: string; parent_run_id: string; note: string }>(`/runs/${runId}/retry`, { method: 'POST' }),
  forkRun: (runId: string) =>
    request<{ run_id: string; thread_id: string; note: string }>(`/runs/${runId}/fork`, { method: 'POST' }),
  timeline: (runId: string) =>
    request<{
      events: RunEvent[]
      operations: Record<string, unknown>[]
      approvals: Approval[]
      artifacts: Record<string, unknown>[]
    }>(`/runs/${runId}/timeline`),
  events: (runId: string, after = 0) =>
    request<{ events: RunEvent[]; last_seq: number }>(`/runs/${runId}/events?stream=false&after=${after}`),

  approvals: (runId: string) => request<{ approvals: Approval[] }>(`/runs/${runId}/approvals`),
  projectApprovals: (projectId: string, status = 'pending') =>
    request<{ approvals: Approval[] }>(`/projects/${projectId}/approvals?status=${status}`),
  decide: (approvalId: string, decision: 'approve' | 'reject', revision: number, hash: string, comment = '') =>
    request<{ approval_id: string; status: string; run_status: string; note: string }>(
      `/approvals/${approvalId}/decision`,
      {
        method: 'POST',
        body: JSON.stringify({
          decision,
          expected_revision: revision,
          arguments_hash: hash,
          comment,
        }),
      },
    ),
  revise: (approvalId: string, args: Record<string, unknown>) =>
    request<Approval>(`/approvals/${approvalId}/revision`, {
      method: 'POST',
      body: JSON.stringify({ arguments: args }),
    }),

  source: (chunkId: string) =>
    request<{
      chunk_id: string
      source_title: string
      heading_path: string
      page?: number | null
      char_start?: number | null
      char_end?: number | null
      text: string
      document_version: number
    }>(`/sources/${chunkId}`),

  evalRuns: (projectId: string, mode?: string) =>
    request<{ eval_id: string; status: string; note: string }>(`/projects/${projectId}/eval-runs`, {
      method: 'POST',
      body: JSON.stringify({ mode: mode ?? null }),
    }),
  evalRun: (evalId: string) => request<EvalRun>(`/eval-runs/${evalId}`),

  memories: (projectId: string) =>
    request<{ memories: Record<string, unknown>[]; note: string }>(`/projects/${projectId}/memories`),
}

/** 用 fetch 读取 SSE：浏览器原生 EventSource 无法携带自定义头，这里统一走受控同源请求。 */
export async function streamRunEvents(
  runId: string,
  lastEventId: number,
  onEvent: (event: RunEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch(`${PREFIX}/runs/${runId}/events`, {
    headers: { Accept: 'text/event-stream', 'Last-Event-ID': String(lastEventId) },
    signal,
  })
  if (!response.ok || !response.body) {
    throw new ApiError({
      code: 'STREAM_FAILED',
      message: `事件流建立失败（HTTP ${response.status}）`,
      retryable: true,
    })
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const raw = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const parsed = parseSseBlock(raw)
      if (parsed) onEvent(parsed)
      boundary = buffer.indexOf('\n\n')
    }
  }
}

function parseSseBlock(block: string): RunEvent | null {
  const lines = block.split('\n')
  let data = ''
  let eventType = ''
  let id = 0
  for (const line of lines) {
    if (line.startsWith(':')) continue
    if (line.startsWith('id:')) id = Number(line.slice(3).trim())
    else if (line.startsWith('event:')) eventType = line.slice(6).trim()
    else if (line.startsWith('data:')) data += line.slice(5).trim()
  }
  if (!data || !eventType || eventType === 'stream.end') return null
  try {
    const payload = JSON.parse(data)
    return { ...payload, seq: payload.seq ?? id } as RunEvent
  } catch {
    return null
  }
}

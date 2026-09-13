import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, streamRunEvents } from '../api'
import type { Approval, Run, RunDetail, RunEvent } from '../types'
import { StatusPill } from '../components/StatusPill'
import { PlanList } from '../components/PlanList'
import { CitationList } from '../components/CitationList'
import { ApprovalCard } from '../components/ApprovalCard'
import { RunDetailPanel } from '../components/RunDetailPanel'

const DEMO_PROMPTS = [
  '比较知识库中 gateway v1 与 v2 的方案差异，引用来源并说明冲突点',
  '按 capacity-notes 明确给出的输入计算单实例理论吞吐与所需实例数，列出假设',
  '资料中是否保证极端负载下零数据丢失？',
  '生成选型报告，然后申请创建一个本地演示工单',
]

export default function Workbench({
  projectId,
  activeThreadId,
  onThreadCreated,
  notify,
  onRunChanged,
}: {
  projectId: string
  activeThreadId: string
  onThreadCreated: (threadId: string) => Promise<void>
  notify: (message: string, tone?: 'info' | 'error' | 'success') => void
  onRunChanged: () => void
}) {
  const [runs, setRuns] = useState<Run[]>([])
  const [activeRunId, setActiveRunId] = useState('')
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [streaming, setStreaming] = useState(false)
  const [message, setMessage] = useState('')
  const [mode, setMode] = useState('research')
  const [toolLimit, setToolLimit] = useState<number | ''>('')
  const [pending, setPending] = useState<Approval | null>(null)
  const [busy, setBusy] = useState('')
  const abortRef = useRef<AbortController | null>(null)

  const loadRuns = useCallback(async () => {
    if (!activeThreadId) {
      setRuns([])
      setActiveRunId('')
      return
    }
    try {
      const data = await api.threadRuns(activeThreadId)
      setRuns(data.runs)
      setActiveRunId((current) => {
        if (data.runs.some((run) => run.id === current)) return current
        return data.runs[0]?.id ?? ''
      })
    } catch (error) {
      notify(error instanceof ApiError ? error.message : '会话读取失败', 'error')
    }
  }, [activeThreadId, notify])

  useEffect(() => {
    loadRuns()
  }, [loadRuns])

  const refreshDetail = useCallback(async () => {
    if (!activeRunId) {
      setDetail(null)
      setEvents([])
      return
    }
    try {
      const [data, timeline] = await Promise.all([api.run(activeRunId), api.timeline(activeRunId)])
      setDetail(data)
      setPending(data.pending_approval)
      setEvents(timeline.events as unknown as RunEvent[])
    } catch (error) {
      notify(error instanceof ApiError ? error.message : '运行详情读取失败', 'error')
    }
  }, [activeRunId, notify])

  useEffect(() => {
    refreshDetail()
  }, [refreshDetail])

  // SSE：先补齐已有事件，再按 seq 去重追加；终态后自动停止
  useEffect(() => {
    if (!activeRunId || !detail) return
    const terminal = ['completed', 'failed', 'cancelled'].includes(detail.run.status)
    abortRef.current?.abort()
    if (terminal) {
      setStreaming(false)
      return
    }
    const controller = new AbortController()
    abortRef.current = controller
    setStreaming(true)
    const lastSeq = events.reduce((max, event) => Math.max(max, event.seq), 0)
    streamRunEvents(
      activeRunId,
      lastSeq,
      (event) => {
        setEvents((prev) => (prev.some((item) => item.seq === event.seq) ? prev : [...prev, event]))
      },
      controller.signal,
    )
      .catch(() => setStreaming(false))
      .finally(() => setStreaming(false))
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRunId, detail?.run.status])

  // 运行状态变化后刷新详情（等待审批、完成、失败等）
  useEffect(() => {
    if (!activeRunId) return
    const timer = window.setInterval(async () => {
      if (!detail) return
      if (['completed', 'failed', 'cancelled'].includes(detail.run.status)) return
      await refreshDetail()
      onRunChanged()
    }, 2500)
    return () => window.clearInterval(timer)
  }, [activeRunId, detail, refreshDetail, onRunChanged])

  async function submit() {
    const text = message.trim()
    if (!text) {
      notify('请输入问题', 'error')
      return
    }
    setBusy('submit')
    try {
      let threadId = activeThreadId
      if (!threadId) {
        const thread = await api.createThread(projectId, text.slice(0, 40))
        threadId = thread.id
        await onThreadCreated(threadId)
      }
      const limits: Record<string, number> = {}
      if (toolLimit !== '' && Number(toolLimit) > 0) limits.max_tool_calls = Number(toolLimit)
      const created = await api.createRun(threadId, text, {
        mode,
        idempotencyKey: `web-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        limits: Object.keys(limits).length ? limits : undefined,
      })
      setMessage('')
      await loadRuns()
      setActiveRunId(created.run_id)
      notify('已提交任务，工作进程会按其状态推进', 'success')
      onRunChanged()
    } catch (error) {
      notify(error instanceof ApiError ? error.message : '提交失败', 'error')
    } finally {
      setBusy('')
    }
  }

  async function cancel() {
    if (!activeRunId) return
    setBusy('cancel')
    try {
      const result = await api.cancelRun(activeRunId)
      notify(`已请求取消，当前状态：${result.status}。${result.note}`, 'info')
      await refreshDetail()
      await loadRuns()
      onRunChanged()
    } catch (error) {
      notify(error instanceof ApiError ? error.message : '取消失败', 'error')
    } finally {
      setBusy('')
    }
  }

  async function retry() {
    if (!activeRunId) return
    setBusy('retry')
    try {
      const result = await api.retryRun(activeRunId)
      notify(`已创建重跑运行 ${result.run_id.slice(0, 8)}（原终态不变）`, 'success')
      await loadRuns()
      setActiveRunId(result.run_id)
    } catch (error) {
      notify(error instanceof ApiError ? error.message : '重跑失败', 'error')
    } finally {
      setBusy('')
    }
  }

  async function fork() {
    if (!activeRunId) return
    setBusy('fork')
    try {
      const result = await api.forkRun(activeRunId)
      notify(`已分叉到新会话 ${result.thread_id.slice(0, 8)}；副作用需重新审批`, 'success')
      await onThreadCreated(result.thread_id)
      await loadRuns()
      setActiveRunId(result.run_id)
    } catch (error) {
      notify(error instanceof ApiError ? error.message : '分叉失败', 'error')
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="workbench">
      <div className="workbench-main">
        <div className="page-head" style={{ marginBottom: 8 }}>
          <div>
            <h1>任务工作台</h1>
            <p>左侧选择会话，中间查看答案与证据，右侧是执行详情、用量与审批。</p>
          </div>
          {runs.length > 1 ? (
            <select
              aria-label="选择运行"
              value={activeRunId}
              onChange={(event) => setActiveRunId(event.target.value)}
              style={{ maxWidth: 220 }}
            >
              {runs.map((run) => (
                <option key={run.id} value={run.id}>
                  {run.id.slice(0, 8)} · {run.status}
                </option>
              ))}
            </select>
          ) : null}
        </div>

        {runs.length === 0 ? (
          <div className="empty">这个会话还没有运行记录。在下方输入问题即可开始。</div>
        ) : null}

        <div className="messages">
          {[...runs].reverse().map((run) => {
            const isActive = run.id === activeRunId
            const result = run.result
            return (
              <article key={run.id} className="message" aria-current={isActive}>
                <div className="message-head">
                  <span className="faint mono">{run.id.slice(0, 8)}</span>
                  <span className="row">
                    <StatusPill status={run.status} />
                    {isActive ? <span className="pill accent">当前</span> : null}
                    {!isActive ? (
                      <button className="ghost" onClick={() => setActiveRunId(run.id)}>
                        查看详情
                      </button>
                    ) : null}
                  </span>
                </div>

                <div className="message user">
                  <div className="message-head">
                    <strong>用户</strong>
                    <span className="faint">{new Date(run.created_at ?? Date.now()).toLocaleString()}</span>
                  </div>
                  <div className="answer">{run.user_message}</div>
                </div>

                <div style={{ marginTop: 10 }}>
                  <div className="message-head">
                    <strong>助手</strong>
                    {result ? (
                      <span className="faint">
                        {result.task_status} · {result.elapsed_ms ?? 0} ms · 索引{' '}
                        {(result.index_version ?? '无').slice(0, 8)}
                      </span>
                    ) : (
                      <span className="faint">尚未产生结果</span>
                    )}
                  </div>

                  {result ? (
                    <>
                      <div className="answer">{result.answer}</div>
                      {result.limitations?.length ? (
                        <ul className="limitations">
                          {result.limitations.map((item) => (
                            <li key={item}>{item}</li>
                          ))}
                        </ul>
                      ) : null}
                      {result.plan?.steps?.length ? (
                        <details style={{ marginTop: 10 }}>
                          <summary>执行计划（{result.plan.steps.length} 步）</summary>
                          <PlanList steps={result.plan.steps} />
                        </details>
                      ) : null}
                      <div style={{ marginTop: 10 }}>
                        <div className="faint" style={{ marginBottom: 6 }}>
                          证据引用（点击展开固定版本原文）
                        </div>
                        <CitationList citations={result.citations_resolved ?? []} onError={(m) => notify(m, 'error')} />
                      </div>
                      {result.artifacts?.length ? (
                        <div style={{ marginTop: 10 }}>
                          <div className="faint" style={{ marginBottom: 6 }}>
                            产物
                          </div>
                          <div className="row">
                            {result.artifacts.map((artifact) => (
                              <a
                                key={artifact.id}
                                className="pill"
                                href={artifact.download_url ?? `/api/v1/artifacts/${artifact.id}/download`}
                                download
                              >
                                {artifact.title}（{artifact.size} 字节）
                              </a>
                            ))}
                          </div>
                        </div>
                      ) : null}
                    </>
                  ) : (
                    <p className="muted">
                      {run.status === 'waiting_approval'
                        ? '运行停在审批前，批准后会继续执行。'
                        : '运行进行中，事件会实时补齐。'}
                    </p>
                  )}
                </div>
              </article>
            )
          })}
        </div>

        <div className="card">
          <h3>新建提问</h3>
          <textarea
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            placeholder="例如：请比较 gateway v1 和 v2，引用来源说明差异及冲突；使用 capacity-notes 中明确给出的输入完成计算，列出假设。"
          />
          <div className="row" style={{ marginTop: 10 }}>
            <label className="faint">
              模式
              <select value={mode} onChange={(event) => setMode(event.target.value)} style={{ width: 130 }}>
                <option value="research">research</option>
                <option value="chat">chat（跳过计划）</option>
              </select>
            </label>
            <label className="faint">
              工具调用上限（只能降低）
              <input
                type="text"
                inputMode="numeric"
                value={toolLimit}
                onChange={(event) =>
                  setToolLimit(event.target.value === '' ? '' : Number(event.target.value.replace(/\D/g, '')))
                }
                placeholder="默认 30"
                style={{ width: 120 }}
              />
            </label>
            <button className="primary" onClick={submit} disabled={busy === 'submit'}>
              提交任务
            </button>
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            {DEMO_PROMPTS.map((prompt) => (
              <button key={prompt} className="ghost" onClick={() => setMessage(prompt)}>
                {prompt.slice(0, 18)}…
              </button>
            ))}
          </div>
        </div>
      </div>

      <aside className="workbench-side">
        {!detail ? (
          <div className="empty">选择一次运行后，这里显示时间线、用量、账本与审批。</div>
        ) : (
          <>
            {pending ? (
              <ApprovalCard approval={pending} onDone={refreshDetail} notify={notify} />
            ) : null}

            <div className="card">
              <h3>运行操作</h3>
              <div className="row">
                <button onClick={cancel} disabled={busy === 'cancel' || ['completed', 'failed', 'cancelled'].includes(detail.run.status)}>
                  取消
                </button>
                <button onClick={retry} disabled={busy === 'retry' || !['completed', 'failed', 'cancelled'].includes(detail.run.status)}>
                  重跑（新 run）
                </button>
                <button onClick={fork} disabled={busy === 'fork'}>
                  从检查点分叉
                </button>
              </div>
              <p className="faint" style={{ marginTop: 8 }}>
                取消保留已有答案与产物；重跑创建新 run；分叉需重新审批副作用。三者互不等价。
              </p>
            </div>

            <RunDetailPanel detail={detail} events={events} streaming={streaming} />
          </>
        )}
      </aside>
    </div>
  )
}

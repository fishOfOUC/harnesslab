import type { RunDetail, RunEvent } from '../types'
import { StatusPill } from './StatusPill'

const EVENT_LABELS: Record<string, string> = {
  'run.queued': '任务入队',
  'run.started': '开始执行',
  'plan.updated': '计划更新',
  'message.delta': '增量输出',
  'message.completed': '消息完成',
  'retrieval.completed': '检索完成',
  'tool.started': '工具开始',
  'tool.completed': '工具结束',
  'approval.requested': '请求审批',
  'approval.decided': '审批决策',
  'artifact.created': '生成产物',
  'budget.updated': '预算更新',
  'run.recovering': '故障恢复',
  'run.completed': '运行完成',
  'run.failed': '运行失败',
  'run.cancelled': '运行取消',
}

function summarize(type: string, payload: Record<string, unknown>): string {
  if (type === 'tool.completed' || type === 'tool.started') {
    const name = payload.name ?? '未知工具'
    const extra = payload.ok === false ? `失败：${payload.error_code ?? ''}` : `${payload.duration_ms ?? '?'} ms`
    return `${name} · ${extra}`
  }
  if (type === 'approval.requested') return `${payload.tool_name ?? ''} · 版本 v${payload.revision ?? 1}`
  if (type === 'artifact.created') return `${payload.title ?? ''}（${payload.size ?? 0} 字节）`
  if (type === 'budget.updated') {
    const usage = payload.usage as Record<string, number> | undefined
    return usage
      ? `模型 ${usage.model_calls ?? 0} 次 · 工具 ${usage.tool_calls ?? 0} 次 · ${usage.elapsed_seconds ?? 0}s`
      : ''
  }
  if (type === 'run.failed') return String(payload.message ?? '')
  if (type === 'run.cancelled') return String(payload.reason ?? '')
  if (type === 'plan.updated') {
    const steps = payload.steps as unknown[] | undefined
    return steps ? `${steps.length} 个步骤（v${payload.version ?? 1}）` : ''
  }
  if (type === 'message.completed') return String(payload.task_status ?? '')
  return ''
}

export function RunDetailPanel({
  detail,
  events,
  streaming,
}: {
  detail: RunDetail
  events: RunEvent[]
  streaming: boolean
}) {
  const run = detail.run
  const usage = run.budget_usage ?? {}
  const limits = run.budget_limits ?? {}

  const modelRatio = ratio(usage.model_calls, limits.max_model_calls)
  const toolRatio = ratio(usage.tool_calls, limits.max_tool_calls)

  return (
    <>
      <div className="card">
        <div className="between">
          <h3 style={{ margin: 0 }}>运行详情</h3>
          <StatusPill status={run.status} />
        </div>
        <p className="faint mono" style={{ marginTop: 6 }}>{run.id}</p>
        {run.error_code ? (
          <p className="limitations">
            {run.error_code}：{run.error_message}
          </p>
        ) : null}
        <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div>
            <div className="faint between">
              <span>模型调用</span>
              <span>
                {String(usage.model_calls ?? 0)} / {String(limits.max_model_calls ?? '?')}
              </span>
            </div>
            <div className="bar">
              <span style={{ width: `${modelRatio}%` }} />
            </div>
          </div>
          <div>
            <div className="faint between">
              <span>工具调用</span>
              <span>
                {String(usage.tool_calls ?? 0)} / {String(limits.max_tool_calls ?? '?')}
              </span>
            </div>
            <div className="bar">
              <span style={{ width: `${toolRatio}%` }} />
            </div>
          </div>
          <p className="faint">
            费用：未知（未配置价格）｜ token 为估算值 ｜ 活跃时长上限 {String(limits.active_timeout_seconds ?? '?')} 秒
          </p>
        </div>
      </div>

      <div className="card">
        <div className="between">
          <h3 style={{ margin: 0 }}>执行时间线</h3>
          {streaming ? <span className="pill accent">实时</span> : <span className="pill">静态</span>}
        </div>
        <div className="timeline">
          {events.length === 0 ? <span className="muted">暂无事件</span> : null}
          {events.map((event) => (
            <div className="timeline-item" key={`${event.seq}`}>
              <span className="seq">#{event.seq}</span>
              <div>
                <div>{EVENT_LABELS[event.type] ?? event.type}</div>
                <div className="faint">{summarize(event.type, event.payload)}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <h3>工具调用与幂等账本</h3>
        {detail.operations.length === 0 ? (
          <p className="muted">本次运行没有工具调用。</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>工具</th>
                <th>状态</th>
                <th>外部回执</th>
              </tr>
            </thead>
            <tbody>
              {detail.operations.map((operation) => (
                <tr key={operation.id}>
                  <td>
                    {operation.tool_name}
                    <div className="faint mono">v{operation.tool_version}</div>
                  </td>
                  <td>
                    {operation.status}
                    {operation.error_code ? <div className="faint">{operation.error_code}</div> : null}
                  </td>
                  <td className="mono">{operation.external_receipt ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <h3>配置快照（用于复现）</h3>
        <details>
          <summary>展开查看模型、索引、Prompt、技能与工具版本</summary>
          <pre>{JSON.stringify(detail.snapshot, null, 2)}</pre>
        </details>
      </div>
    </>
  )
}

function ratio(used?: number, limit?: number): number {
  if (!limit || !used) return 0
  return Math.min(100, Math.round((used / limit) * 100))
}

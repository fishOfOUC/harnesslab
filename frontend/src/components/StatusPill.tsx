import type { RunStatus } from '../types'

const LABELS: Record<RunStatus, { text: string; tone: string }> = {
  queued: { text: '排队中', tone: 'info' },
  running: { text: '执行中', tone: 'accent' },
  waiting_approval: { text: '等待审批', tone: 'warn' },
  recovering: { text: '恢复中', tone: 'warn' },
  cancelling: { text: '取消中', tone: 'warn' },
  completed: { text: '已完成', tone: 'ok' },
  failed: { text: '失败', tone: 'danger' },
  cancelled: { text: '已取消', tone: 'danger' },
}

export function StatusPill({ status }: { status: RunStatus | string }) {
  const meta = LABELS[status as RunStatus] ?? { text: status, tone: '' }
  return (
    <span className={`pill ${meta.tone}`} title={`状态：${meta.text}`}>
      {meta.text}
    </span>
  )
}

export function TextPill({ text, tone = '' }: { text: string; tone?: string }) {
  return <span className={`pill ${tone}`}>{text}</span>
}

import type { PlanStep } from '../types'

const MARKS: Record<PlanStep['status'], string> = {
  pending: '[ ]',
  in_progress: '[~]',
  done: '[x]',
  failed: '[!]',
  skipped: '[-]',
}

const STATUS_TEXT: Record<PlanStep['status'], string> = {
  pending: '待执行',
  in_progress: '进行中',
  done: '已完成',
  failed: '失败',
  skipped: '已跳过',
}

export function PlanList({ steps }: { steps: PlanStep[] }) {
  if (!steps.length) {
    return <p className="muted">本次运行没有生成计划（短任务直接进入 Agent）。</p>
  }
  return (
    <ul className="plan">
      {steps.map((step) => (
        <li key={step.id} className={step.status}>
          <span className="mark" aria-hidden="true">
            {MARKS[step.status]}
          </span>
          <span>
            {step.title}
            <span className="faint"> · {STATUS_TEXT[step.status]}</span>
            {step.note ? <span className="faint"> · {step.note}</span> : null}
          </span>
        </li>
      ))}
    </ul>
  )
}

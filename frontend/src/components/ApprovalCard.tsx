import { useState } from 'react'
import { api, ApiError } from '../api'
import type { Approval } from '../types'

/** 审批卡：展示真实规范化参数与预期影响；修改参数会生成新的待审批版本。 */
export function ApprovalCard({
  approval,
  onDone,
  notify,
}: {
  approval: Approval
  onDone: () => void
  notify: (message: string, tone?: 'info' | 'error' | 'success') => void
}) {
  const [editing, setEditing] = useState(false)
  const [json, setJson] = useState(() => JSON.stringify(approval.arguments, null, 2))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function decide(decision: 'approve' | 'reject') {
    setBusy(true)
    setError('')
    try {
      const result = await api.decide(approval.id, decision, approval.revision, approval.arguments_hash)
      notify(
        decision === 'approve'
          ? `已批准该参数版本，运行状态：${result.run_status}`
          : '已拒绝该操作，运行将按拒绝结果继续',
        'success',
      )
      onDone()
    } catch (err) {
      const message = err instanceof ApiError ? err.message : '审批提交失败'
      setError(message)
      notify(message, 'error')
    } finally {
      setBusy(false)
    }
  }

  async function submitRevision() {
    setBusy(true)
    setError('')
    try {
      const parsed = JSON.parse(json)
      const revised = await api.revise(approval.id, parsed)
      notify(`已生成新的待审批版本 v${revised.revision}，旧批准已失效`, 'success')
      setEditing(false)
      onDone()
    } catch (err) {
      const message =
        err instanceof SyntaxError ? '参数不是合法 JSON' : err instanceof ApiError ? err.message : '修订失败'
      setError(message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card">
      <div className="between">
        <h3 style={{ margin: 0 }}>
          {approval.kind === 'manual_reconcile' ? '人工核对' : '待审批操作'} · {approval.tool_name}
        </h3>
        <span className="pill warn">v{approval.revision}</span>
      </div>
      <p className="muted" style={{ marginTop: 8 }}>{approval.expected_effect}</p>
      <p className="faint">
        有效期至 {new Date(approval.expires_at).toLocaleString()} ｜ 参数哈希{' '}
        <span className="mono">{approval.arguments_hash.slice(0, 18)}…</span>
      </p>

      {editing ? (
        <>
          <label className="faint" htmlFor={`args-${approval.id}`}>
            修改规范化参数（提交后旧批准立即失效）
          </label>
          <textarea
            id={`args-${approval.id}`}
            value={json}
            onChange={(event) => setJson(event.target.value)}
            spellCheck={false}
          />
          <div className="row" style={{ marginTop: 8 }}>
            <button className="primary" onClick={submitRevision} disabled={busy}>
              提交新版本
            </button>
            <button className="ghost" onClick={() => setEditing(false)} disabled={busy}>
              取消
            </button>
          </div>
        </>
      ) : (
        <>
          <pre>{JSON.stringify(approval.arguments, null, 2)}</pre>
          <div className="row" style={{ marginTop: 10 }}>
            <button className="primary" onClick={() => decide('approve')} disabled={busy}>
              批准并继续
            </button>
            <button className="danger" onClick={() => decide('reject')} disabled={busy}>
              拒绝
            </button>
            <button className="ghost" onClick={() => setEditing(true)} disabled={busy}>
              修改参数
            </button>
          </div>
        </>
      )}
      {error ? <p className="limitations">{error}</p> : null}
    </div>
  )
}

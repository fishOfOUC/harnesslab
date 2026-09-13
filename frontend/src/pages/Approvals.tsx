import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import type { Approval } from '../types'
import { ApprovalCard } from '../components/ApprovalCard'

export default function Approvals({
  projectId,
  notify,
  onHandled,
}: {
  projectId: string
  notify: (message: string, tone?: 'info' | 'error' | 'success') => void
  onHandled: () => Promise<void>
}) {
  const [approvals, setApprovals] = useState<Approval[]>([])
  const [status, setStatus] = useState('pending')

  const load = useCallback(async () => {
    try {
      const data = await api.projectApprovals(projectId, status)
      setApprovals(data.approvals)
    } catch (error) {
      notify(error instanceof ApiError ? error.message : '审批列表读取失败', 'error')
    }
  }, [projectId, status, notify])

  useEffect(() => {
    load()
    const timer = window.setInterval(load, 5000)
    return () => window.clearInterval(timer)
  }, [load])

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>审批中心</h1>
          <p>
            审批载荷展示真实规范化参数与预期影响，而不是模型生成的摘要。一次批准只绑定该参数版本；
            修改参数会生成新的待审批版本，旧批准立即失效。
          </p>
        </div>
        <div className="row">
          <select value={status} onChange={(event) => setStatus(event.target.value)} style={{ width: 150 }}>
            <option value="pending">待审批</option>
            <option value="approved">已批准</option>
            <option value="rejected">已拒绝</option>
            <option value="expired">已过期</option>
            <option value="superseded">已被替代</option>
            <option value="">全部</option>
          </select>
          <button onClick={load}>刷新</button>
        </div>
      </div>

      {approvals.length === 0 ? (
        <div className="empty">
          当前没有该状态的审批项。运行中遇到需要审批的操作时会在这里出现，并保留独立过期时间。
        </div>
      ) : (
        <div className="grid">
          {approvals.map((approval) =>
            approval.status === 'pending' ? (
              <ApprovalCard
                key={approval.id}
                approval={approval}
                notify={notify}
                onDone={async () => {
                  await load()
                  await onHandled()
                }}
              />
            ) : (
              <div className="card" key={approval.id}>
                <div className="between">
                  <h3 style={{ margin: 0 }}>{approval.tool_name}</h3>
                  <span className="pill">{approval.status}</span>
                </div>
                <p className="faint">
                  运行 {approval.run_id.slice(0, 8)} ｜ 版本 v{approval.revision} ｜ 有效期{' '}
                  {new Date(approval.expires_at).toLocaleString()}
                </p>
                <pre>{JSON.stringify(approval.arguments, null, 2)}</pre>
              </div>
            ),
          )}
        </div>
      )}
    </div>
  )
}

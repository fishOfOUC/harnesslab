import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import type { EvalRun } from '../types'

export default function Evaluations({
  projectId,
  notify,
}: {
  projectId: string
  notify: (message: string, tone?: 'info' | 'error' | 'success') => void
}) {
  const [evals, setEvals] = useState<EvalRun[]>([])
  const [mode, setMode] = useState('hybrid')
  const [busy, setBusy] = useState(false)
  const [selected, setSelected] = useState<EvalRun | null>(null)

  const load = useCallback(async () => {
    try {
      const stored = window.localStorage.getItem(`harnesslab-evals-${projectId}`)
      const ids: string[] = stored ? JSON.parse(stored) : []
      const results: EvalRun[] = []
      for (const id of ids) {
        try {
          results.push(await api.evalRun(id))
        } catch {
          /* 忽略已清理的评测 */
        }
      }
      setEvals(results.reverse())
    } catch {
      setEvals([])
    }
  }, [projectId])

  useEffect(() => {
    load()
  }, [load])

  const poll = useCallback(
    async (evalId: string) => {
      for (let attempt = 0; attempt < 120; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000))
        try {
          const result = await api.evalRun(evalId)
          setSelected(result)
          if (result.status === 'succeeded' || result.status === 'failed') {
            await load()
            return
          }
        } catch {
          return
        }
      }
    },
    [load],
  )

  async function start() {
    setBusy(true)
    try {
      const created = await api.evalRuns(projectId, mode)
      const stored = window.localStorage.getItem(`harnesslab-evals-${projectId}`)
      const ids: string[] = stored ? JSON.parse(stored) : []
      window.localStorage.setItem(
        `harnesslab-evals-${projectId}`,
        JSON.stringify([...new Set([...ids, created.eval_id])].slice(-20)),
      )
      notify(`${created.note}（模式：${mode}）`, 'info')
      setEvals((prev) => [
        {
          eval_id: created.eval_id,
          status: created.status,
          created_at: new Date().toISOString(),
        },
        ...prev,
      ])
      poll(created.eval_id)
    } catch (error) {
      notify(error instanceof ApiError ? error.message : '评测启动失败', 'error')
    } finally {
      setBusy(false)
    }
  }

  const metrics = (selected?.metrics ?? {}) as Record<string, unknown>
  const environment = (selected?.environment ?? {}) as Record<string, unknown>

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>评测中心</h1>
          <p>
            这里运行的是检索质量评测（Recall@k、MRR、无答案处理），由工作进程执行。
            生成质量、任务成功率与故障恢复需要真实模型端到端运行，报告会明确标注“未执行”。
          </p>
        </div>
        <div className="row">
          <select value={mode} onChange={(event) => setMode(event.target.value)} style={{ width: 140 }}>
            <option value="hybrid">混合检索</option>
            <option value="vector">仅向量</option>
          </select>
          <button className="primary" onClick={start} disabled={busy}>
            运行评测
          </button>
          <button onClick={load}>刷新</button>
        </div>
      </div>

      <div className="split">
        <div className="card">
          <h3>评测记录</h3>
          {evals.length === 0 ? (
            <p className="muted">还没有评测记录。运行一次后，报告会保存在后台任务结果中。</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>评测 ID</th>
                  <th>状态</th>
                  <th>时间</th>
                </tr>
              </thead>
              <tbody>
                {evals.map((item) => (
                  <tr
                    key={item.eval_id}
                    onClick={() => setSelected(item)}
                    style={{ cursor: 'pointer' }}
                  >
                    <td className="mono">{item.eval_id.slice(0, 8)}</td>
                    <td className="faint">{item.status}</td>
                    <td className="faint">{new Date(item.created_at).toLocaleTimeString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="card">
          <h3>指标</h3>
          {Object.keys(metrics).length === 0 ? (
            <p className="muted">选择一条评测记录查看指标。</p>
          ) : (
            <table>
              <tbody>
                {Object.entries(metrics).map(([key, value]) => (
                  <tr key={key}>
                    <td>{key}</td>
                    <td className="mono">
                      {typeof value === 'object' ? JSON.stringify(value) : String(value)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {Object.keys(environment).length ? (
            <details style={{ marginTop: 10 }}>
              <summary>环境与版本快照</summary>
              <pre>{JSON.stringify(environment, null, 2)}</pre>
            </details>
          ) : null}
        </div>
      </div>

      {selected?.case_results?.length ? (
        <div className="card" style={{ marginTop: 14 }}>
          <h3>样本明细（失败样本会保留原因，不隐藏）</h3>
          <table>
            <thead>
              <tr>
                <th>样本</th>
                <th>类别</th>
                <th>命中 / 期望</th>
                <th>首个相关排名</th>
                <th>无答案处理</th>
                <th>错误</th>
              </tr>
            </thead>
            <tbody>
              {selected.case_results.map((item, index) => (
                <tr key={index}>
                  <td className="mono">{String(item.case_id)}</td>
                  <td className="faint">{String(item.category)}</td>
                  <td className="faint">
                    {String(item.hit_count)} / {String(item.expected_count)}
                  </td>
                  <td className="faint">{String(item.first_relevant_rank ?? '—')}</td>
                  <td className="faint">{item.insufficient_evidence ? '已说明证据不足' : '有证据'}</td>
                  <td className="faint">{item.error ? String(item.error) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  )
}

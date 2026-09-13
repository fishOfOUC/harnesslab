import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import type { HealthReady, IndexStatus, ModelProfile } from '../types'
import { StatusPill } from '../components/StatusPill'

/** 必需项显示正常/异常，提示项（advisory）不参与就绪判定。 */
function describeCheck(value: Record<string, unknown>): string {
  if (value.advisory) return '提示项（不影响就绪）'
  return value.ok === true ? '正常' : '异常'
}

export default function Overview({
  projectId,
  notify,
}: {
  projectId: string
  notify: (message: string, tone?: 'info' | 'error' | 'success') => void
  onCreateProject: () => void
}) {
  const [health, setHealth] = useState<HealthReady | null>(null)
  const [profiles, setProfiles] = useState<ModelProfile[]>([])
  const [probe, setProbe] = useState<Record<string, Record<string, unknown>>>({})
  const [index, setIndex] = useState<IndexStatus | null>(null)
  const [runs, setRuns] = useState<{ id: string; status: string; user_message: string; created_at: string }[]>([])
  const [policy, setPolicy] = useState<{ name: string; risk: string; decision: string; reason: string }[]>([])
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try {
      const [healthData, profileData, policyData] = await Promise.all([
        api.health(),
        api.modelProfiles(),
        api.policy(),
      ])
      setHealth(healthData)
      setProfiles(profileData.profiles)
      setPolicy(policyData.policies)
    } catch (error) {
      notify(error instanceof ApiError ? error.message : '概览数据加载失败', 'error')
    }
    try {
      const documents = await api.documents(projectId)
      setIndex(documents.index)
    } catch {
      setIndex(null)
    }
    try {
      const threads = await api.threads(projectId)
      const collected: typeof runs = []
      for (const thread of threads.threads.slice(0, 5)) {
        const detail = await api.threadRuns(thread.id)
        collected.push(...detail.runs)
      }
      collected.sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
      setRuns(collected.slice(0, 8))
    } catch {
      setRuns([])
    }
  }, [projectId, notify])

  useEffect(() => {
    load()
  }, [load])

  async function runProbe(profileId: string) {
    setBusy(profileId)
    try {
      const result = await api.probeProfile(profileId)
      setProbe((prev) => ({ ...prev, [profileId]: result.result }))
      notify(`${profileId} 探测完成（真实调用，见下方结果）`, 'success')
    } catch (error) {
      notify(error instanceof ApiError ? error.message : '探测失败', 'error')
    } finally {
      setBusy('')
    }
  }

  async function seedDemo() {
    setBusy('seed')
    try {
      const result = await api.seedDemo(projectId)
      notify(
        result.queued.length
          ? `已排队 ${result.queued.length} 份演示资料；请启动工作进程完成解析与向量化`
          : '演示资料已全部导入过，本次没有新增',
        'success',
      )
      await load()
    } catch (error) {
      notify(error instanceof ApiError ? error.message : '导入演示资料失败', 'error')
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>概览</h1>
          <p>
            这里展示真实探测结果与真实运行记录。未执行的检查一律标注“未执行”，不会用替身结果冒充模型成绩。
          </p>
        </div>
        <div className="row">
          <button onClick={load}>刷新</button>
          <button className="primary" onClick={seedDemo} disabled={busy === 'seed'}>
            导入演示资料
          </button>
        </div>
      </div>

      <div className="grid">
        <div className="card">
          <h3>服务健康</h3>
          {health ? (
            <>
              <p className="row">
                <StatusPill status={health.status === 'ready' ? 'completed' : 'failed'} />
                <span className="muted">{health.status === 'ready' ? '存储就绪' : '存在必需项异常'}</span>
              </p>
              <table>
                <tbody>
                  {Object.entries(health.checks).map(([name, value]) => (
                    <tr key={name}>
                      <td>{name}</td>
                      <td className="faint">
                        {describeCheck(value)}
                        {value.note ? ` · ${String(value.note)}` : ''}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          ) : (
            <p className="muted">加载中…</p>
          )}
        </div>

        <div className="card">
          <h3>知识库状态</h3>
          {index ? (
            <>
              <p>
                文档 <strong>{index.document_count}</strong> 份 ｜ chunk{' '}
                <strong>{index.chunk_count}</strong> 条 ｜ 向量维度{' '}
                <strong>{index.dimension ?? '未探测'}</strong>
              </p>
              <p className="faint mono">活动索引：{index.active_index_version ?? '尚未建立'}</p>
              {index.pending_documents.length ? (
                <p className="limitations">
                  有 {index.pending_documents.length} 份资料尚未就绪：当前不可检索，可重试失败批次。
                </p>
              ) : (
                <p className="muted">所有资料版本均已就绪。</p>
              )}
            </>
          ) : (
            <p className="muted">尚未建立索引；先导入资料。</p>
          )}
        </div>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h3>模型档案（Chat 与 Embedding 独立配置）</h3>
        <table>
          <thead>
            <tr>
              <th>档案</th>
              <th>模型 / 地址</th>
              <th>能力</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {profiles.map((profile) => {
              const probeResult = probe[profile.id]
              return (
                <tr key={profile.id}>
                  <td>
                    {profile.id}
                    <div className="faint">{profile.role}</div>
                  </td>
                  <td>
                    <span className="mono">{profile.model}</span>
                    <div className="faint mono">{profile.base_url ?? profile.dimension}</div>
                  </td>
                  <td className="faint">
                    {probeResult
                      ? JSON.stringify(probeResult)
                      : '未探测（点击右侧按钮执行真实调用）'}
                  </td>
                  <td>
                    <button onClick={() => runProbe(profile.id)} disabled={busy === profile.id}>
                      探测
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <div className="split" style={{ marginTop: 14 }}>
        <div className="card">
          <h3>最近任务</h3>
          {runs.length === 0 ? (
            <p className="muted">还没有运行记录。</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>状态</th>
                  <th>问题</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.id}>
                    <td>
                      <StatusPill status={run.status} />
                    </td>
                    <td>
                      {run.user_message.slice(0, 60)}
                      <div className="faint mono">{run.id.slice(0, 8)}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="card">
          <h3>工具权限策略（服务端权威）</h3>
          <table>
            <thead>
              <tr>
                <th>工具</th>
                <th>风险</th>
                <th>默认处置</th>
              </tr>
            </thead>
            <tbody>
              {policy.map((item) => (
                <tr key={item.name}>
                  <td>
                    {item.name}
                    <div className="faint">{item.reason}</div>
                  </td>
                  <td className="faint">{item.risk}</td>
                  <td className="faint">{item.decision}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

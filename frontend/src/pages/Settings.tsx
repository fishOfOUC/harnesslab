import { useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import type { ModelProfile } from '../types'

export default function Settings({
  notify,
}: {
  notify: (message: string, tone?: 'info' | 'error' | 'success') => void
}) {
  const [profiles, setProfiles] = useState<ModelProfile[]>([])
  const [policy, setPolicy] = useState<{ name: string; version: string; risk: string; decision: string; reason: string }[]>([])

  useEffect(() => {
    Promise.all([api.modelProfiles(), api.policy()])
      .then(([profileData, policyData]) => {
        setProfiles(profileData.profiles)
        setPolicy(policyData.policies)
      })
      .catch((error) => {
        notify(error instanceof ApiError ? error.message : '设置读取失败', 'error')
      })
  }, [notify])

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>设置</h1>
          <p>
            配置来自环境变量与 <span className="mono">configs/app.config.json</span>（内置默认值 &lt; 配置文件 &lt;
            环境变量）。密钥只通过环境变量注入，接口只返回脱敏摘要，前端不直接访问模型端点。
          </p>
        </div>
      </div>

      <div className="grid">
        {profiles.map((profile) => (
          <div className="card" key={profile.id}>
            <h3>
              {profile.role === 'chat' ? '对话模型' : 'Embedding 模型'} · {profile.id}
            </h3>
            <table>
              <tbody>
                <tr>
                  <td>提供方</td>
                  <td className="mono">{profile.provider}</td>
                </tr>
                <tr>
                  <td>模型 ID</td>
                  <td className="mono">{profile.model}</td>
                </tr>
                <tr>
                  <td>服务地址</td>
                  <td className="mono">{profile.base_url ?? '—'}</td>
                </tr>
                <tr>
                  <td>密钥</td>
                  <td className="faint">{profile.api_key_configured ? '已配置（不回显）' : '未配置'}</td>
                </tr>
                {profile.dimension ? (
                  <tr>
                    <td>维度</td>
                    <td className="mono">{profile.dimension}</td>
                  </tr>
                ) : null}
              </tbody>
            </table>
            {profile.note ? <p className="faint">{profile.note}</p> : null}
          </div>
        ))}

        <div className="card">
          <h3>技能与工具权限</h3>
          <p className="muted">
            技能只提供方法与模板，不能通过 <span className="mono">required_tools</span> 提升权限；
            真实 effect 与权限来自服务端注册表。
          </p>
          <table>
            <thead>
              <tr>
                <th>工具</th>
                <th>版本</th>
                <th>风险</th>
                <th>处置</th>
              </tr>
            </thead>
            <tbody>
              {policy.map((item) => (
                <tr key={item.name}>
                  <td>
                    {item.name}
                    <div className="faint">{item.reason}</div>
                  </td>
                  <td className="mono">{item.version}</td>
                  <td className="faint">{item.risk}</td>
                  <td className="faint">{item.decision}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="card">
          <h3>离线与降级说明</h3>
          <ul className="muted" style={{ paddingLeft: 18 }}>
            <li>Chat 模型不可用时，界面只展示 Embedding 检查、检索实验室与标注为历史回放的结果。</li>
            <li>“替身模式”（CHAT_PROVIDER=stub）仅用于界面与契约演示，不代表真实模型能力。</li>
            <li>沙箱未启用时，代码执行工具在后端策略层直接拒绝，界面不提供入口。</li>
            <li>本地追踪为默认；启用云端追踪前需要明确将发送哪些字段。</li>
          </ul>
        </div>
      </div>
    </div>
  )
}

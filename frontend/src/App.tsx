import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from './api'
import type { Project, Thread } from './types'
import Overview from './pages/Overview'
import Workbench from './pages/Workbench'
import Knowledge from './pages/Knowledge'
import Approvals from './pages/Approvals'
import Evaluations from './pages/Evaluations'
import Settings from './pages/Settings'

type PageKey = 'overview' | 'workbench' | 'knowledge' | 'approvals' | 'evals' | 'settings'

const NAV: { key: PageKey; label: string }[] = [
  { key: 'overview', label: '概览' },
  { key: 'workbench', label: '任务工作台' },
  { key: 'knowledge', label: '知识库' },
  { key: 'approvals', label: '审批中心' },
  { key: 'evals', label: '评测中心' },
  { key: 'settings', label: '设置' },
]

export interface Toast {
  id: number
  message: string
  tone: 'info' | 'error' | 'success'
}

export default function App() {
  const [page, setPage] = useState<PageKey>('overview')
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState('')
  const [threads, setThreads] = useState<Thread[]>([])
  const [activeThreadId, setActiveThreadId] = useState('')
  const [toasts, setToasts] = useState<Toast[]>([])
  const [booting, setBooting] = useState(true)
  const [pendingApprovals, setPendingApprovals] = useState(0)

  const notify = useCallback((message: string, tone: Toast['tone'] = 'info') => {
    const id = Date.now() + Math.random()
    setToasts((prev) => [...prev, { id, message, tone }])
    window.setTimeout(() => setToasts((prev) => prev.filter((item) => item.id !== id)), 6000)
  }, [])

  const reloadProjects = useCallback(async () => {
    const data = await api.projects()
    setProjects(data.projects)
    if (!data.projects.length) {
      setProjectId('')
      return
    }
    setProjectId((current) =>
      data.projects.some((project) => project.id === current) ? current : data.projects[0].id,
    )
  }, [])

  useEffect(() => {
    reloadProjects()
      .catch((error) => {
        const message = error instanceof ApiError ? error.message : '后端不可用'
        notify(`初始化失败：${message}`, 'error')
      })
      .finally(() => setBooting(false))
  }, [reloadProjects, notify])

  const reloadThreads = useCallback(async () => {
    if (!projectId) {
      setThreads([])
      return
    }
    const data = await api.threads(projectId)
    setThreads(data.threads)
    setActiveThreadId((current) =>
      data.threads.some((thread) => thread.id === current) ? current : (data.threads[0]?.id ?? ''),
    )
  }, [projectId])

  const reloadApprovals = useCallback(async () => {
    if (!projectId) {
      setPendingApprovals(0)
      return
    }
    try {
      const data = await api.projectApprovals(projectId, 'pending')
      setPendingApprovals(data.approvals.length)
    } catch {
      setPendingApprovals(0)
    }
  }, [projectId])

  useEffect(() => {
    reloadThreads().catch(() => setThreads([]))
  }, [reloadThreads])

  useEffect(() => {
    reloadApprovals()
    const timer = window.setInterval(reloadApprovals, 8000)
    return () => window.clearInterval(timer)
  }, [reloadApprovals])

  async function createDemoProject() {
    try {
      const project = await api.createProject('HarnessLab 演示项目')
      await reloadProjects()
      setProjectId(project.id)
      notify('已创建演示项目', 'success')
    } catch (error) {
      notify(error instanceof ApiError ? error.message : '创建项目失败', 'error')
    }
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <strong>HarnessLab</strong>
          <span>单机技术展示</span>
        </div>

        <div className="sidebar-section">
          <span className="sidebar-title">项目</span>
          <select
            value={projectId}
            onChange={(event) => setProjectId(event.target.value)}
            aria-label="选择项目"
          >
            {projects.length === 0 ? <option value="">尚无项目</option> : null}
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
          <button onClick={createDemoProject}>新建演示项目</button>
        </div>

        <nav className="nav" aria-label="主导航">
          {NAV.map((item) => (
            <button
              key={item.key}
              aria-current={page === item.key ? 'page' : undefined}
              onClick={() => setPage(item.key)}
            >
              {item.label}
              {item.key === 'approvals' && pendingApprovals > 0 ? `（${pendingApprovals}）` : ''}
            </button>
          ))}
        </nav>

        {page === 'workbench' ? (
          <div className="sidebar-section">
            <span className="sidebar-title">会话</span>
            {threads.length === 0 ? <span className="faint">暂无会话</span> : null}
            {threads.map((thread) => (
              <button
                key={thread.id}
                className="thread-item"
                aria-current={activeThreadId === thread.id}
                onClick={() => setActiveThreadId(thread.id)}
                title={thread.title || '未命名会话'}
              >
                {thread.title || '未命名会话'}
              </button>
            ))}
          </div>
        ) : null}

        <div className="sidebar-section" style={{ marginTop: 'auto' }}>
          <span className="faint">
            本机开发模式：固定身份，仅绑定 loopback。界面展示的“已实现/已验证/受模型限制”以实际运行结果为准。
          </span>
        </div>
      </aside>

      <main className="main">
        {booting ? (
          <div className="page">
            <p className="muted">正在检查后端与存储状态…</p>
          </div>
        ) : !projectId ? (
          <div className="page">
            <div className="empty">
              还没有项目。点击左侧“新建演示项目”，然后到知识库导入演示资料。
            </div>
          </div>
        ) : page === 'overview' ? (
          <Overview projectId={projectId} notify={notify} onCreateProject={createDemoProject} />
        ) : page === 'workbench' ? (
          <Workbench
            projectId={projectId}
            activeThreadId={activeThreadId}
            onThreadCreated={async (threadId) => {
              await reloadThreads()
              setActiveThreadId(threadId)
            }}
            notify={notify}
            onRunChanged={() => {
              reloadApprovals()
            }}
          />
        ) : page === 'knowledge' ? (
          <Knowledge projectId={projectId} notify={notify} />
        ) : page === 'approvals' ? (
          <Approvals
            projectId={projectId}
            notify={notify}
            onHandled={async () => {
              await reloadApprovals()
            }}
          />
        ) : page === 'evals' ? (
          <Evaluations projectId={projectId} notify={notify} />
        ) : (
          <Settings notify={notify} />
        )}
      </main>

      <div className="toast" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <div key={toast.id} className={`item ${toast.tone === 'info' ? '' : toast.tone}`}>
            {toast.message}
          </div>
        ))}
      </div>
    </div>
  )
}

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import type { DocumentItem, IndexStatus, RetrievalHit } from '../types'

const STATUS_TEXT: Record<string, string> = {
  pending: '待处理',
  parsing: '解析中',
  staging: '写入索引中',
  ready: '已就绪',
  unsupported: '不支持的格式',
  failed: '处理失败',
  deleted: '已删除',
}

export default function Knowledge({
  projectId,
  notify,
}: {
  projectId: string
  notify: (message: string, tone?: 'info' | 'error' | 'success') => void
}) {
  const [documents, setDocuments] = useState<DocumentItem[]>([])
  const [index, setIndex] = useState<IndexStatus | null>(null)
  const [query, setQuery] = useState('v2 的并发上限与过载策略')
  const [mode, setMode] = useState('hybrid')
  const [hits, setHits] = useState<RetrievalHit[]>([])
  const [candidates, setCandidates] = useState<Record<string, unknown>[]>([])
  const [previewNote, setPreviewNote] = useState('')
  const [busy, setBusy] = useState('')
  const [jobs, setJobs] = useState<{ id: string; label: string; status: string }[]>([])
  const fileRef = useRef<HTMLInputElement | null>(null)

  const load = useCallback(async () => {
    try {
      const data = await api.documents(projectId)
      setDocuments(data.documents)
      setIndex(data.index)
    } catch (error) {
      notify(error instanceof ApiError ? error.message : '知识库读取失败', 'error')
    }
  }, [projectId, notify])

  useEffect(() => {
    load()
  }, [load])

  async function watchJob(jobId: string, label: string) {
    setJobs((prev) => [...prev, { id: jobId, label, status: 'queued' }])
    for (let attempt = 0; attempt < 60; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000))
      try {
        const job = await api.job(jobId)
        setJobs((prev) => prev.map((item) => (item.id === jobId ? { ...item, status: job.status } : item)))
        if (job.status === 'succeeded' || job.status === 'failed') {
          await load()
          return
        }
      } catch {
        return
      }
    }
  }

  async function upload(files: FileList | null) {
    if (!files || !files.length) return
    setBusy('upload')
    try {
      for (const file of Array.from(files)) {
        const result = await api.upload(projectId, file)
        notify(`${file.name}：${result.note}`, 'success')
        watchJob(result.job_id, file.name)
      }
    } catch (error) {
      notify(error instanceof ApiError ? error.message : '上传失败', 'error')
    } finally {
      setBusy('')
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  async function remove(documentId: string, title: string) {
    setBusy(documentId)
    try {
      await api.deleteDocument(projectId, documentId)
      notify(`${title} 已逻辑删除：后续检索与原文访问被立即阻断`, 'success')
      await load()
    } catch (error) {
      notify(error instanceof ApiError ? error.message : '删除失败', 'error')
    } finally {
      setBusy('')
    }
  }

  async function rebuild() {
    setBusy('rebuild')
    try {
      const result = await api.rebuildIndex(projectId)
      notify(result.note, 'info')
      watchJob(result.job_id, '索引重建')
    } catch (error) {
      notify(error instanceof ApiError ? error.message : '重建失败', 'error')
    } finally {
      setBusy('')
    }
  }

  async function preview() {
    setBusy('preview')
    try {
      const result = await api.preview(projectId, query, 8, mode)
      setHits(result.hits)
      setCandidates(result.candidates)
      setPreviewNote(`${result.mode} ｜ 索引 ${result.index_version.slice(0, 8)} ｜ ${result.note}`)
      if (result.insufficient_evidence) {
        notify('检索到的证据不足，问答时应明确说明无法确认', 'info')
      }
    } catch (error) {
      notify(error instanceof ApiError ? error.message : '检索失败', 'error')
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>知识库</h1>
          <p>
            导入流水线：上传 → 文件检查 → 解析 → 切分 → 向量化 → 写索引 → 可见性提交。
            未完成的版本不会被检索到。
          </p>
        </div>
        <div className="row">
          <button onClick={load}>刷新</button>
          <button onClick={rebuild} disabled={busy === 'rebuild'}>
            重建索引
          </button>
        </div>
      </div>

      <div className="card">
        <h3>上传资料（Markdown / TXT / 带文本层 PDF，单文件 ≤ 20 MB）</h3>
        <input
          ref={fileRef}
          type="file"
          multiple
          accept=".md,.markdown,.txt,.pdf,.csv,.json,.yaml,.yml"
          onChange={(event) => upload(event.target.files)}
          disabled={busy === 'upload'}
        />
        <p className="faint" style={{ marginTop: 8 }}>
          扫描件 PDF 会明确标记为不支持并提示 OCR 扩展，不会被当作空文档索引成功。
        </p>
        {jobs.length ? (
          <table style={{ marginTop: 10 }}>
            <thead>
              <tr>
                <th>后台任务</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.id}>
                  <td>{job.label}</td>
                  <td className="faint">{job.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h3>
          文档与版本
          {index ? (
            <span className="faint">
              {' '}
              ｜ 活动索引 {index.active_index_version?.slice(0, 8) ?? '未建立'} ｜ {index.chunk_count} 个 chunk ｜ 维度{' '}
              {index.dimension ?? '未探测'}
            </span>
          ) : null}
        </h3>
        {documents.length === 0 ? (
          <p className="muted">还没有资料。可以在概览页一键导入演示资料。</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>标题</th>
                <th>类型</th>
                <th>活动版本 / 状态</th>
                <th>chunk</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {documents.map((document) => {
                const version = document.versions[0]
                return (
                  <tr key={document.id}>
                    <td>
                      {document.title}
                      <div className="faint mono">{document.id.slice(0, 8)}</div>
                    </td>
                    <td className="faint">{document.media_type}</td>
                    <td>
                      {version ? (
                        <>
                          v{version.version} · {STATUS_TEXT[version.parse_status] ?? version.parse_status}
                          {version.error_message ? (
                            <div className="faint">{version.error_message}</div>
                          ) : null}
                        </>
                      ) : (
                        '无版本'
                      )}
                    </td>
                    <td className="faint">{version?.chunk_count ?? 0}</td>
                    <td>
                      <button
                        className="danger"
                        onClick={() => remove(document.id, document.title)}
                        disabled={busy === document.id || Boolean(document.deleted_at)}
                      >
                        {document.deleted_at ? '已删除' : '删除'}
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h3>检索实验室</h3>
        <div className="row">
          <input
            type="text"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="输入查询以查看各阶段候选与融合结果"
            style={{ flex: 1, minWidth: 260 }}
          />
          <select value={mode} onChange={(event) => setMode(event.target.value)} style={{ width: 140 }}>
            <option value="hybrid">混合检索</option>
            <option value="vector">仅向量</option>
          </select>
          <button className="primary" onClick={preview} disabled={busy === 'preview'}>
            检索
          </button>
        </div>
        <p className="faint" style={{ marginTop: 8 }}>
          {previewNote || '相同查询在不同检索模式下可对比召回差异；阈值需用评测集校准，不跨模型复用。'}
        </p>
        {hits.length ? (
          <table style={{ marginTop: 10 }}>
            <thead>
              <tr>
                <th>来源</th>
                <th>定位</th>
                <th>分数</th>
                <th>向量/稀疏排名</th>
                <th>片段</th>
              </tr>
            </thead>
            <tbody>
              {hits.map((hit) => (
                <tr key={hit.chunk_id}>
                  <td>
                    {hit.source_title}
                    <div className="faint">v{hit.document_version}</div>
                  </td>
                  <td className="faint">
                    {hit.heading_path || '—'}
                    {hit.page ? ` · 第 ${hit.page} 页` : ''}
                  </td>
                  <td className="mono">{hit.score.toFixed(4)}</td>
                  <td className="faint">
                    {hit.vector_rank ?? '—'} / {hit.sparse_rank ?? '—'}
                  </td>
                  <td className="faint">{hit.text.slice(0, 90)}…</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        {candidates.length ? (
          <details style={{ marginTop: 10 }}>
            <summary>融合阶段候选（{candidates.length} 条）</summary>
            <pre>{JSON.stringify(candidates, null, 2)}</pre>
          </details>
        ) : null}
      </div>
    </div>
  )
}

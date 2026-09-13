import { useState } from 'react'
import { api, ApiError } from '../api'
import type { Citation } from '../types'

interface SourceDetail {
  source_title: string
  heading_path: string
  page?: number | null
  char_start?: number | null
  char_end?: number | null
  text: string
  document_version: number
}

/** 引用列表：点击后查看该引用绑定的固定文档版本原文，不跳转到可能已更新的最新版本。 */
export function CitationList({
  citations,
  onError,
}: {
  citations: Citation[]
  onError: (message: string) => void
}) {
  const [opened, setOpened] = useState<Record<string, SourceDetail | 'loading'>>({})

  if (!citations.length) {
    return <p className="muted">本次答案没有可解析的引用。</p>
  }

  async function toggle(citation: Citation) {
    if (opened[citation.chunk_id]) {
      setOpened((prev) => {
        const next = { ...prev }
        delete next[citation.chunk_id]
        return next
      })
      return
    }
    setOpened((prev) => ({ ...prev, [citation.chunk_id]: 'loading' }))
    try {
      const detail = await api.source(citation.chunk_id)
      setOpened((prev) => ({ ...prev, [citation.chunk_id]: detail }))
    } catch (error) {
      const message = error instanceof ApiError ? error.message : '原文读取失败'
      onError(message)
      setOpened((prev) => {
        const next = { ...prev }
        delete next[citation.chunk_id]
        return next
      })
    }
  }

  return (
    <div className="citations">
      {citations.map((citation) => {
        const state = opened[citation.chunk_id]
        return (
          <div key={citation.chunk_id}>
            <button
              className="citation"
              onClick={() => toggle(citation)}
              aria-expanded={Boolean(state)}
              aria-label={`查看引用 ${citation.label} 的原文`}
            >
              <div className="between">
                <strong>[{citation.label}] {citation.source_title}</strong>
                <span className="faint">v{citation.document_version}</span>
              </div>
              <div className="faint">
                {citation.heading_path || '未标注标题'}
                {citation.page ? ` · 第 ${citation.page} 页` : ''}
                {citation.char_start !== null && citation.char_start !== undefined
                  ? ` · 字符 ${citation.char_start}-${citation.char_end}`
                  : ''}
              </div>
              {state === 'loading' ? (
                <div className="snippet">正在读取原文…</div>
              ) : typeof state === 'object' ? (
                <pre>{state.text}</pre>
              ) : citation.snippet ? (
                <div className="snippet">{citation.snippet}</div>
              ) : null}
            </button>
          </div>
        )
      })}
    </div>
  )
}

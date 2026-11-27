import { useEffect, useState } from 'react'
import { api, apiErrorMessage } from '../api'
import { AdminSessionCard, useAdminAuth } from './AdminAuth'

type DocumentRecord = { id: string; filename: string; size: number; format: string; status: string; content?: string; truncated?: boolean }

export default function DocumentPanel({ selected, onSelected, maxSelected = 5 }: { selected: string[]; onSelected: (ids: string[]) => void; maxSelected?: number }) {
  const [items, setItems] = useState<DocumentRecord[]>([])
  const [preview, setPreview] = useState<DocumentRecord | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  // 管理员会话（V2）：上传/删除由服务端 require_admin 保护；未强制时不改变本机原有行为。
  const admin = useAdminAuth()
  const needAdminLogin = admin.session?.required === true && admin.session?.authenticated !== true
  // 会话尚未读到时短暂禁用，避免「先可点、随后被服务端拒绝」的观感
  const adminUnknown = admin.loading && admin.session === null
  const adminNote = needAdminLogin
    ? '需要管理员登录：上传与删除文件由服务端管理员校验保护。'
    : admin.session && !admin.session.configured
      ? '服务端未配置管理员令牌：本地开发模式下上传/删除直接可用。'
      : ''

  async function refresh() {
    try { const r = await api.get<{ documents: DocumentRecord[] }>('/api/documents'); setItems(r.data.documents) }
    catch (e) { setError(apiErrorMessage(e)) }
  }
  useEffect(() => { void refresh() }, [])
  async function upload(file: File) {
    if (needAdminLogin) { setError('需要管理员登录后才能上传文件'); return }
    setLoading(true); setError('')
    try {
      const data = new FormData(); data.append('file', file)
      // 文件解析（PDF/DOCX/XLSX）可能耗时，单独放宽超时，仍受服务端 8MB 上限约束
      await api.post('/api/documents', data, { timeout: 60000 })
      await refresh()
    } catch (e) { setError(apiErrorMessage(e)) }
    finally { setLoading(false) }
  }
  async function show(id: string) {
    try { const r = await api.get<{ document: DocumentRecord }>(`/api/documents/${id}`); setPreview(r.data.document) }
    catch (e) { setError(apiErrorMessage(e)) }
  }
  async function remove(id: string) {
    if (needAdminLogin) { setError('需要管理员登录后才能删除文件'); return }
    if (!window.confirm('确认删除这个已解析的文件？')) return
    try {
      // 公网网关可能拒绝 DELETE，统一走 POST 等价入口（本地后端同样支持）
      await api.post(`/api/documents/${id}/delete`)
      onSelected(selected.filter(x => x !== id))
      if (preview?.id === id) setPreview(null)
      await refresh()
    } catch (e) { setError(apiErrorMessage(e)) }
  }
  const uploadDisabled = loading || needAdminLogin || adminUnknown
  const deleteDisabled = needAdminLogin || adminUnknown
  return <section className="card tp-doc-panel">
    <div className="tp-doc-header"><div><strong>文件资料工作台</strong><p className="muted small">上传、解析与预览文件，并引用到 Agent 任务 · 单文件 8MB</p></div>
      <label className="btn primary" style={{ cursor: uploadDisabled ? 'not-allowed' : 'pointer', opacity: uploadDisabled ? 0.55 : 1 }} title={needAdminLogin ? '需要管理员登录后才能上传文件' : undefined} aria-disabled={uploadDisabled}> {loading ? '解析中…' : '上传文件'}
        <input type="file" accept=".txt,.md,.json,.csv,.pdf,.docx,.doc,.xlsx" disabled={uploadDisabled} hidden onChange={e => { const f = e.target.files?.[0]; if (f) void upload(f); e.target.value = '' }} />
      </label></div>
    <AdminSessionCard auth={admin} compact />
    {adminNote && <p className="muted small" style={{ margin: '8px 0 0' }}>{adminNote}</p>}
    {error && <div className="errorbox" style={{ marginTop: 10 }}>{error}</div>}
    <div className="tp-doc-grid" style={{ marginTop: 12 }}><div className="tp-doc-list">
      {items.length === 0 ? <p className="muted small">暂无文件。上传后可选择文件作为任务资料。</p> : items.map(d => <div className="tp-doc-row" key={d.id}>
        <input type="checkbox" aria-label={`引用 ${d.filename}`} checked={selected.includes(d.id)} onChange={e => onSelected(e.target.checked ? [...selected, d.id].slice(0, maxSelected) : selected.filter(x => x !== d.id))} />
        <div className="tp-doc-name" title={d.filename}>{d.filename}<small>{d.format.toUpperCase()} · {(d.size / 1024).toFixed(1)} KB</small></div>
        <button className="btn" onClick={() => void show(d.id)}>预览</button><button className="btn" onClick={() => void remove(d.id)} disabled={deleteDisabled} title={needAdminLogin ? '需要管理员登录后才能删除文件' : undefined}>删除</button>
      </div>)}
    </div><div className="tp-doc-preview">{preview ? <><strong>{preview.filename}</strong><pre>{preview.content || '未提取到文本'}</pre></> : <div className="muted small">选择文件后点击「预览」，这里显示实际解析的文字。</div>}</div></div>
    <p className="muted small" style={{ margin: '10px 0 0' }}>最多引用 {maxSelected} 个文件；仅解析文本，不执行文件内指令。旧版 DOC 请先转为 DOCX；扫描版 PDF 需 OCR。</p>
  </section>
}

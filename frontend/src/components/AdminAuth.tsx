// 管理员会话（V2）：登录 / 登出状态与卡片。
//
// 安全边界：
// - 令牌只通过 POST /api/admin/login 发给服务端，登录成功后立即从组件状态清空；
// - 不写 localStorage / sessionStorage / Cookie（会话 Cookie 由服务端 HttpOnly 下发）；
// - 未配置管理员令牌（configured=false）时不提供登录表单，避免误伤本地开发。

import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { KeyRound, LogOut, ShieldCheck, ShieldAlert } from 'lucide-react'
import { adminLogin, adminLogout, fetchAdminSession, v2ErrorMessage } from '../v2api'
import type { AdminSession } from '../v2api'
import { Card, ErrorBox, Spinner } from './ui'

export interface AdminAuthController {
  session: AdminSession | null
  loading: boolean
  busy: boolean
  error: string | null
  refresh: () => Promise<void>
  login: (token: string) => Promise<boolean>
  logout: () => Promise<void>
}

/** 读取并操作管理员会话；多个卡片可各自持有实例（会话在服务端 Cookie 中）。 */
export function useAdminAuth(): AdminAuthController {
  const [session, setSession] = useState<AdminSession | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setSession(await fetchAdminSession())
    } catch (e) {
      setError(v2ErrorMessage(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const login = useCallback(async (token: string) => {
    const value = token.trim()
    if (!value) {
      setError('请输入管理员令牌')
      return false
    }
    setBusy(true)
    setError(null)
    try {
      await adminLogin(value)
      // 以服务端会话为唯一事实来源：登录后重新读取，而不是直接相信本地状态
      const next = await fetchAdminSession()
      setSession(next)
      if (!next.authenticated) {
        setError(
          '登录请求已被服务端接受，但浏览器没有保存会话 Cookie。' +
            '请通过同源地址访问后端（或检查服务端 Secure/SameSite Cookie 配置）后重试。',
        )
      }
      return true
    } catch (e) {
      setError(v2ErrorMessage(e))
      return false
    } finally {
      setBusy(false)
    }
  }, [])

  const logout = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      await adminLogout()
      await refresh()
    } catch (e) {
      setError(v2ErrorMessage(e))
    } finally {
      setBusy(false)
    }
  }, [refresh])

  return { session, loading, busy, error, refresh, login, logout }
}

/** 管理员登录 / 登出小卡。compact 用于嵌在既有卡片（如文件资料工作台）内部。 */
export function AdminSessionCard({
  auth,
  compact = false,
}: {
  auth: AdminAuthController
  compact?: boolean
}) {
  const [token, setToken] = useState('')
  const session = auth.session

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const ok = await auth.login(token)
    // 无论成功失败都不把令牌留在组件状态里；成功后立即清空
    if (ok) setToken('')
  }

  const statusBadge = (() => {
    if (auth.loading && !session) return <span className="badge muted">读取中…</span>
    if (!session) return <span className="badge muted">状态未知</span>
    if (session.authenticated) return <span className="badge success">已认证</span>
    if (!session.configured) return <span className="badge muted">未配置管理员令牌</span>
    if (session.required) return <span className="badge partial">未登录 · 需要管理员</span>
    return <span className="badge partial">未登录 · 未强制</span>
  })()

  const body = (
    <div className="stack small">
      <div className="row" style={{ gap: 8 }}>
        <ShieldCheck size={15} />
        <span>管理员会话</span>
        {statusBadge}
        {session && (
          <span className="muted mono" style={{ fontSize: 11 }}>
            {session.cookie_name} · TTL {Math.round(session.session_ttl_seconds / 3600)}h
          </span>
        )}
        <div style={{ flex: 1 }} />
        <button
          type="button"
          className="btn"
          onClick={() => void auth.refresh()}
          disabled={auth.loading || auth.busy}
        >
          刷新状态
        </button>
      </div>

      {auth.error && <ErrorBox message={auth.error} />}

      {!session && auth.loading && <Spinner />}

      {session && !session.authenticated && (
        <>
          {session.configured ? (
            <form className="row" onSubmit={(e) => void submit(e)} style={{ gap: 8 }}>
              <label className="muted small" htmlFor="tp-admin-token">
                管理员令牌
              </label>
              <input
                id="tp-admin-token"
                type="password"
                autoComplete="off"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="粘贴服务端管理员令牌"
                style={{ minWidth: 220, flex: '1 1 220px' }}
                disabled={auth.busy}
              />
              <button className="btn primary" type="submit" disabled={auth.busy || !token.trim()}>
                <KeyRound size={14} /> {auth.busy ? '登录中…' : '登录'}
              </button>
            </form>
          ) : (
            // 未配置管理员令牌时保留输入框样式但禁用：登录端点在服务端会返回 503
            <form className="row" onSubmit={(e) => e.preventDefault()} style={{ gap: 8 }}>
              <label className="muted small" htmlFor="tp-admin-token">
                管理员令牌
              </label>
              <input
                id="tp-admin-token"
                type="password"
                autoComplete="off"
                value={token}
                placeholder="服务端未配置管理员令牌，暂不可登录"
                style={{ minWidth: 220, flex: '1 1 220px' }}
                disabled
              />
              <button className="btn primary" type="button" disabled>
                <KeyRound size={14} /> 登录
              </button>
            </form>
          )}

          {!session.configured ? (
            <p className="muted" style={{ margin: 0 }}>
              服务端未配置管理员令牌（<span className="mono">TRACEPILOT_ADMIN_TOKEN</span>）：
              管理员功能在本机开发模式下直接可用，无需登录。
            </p>
          ) : (
            <p className="muted" style={{ margin: 0 }}>
              令牌只用于本次登录请求，不写入 localStorage；成功后由服务端下发 HttpOnly 会话 Cookie。
            </p>
          )}

          {session.required && (
            <div
              className="errorbox"
              style={{ background: 'var(--warning-soft)', color: 'var(--warning)' }}
            >
              该服务已强制管理员校验：未登录时上传/删除、真实调用与数据导入都会被服务端拒绝。
            </div>
          )}

          {session.configured && !session.required && (
            <p className="muted small" style={{ margin: 0 }}>
              <ShieldAlert size={13} /> 服务端未强制管理员校验（TRACEPILOT_ADMIN_REQUIRED 未开启）：
              当前功能仍可用，登录仅用于验证令牌是否正确。
            </p>
          )}
        </>
      )}

      {session && session.authenticated && (
        <div className="row" style={{ gap: 10 }}>
          <span className="muted small">
            已通过服务端管理员校验（HttpOnly 会话），可执行受控真实调用与数据导入。
          </span>
          <div style={{ flex: 1 }} />
          <button className="btn" type="button" onClick={() => void auth.logout()} disabled={auth.busy}>
            <LogOut size={14} /> {auth.busy ? '退出中…' : '登出'}
          </button>
        </div>
      )}

      {session?.note && (
        <p className="muted" style={{ margin: 0, fontSize: 11.5 }}>
          {session.note}
        </p>
      )}
    </div>
  )

  if (compact) {
    return (
      <div className="cf-note" role="region" aria-label="管理员登录">
        {body}
      </div>
    )
  }

  return (
    <Card title="管理员登录" action={<ShieldCheck size={16} />}>
      {body}
    </Card>
  )
}

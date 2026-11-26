import { useEffect, useRef, useState } from 'react'
import { Check, Copy, ExternalLink, LoaderCircle, ShieldCheck, X } from 'lucide-react'
import {
  ApiError,
  cancelAccountAuth,
  fetchAccountAuth,
  fetchProxies,
  getPendingAccountAuthId,
  setPendingAccountAuthId,
  startAccountAuth,
  submitAccountAuthCode,
} from '../api/client'
import type { Account, AccountAuthSession, ProxyConfig } from '../types'

const activeStatuses = new Set(['starting', 'awaiting_code', 'verifying'])
const inputClass = 'w-full rounded-xl border border-control bg-inset px-3 py-2.5 text-sm text-ink focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent-ring disabled:opacity-60'
const buttonClass = 'inline-flex min-h-11 items-center justify-center gap-2 rounded-xl px-4 py-2 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50'

export function AccountAuthDialog({
  account,
  onClose,
  onAuthorized,
}: {
  account: Account | null
  onClose: () => void
  onAuthorized: (account: Account) => void
}) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const completedRef = useRef(false)
  const [name, setName] = useState(account?.name ?? '')
  const [tier, setTier] = useState(account?.tier ?? 'pro')
  const [proxyId, setProxyId] = useState(account?.proxy_id ?? '')
  const [proxies, setProxies] = useState<ProxyConfig[]>([])
  const [session, setSession] = useState<AccountAuthSession | null>(() => {
    const sessionId = getPendingAccountAuthId()
    return sessionId ? {
      session_id: sessionId, name: '', tier: 'pro', proxy_id: null, status: 'starting',
      authorization_url: null, created_at: 0, expires_at: 0, error: '', account: null,
    } : null
  })
  const sessionRef = useRef<AccountAuthSession | null>(session)
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [pollError, setPollError] = useState('')
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)

  const acceptSession = (next: AccountAuthSession) => {
    const current = sessionRef.current
    if (current?.session_id === next.session_id) {
      // Ignore a GET that started before the code POST or completion arrived.
      if (!activeStatuses.has(current.status) && activeStatuses.has(next.status)) return
      if (current.status === 'verifying' && next.status === 'awaiting_code') return
    }
    sessionRef.current = next
    setSession(next)
    setName(next.name)
    setTier(next.tier)
    setProxyId(next.proxy_id ?? '')
    setPendingAccountAuthId(activeStatuses.has(next.status) ? next.session_id : null)
  }

  useEffect(() => {
    const dialog = dialogRef.current
    dialog?.showModal()
    dialog?.querySelector<HTMLInputElement>('#account-auth-name')?.focus()
    return () => {
      dialog?.close()
    }
  }, [])

  useEffect(() => {
    let disposed = false
    void fetchProxies().then((data) => {
      if (!disposed) setProxies(data.proxies)
    }).catch(() => {
      if (!disposed) setError('无法加载代理列表；请先在「代理管理」页面检查配置。')
    })
    return () => { disposed = true }
  }, [])

  const sessionId = session?.session_id
  const active = !!session && activeStatuses.has(session.status)
  useEffect(() => {
    if (!sessionId || !active) return
    let disposed = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const next = await fetchAccountAuth(sessionId)
        if (disposed) return
        acceptSession(next)
        setPollError('')
        if (!activeStatuses.has(next.status)) return
      } catch (err) {
        if (disposed) return
        if (err instanceof ApiError && err.status === 404) {
          const current = sessionRef.current
          if (current) acceptSession({ ...current, status: 'failed', authorization_url: null, error: err.message })
          return
        }
        setPollError('暂时无法查询授权状态，正在重连…')
      }
      if (!disposed) timer = setTimeout(poll, 1000)
    }
    void poll()
    return () => {
      disposed = true
      clearTimeout(timer)
    }
  }, [sessionId, active])

  useEffect(() => {
    if (session?.status === 'succeeded' && session.account && !completedRef.current) {
      completedRef.current = true
      onAuthorized(session.account)
    }
  }, [session, onAuthorized])

  const start = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setPollError('')
    setBusy(true)
    try {
      acceptSession(await startAccountAuth(name.trim(), tier, proxyId || null))
      setCode('')
      setCopied(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : '无法生成授权链接')
    } finally {
      setBusy(false)
    }
  }

  const submitCode = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!session) return
    setError('')
    setBusy(true)
    try {
      acceptSession(await submitAccountAuthCode(session.session_id, code.trim()))
      setCode('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '提交授权码失败')
    } finally {
      setBusy(false)
    }
  }

  const close = async () => {
    if (busy) return
    setBusy(true)
    try {
      const current = sessionRef.current
      if (current && activeStatuses.has(current.status)) {
        acceptSession(await cancelAccountAuth(current.session_id))
      }
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : '取消授权失败，请重试')
      setBusy(false)
    }
  }

  const finished = !!session && !activeStatuses.has(session.status)
  const success = session?.status === 'succeeded'
  const authorizationUrl = session?.authorization_url

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby="account-auth-title"
      aria-describedby="account-auth-description"
      onCancel={(e) => { e.preventDefault(); void close() }}
      className="m-auto max-h-[90dvh] w-[calc(100%_-_2rem)] max-w-xl overflow-y-auto rounded-3xl border border-line bg-surface p-5 text-ink shadow-floating backdrop:bg-scrim sm:p-7"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="pt-2">
          <h2 id="account-auth-title" className="text-xl font-semibold tracking-tight">
            {account ? `授权账号 ${account.name}` : '添加 Colab 账号'}
          </h2>
        </div>
        <button type="button" aria-label="关闭授权窗口" disabled={busy} onClick={() => void close()}
          className={`${buttonClass} shrink-0 px-3 text-secondary hover:bg-hover-surface`}>
          <X aria-hidden="true" className="h-5 w-5" />
        </button>
      </div>
      <p id="account-auth-description" className="mt-2 text-sm leading-6 text-muted">
        打开 Google 授权链接，登录目标账号，再将 Google 显示的授权码贴回此处。
      </p>

      {!active && !success && (
        <form onSubmit={start} className="mt-5 space-y-4">
          <div>
            <label htmlFor="account-auth-name" className="mb-1.5 block text-sm text-ink">账号槽位名称</label>
            <input id="account-auth-name" value={name} onChange={(e) => setName(e.target.value)}
              required pattern={'[A-Za-z0-9_\\-]+'} maxLength={64} readOnly={!!account} disabled={busy}
              placeholder="例如 pro_03" aria-describedby="account-auth-name-hint" className={inputClass} />
            <p id="account-auth-name-hint" className="mt-1 text-xs text-muted">限字母、数字、下划线和连字符，每个账号使用独立槽位。</p>
          </div>
          <div>
            <label htmlFor="account-auth-tier" className="mb-1.5 block text-sm text-ink">订阅等级</label>
            <select id="account-auth-tier" value={tier} onChange={(e) => setTier(e.target.value)} disabled={busy} className={inputClass}>
              <option value="pro">Pro</option><option value="pro+">Pro+</option><option value="free">Free</option>
            </select>
          </div>
          <div>
            <label htmlFor="account-auth-proxy" className="mb-1.5 block text-sm text-ink">该账号专属代理</label>
            <select id="account-auth-proxy" value={proxyId} onChange={(e) => setProxyId(e.target.value)} disabled={busy} className={inputClass}>
              <option value="">沿用服务器网络</option>
              {proxies.map((proxy) => <option key={proxy.proxy_id} value={proxy.proxy_id}>{proxy.name} · {proxy.endpoint}</option>)}
            </select>
            <p className="mt-1 text-xs text-muted">新账号授权请求也会使用这里选择的代理。</p>
          </div>
          <button type="submit" disabled={busy} className={`${buttonClass} w-full bg-accent text-on-accent hover:bg-accent-hover`}>
            {busy && <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin motion-reduce:animate-none" />}
            {busy ? '正在发起授权…' : finished ? '重新生成授权链接' : '生成 Google 授权链接'}
          </button>
        </form>
      )}

      {active && (
        <div className="mt-5 space-y-4">
          <div role="status" className="flex items-center gap-2 text-sm text-accent">
            <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin motion-reduce:animate-none" />
            {session.status === 'starting' ? '正在生成 Google 授权链接…' : session.status === 'verifying' ? '正在验证授权并保存凭据…' : `等待 ${session.name} 完成 Google 授权`}
          </div>
          {authorizationUrl && session.status === 'awaiting_code' && (
            <>
              <p className="text-sm leading-6 text-secondary">请登录此槽位对应的 Google 账号，并勾选全部请求的权限。链接有效期约 {Math.ceil((session.expires_at - session.created_at) / 60)} 分钟。</p>
              <div className="flex flex-wrap gap-2">
                <a href={authorizationUrl} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer"
                  className={`${buttonClass} bg-accent text-on-accent hover:bg-accent-hover`}>
                  <ExternalLink aria-hidden="true" className="h-4 w-4" />打开 Google 授权
                </a>
                <button type="button" onClick={async () => {
                  try { await navigator.clipboard.writeText(authorizationUrl); setCopied(true) }
                  catch { setError('无法复制，请选择下方链接手动复制，或直接点击打开授权。') }
                }} className={`${buttonClass} border border-control text-ink hover:bg-hover-surface`}>
                  {copied ? <Check aria-hidden="true" className="h-4 w-4" /> : <Copy aria-hidden="true" className="h-4 w-4" />}
                  {copied ? '已复制链接' : '复制链接'}
                </button>
              </div>
              <label htmlFor="account-auth-link" className="block text-xs text-muted">Google 授权链接</label>
              <input id="account-auth-link" readOnly value={authorizationUrl} onFocus={(e) => e.target.select()}
                className={`${inputClass} font-mono text-xs`} />
              <form onSubmit={submitCode} className="space-y-3">
                <div>
                  <label htmlFor="account-auth-code" className="mb-1.5 block text-sm text-ink">Google 授权码</label>
                  <input id="account-auth-code" type="password" value={code} onChange={(e) => setCode(e.target.value)}
                    required maxLength={4096} autoComplete="off" spellCheck={false} disabled={busy}
                    placeholder="粘贴 Google 页面显示的完整授权码" className={inputClass} />
                </div>
                <button type="submit" disabled={busy || !code.trim()} className={`${buttonClass} w-full bg-accent text-on-accent hover:bg-accent-hover`}>
                  {busy ? '正在提交…' : '完成授权并加入账号池'}
                </button>
              </form>
            </>
          )}
        </div>
      )}

      {success && (
        <div role="status" className="mt-5 rounded-xl border border-success-line bg-success-soft p-4 text-sm leading-6 text-success">
          <div className="flex items-center gap-2 font-semibold"><ShieldCheck aria-hidden="true" className="h-5 w-5" />{session.name} 授权成功</div>
          <p className="mt-1">凭据已保存到该账号的独立目录，账号池已更新。</p>
        </div>
      )}
      {(error || session?.error) && <p role="alert" className="mt-4 rounded-xl border border-danger-line bg-danger-soft p-3 text-sm text-danger">{error || session?.error}</p>}
      {pollError && <p role="status" className="mt-3 text-sm text-warning">{pollError}</p>}
      <button type="button" disabled={busy} onClick={() => void close()}
        className={`${buttonClass} mt-4 w-full border border-control text-ink hover:bg-hover-surface`}>
        {success ? '完成' : active ? '取消授权' : '关闭'}
      </button>
    </dialog>
  )
}

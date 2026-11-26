import { useCallback, useEffect, useState } from 'react'
import { KeyRound, Network, Plus, RefreshCw, ShieldCheck, Trash2, X } from 'lucide-react'
import {
  createProxy,
  deleteProxy,
  fetchAccounts,
  fetchProxies,
  updateAccount,
  updateProxy,
} from '../api/client'
import { SectionCard } from '../components/StatusBadge'
import { useToast } from '../components/Toast'
import type { Account, ProxyConfig } from '../types'

const inputClass = 'w-full rounded-xl border border-control bg-inset px-3 py-2.5 text-sm text-ink focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent-ring disabled:opacity-60'

export function ProxiesPage() {
  const { pushToast } = useToast()
  const [proxies, setProxies] = useState<ProxyConfig[]>([])
  const [accounts, setAccounts] = useState<Account[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [assigning, setAssigning] = useState<string | null>(null)
  const [editorOpen, setEditorOpen] = useState(false)
  const [editing, setEditing] = useState<ProxyConfig | null>(null)
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')

  const load = useCallback(async () => {
    try {
      const [proxyData, accountData] = await Promise.all([fetchProxies(), fetchAccounts()])
      setProxies(proxyData.proxies)
      setAccounts(accountData.accounts)
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '加载代理配置失败')
    } finally {
      setLoading(false)
    }
  }, [pushToast])

  useEffect(() => { void load() }, [load])

  const openCreate = () => {
    setEditing(null)
    setName('')
    setUrl('')
    setEditorOpen(true)
  }

  const openEdit = (proxy: ProxyConfig) => {
    setEditing(proxy)
    setName(proxy.name)
    setUrl('')
    setEditorOpen(true)
  }

  const saveProxy = async (event: React.FormEvent) => {
    event.preventDefault()
    setSaving(true)
    try {
      if (editing) {
        await updateProxy(editing.proxy_id, { name: name.trim(), ...(url.trim() ? { url: url.trim() } : {}) })
        pushToast('success', `代理「${name.trim()}」已更新`)
      } else {
        await createProxy({ name: name.trim(), url: url.trim() })
        pushToast('success', `代理「${name.trim()}」已添加`)
      }
      setEditorOpen(false)
      await load()
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '保存代理失败')
    } finally {
      setSaving(false)
    }
  }

  const removeProxy = async (proxy: ProxyConfig) => {
    if (proxy.assigned_accounts > 0) {
      pushToast('error', '请先将使用此代理的账号改为其他代理或不使用代理。')
      return
    }
    if (!window.confirm(`确认删除代理「${proxy.name}」？`)) return
    try {
      await deleteProxy(proxy.proxy_id)
      pushToast('success', `代理「${proxy.name}」已删除`)
      await load()
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '删除代理失败')
    }
  }

  const assignProxy = async (account: Account, proxyId: string) => {
    setAssigning(account.name)
    try {
      const updated = await updateAccount(account.name, { proxy_id: proxyId || null })
      setAccounts((current) => current.map((item) => item.name === account.name ? updated : item))
      pushToast('success', proxyId ? `已为 ${account.name} 分配代理` : `已移除 ${account.name} 的专属代理`)
      const refreshed = await fetchProxies()
      setProxies(refreshed.proxies)
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '更新账号代理失败')
    } finally {
      setAssigning(null)
    }
  }

  const busy = (account: Account) => account.status === 'busy' || account.status === 'maintenance'

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="page-title">代理管理</h1>
          <p className="page-description">
            为每个账号单独配置服务端出站代理；授权、用量查询和 Colab CLI 任务均使用该账号的代理。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button type="button" onClick={() => { setLoading(true); void load() }} className="button-secondary">
            <RefreshCw aria-hidden="true" className={`h-3.5 w-3.5 ${loading ? 'animate-spin motion-reduce:animate-none' : ''}`} />刷新
          </button>
          <button type="button" onClick={openCreate} className="button-primary">
            <Plus aria-hidden="true" className="h-4 w-4" />添加代理
          </button>
        </div>
      </div>

      <SectionCard
        title={`代理列表 (${proxies.length})`}
        subtitle="支持 HTTP / HTTPS 代理，可在地址中填写 user:password@；保存后凭据不会再次显示。"
      >
        {loading && proxies.length === 0 ? (
          <div className="py-8 text-center text-sm text-muted">正在加载代理配置…</div>
        ) : proxies.length === 0 ? (
          <div className="rounded-xl border border-dashed border-control bg-inset px-4 py-8 text-center">
            <Network aria-hidden="true" className="mx-auto h-6 w-6 text-muted" />
            <p className="mt-2 text-sm font-medium text-secondary">还没有代理</p>
            <p className="mt-1 text-xs text-muted">先添加代理，再在下方为账号分配。</p>
          </div>
        ) : (
          <div className="divide-y divide-line">
            {proxies.map((proxy) => (
              <div key={proxy.proxy_id} className="flex flex-wrap items-center justify-between gap-3 py-3 first:pt-0 last:pb-0">
                <div className="flex min-w-0 items-center gap-3">
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent">
                    <Network aria-hidden="true" className="h-4 w-4" />
                  </span>
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium text-ink">{proxy.name}</span>
                      {proxy.has_auth && <span className="inline-flex items-center gap-1 rounded-md bg-subtle px-1.5 py-0.5 text-[11px] text-secondary"><KeyRound className="h-3 w-3" />带认证</span>}
                    </div>
                    <div className="mt-0.5 break-all font-mono text-xs text-muted">{proxy.endpoint}</div>
                    <div className="mt-1 text-xs text-muted">已分配 {proxy.assigned_accounts} 个账号</div>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button type="button" onClick={() => openEdit(proxy)} className="button-secondary">编辑</button>
                  <button type="button" onClick={() => void removeProxy(proxy)} disabled={proxy.assigned_accounts > 0}
                    title={proxy.assigned_accounts > 0 ? '请先解除账号分配' : '删除代理'}
                    className="inline-flex min-h-[38px] items-center gap-1.5 rounded-lg border border-line px-3 text-xs text-muted hover:border-danger-line hover:text-danger disabled:cursor-not-allowed disabled:opacity-40">
                    <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />删除
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </SectionCard>

      <SectionCard
        title={`账号代理分配 (${accounts.length})`}
        subtitle="账号正在运行任务或维护时不可更改。选择「沿用服务器网络」时，不设置账号专属代理。"
      >
        {accounts.length === 0 ? (
          <div className="py-8 text-center text-sm text-muted">暂无账号，请先添加并授权账号。</div>
        ) : (
          <div className="space-y-2">
            {accounts.map((account) => (
              <div key={account.name} className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-line bg-inset px-3.5 py-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-sm font-semibold text-ink">{account.name}</span>
                    <span className="text-xs text-muted">{account.status === 'busy' ? '运行中' : account.status === 'maintenance' ? '维护中' : account.status}</span>
                  </div>
                  <div className="mt-1 flex items-center gap-1.5 text-xs text-muted">
                    <ShieldCheck aria-hidden="true" className="h-3.5 w-3.5" />
                    当前：{account.proxy_name || '沿用服务器网络'}
                  </div>
                </div>
                <label className="flex items-center gap-2 text-xs text-secondary">
                  <span>使用代理</span>
                  <select aria-label={`${account.name} 使用的代理`}
                    value={account.proxy_id || ''}
                    disabled={busy(account) || assigning === account.name || proxies.length === 0}
                    onChange={(event) => void assignProxy(account, event.target.value)}
                    className="min-h-10 max-w-[min(70vw,18rem)] rounded-lg border border-control bg-surface px-2.5 py-2 text-sm text-ink disabled:opacity-50">
                    <option value="">沿用服务器网络</option>
                    {proxies.map((proxy) => <option key={proxy.proxy_id} value={proxy.proxy_id}>{proxy.name} · {proxy.endpoint}</option>)}
                  </select>
                </label>
              </div>
            ))}
            {proxies.length === 0 && <p className="px-1 pt-2 text-xs text-muted">添加代理后即可分配给账号。</p>}
          </div>
        )}
      </SectionCard>

      {editorOpen && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-scrim p-4" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !saving) setEditorOpen(false) }}>
          <section role="dialog" aria-modal="true" aria-labelledby="proxy-editor-title" className="w-full max-w-lg rounded-2xl border border-line bg-surface p-5 shadow-floating sm:p-6">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 id="proxy-editor-title" className="text-lg font-semibold text-ink">{editing ? '编辑代理' : '添加代理'}</h2>
                <p className="mt-1 text-xs leading-relaxed text-muted">代理地址仅用于服务端 Colab CLI 出站请求。</p>
              </div>
              <button type="button" aria-label="关闭" disabled={saving} onClick={() => setEditorOpen(false)} className="rounded-lg p-2 text-muted hover:bg-subtle disabled:opacity-50"><X aria-hidden="true" className="h-4 w-4" /></button>
            </div>
            <form onSubmit={(event) => void saveProxy(event)} className="mt-5 space-y-4">
              <div>
                <label htmlFor="proxy-name" className="mb-1.5 block text-sm font-medium text-ink">代理名称</label>
                <input id="proxy-name" required maxLength={64} value={name} onChange={(event) => setName(event.target.value)} disabled={saving} placeholder="例如：东京节点 01" className={inputClass} />
              </div>
              <div>
                <label htmlFor="proxy-url" className="mb-1.5 block text-sm font-medium text-ink">代理地址</label>
                <input id="proxy-url" type="password" required={!editing} autoComplete="new-password" value={url} onChange={(event) => setUrl(event.target.value)} disabled={saving}
                  placeholder={editing ? '留空则保持现有地址与认证信息' : 'http://user:password@host:port'} className={`${inputClass} font-mono`} />
                <p className="mt-1.5 text-xs leading-relaxed text-muted">仅支持 http:// 或 https://；带认证时可用 user:password@host:port 格式。凭据保存在服务端 SQLite 中（未加密），请保护数据目录与备份；接口不会返回认证信息。</p>
              </div>
              <div className="flex justify-end gap-2 pt-1">
                <button type="button" disabled={saving} onClick={() => setEditorOpen(false)} className="button-secondary">取消</button>
                <button type="submit" disabled={saving || !name.trim() || (!editing && !url.trim())} className="button-primary disabled:opacity-50">{saving ? '正在保存…' : '保存代理'}</button>
              </div>
            </form>
          </section>
        </div>
      )}
    </div>
  )
}

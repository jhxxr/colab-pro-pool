import { useCallback, useEffect, useRef, useState } from 'react'
import { Copy, KeyRound, Plus, RefreshCw, ShieldCheck } from 'lucide-react'
import { createMcpKey, fetchMcpKeys, fetchResources, updateMcpKey } from '../api/client'
import { SectionCard } from '../components/StatusBadge'
import { useToast } from '../components/Toast'
import type { ComputeResource, McpKey } from '../types'

const inputClass = 'mt-2 min-h-11 w-full rounded-xl border border-control bg-inset px-3 py-2.5 text-sm text-ink'
const cu = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 4 })

export function KeysPage() {
  const { pushToast } = useToast()
  const [keys, setKeys] = useState<McpKey[]>([])
  const [resources, setResources] = useState<ComputeResource[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [editor, setEditor] = useState<McpKey | 'new' | null>(null)
  const [name, setName] = useState('')
  const [limit, setLimit] = useState('100')
  const [models, setModels] = useState<string[]>(['T4'])
  const [secret, setSecret] = useState('')
  const nameRef = useRef<HTMLInputElement>(null)

  const load = useCallback(async () => {
    try {
      const [data, catalog] = await Promise.all([fetchMcpKeys(), fetchResources()])
      setKeys(data.keys)
      setResources(catalog.resources)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载 MCP 密钥失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
    const timer = window.setInterval(() => { void load() }, 10000)
    return () => window.clearInterval(timer)
  }, [load])
  useEffect(() => { if (editor) nameRef.current?.focus() }, [editor])

  const edit = (key: McpKey | 'new') => {
    setEditor(key)
    setName(key === 'new' ? '' : key.name)
    setLimit(key === 'new' ? '100' : String(key.cu_limit))
    setModels(key === 'new' ? ['T4'] : key.allowed_models)
  }

  const save = async (event: React.FormEvent) => {
    event.preventDefault()
    const value = Number(limit)
    if (!name.trim() || !limit.trim() || !Number.isFinite(value) || value < 0 || models.length === 0) {
      pushToast('error', '请填写名称、有效的 CU 总额度，并至少选择一种机型。')
      return
    }
    if (!editor) return
    setBusy(true)
    try {
      const input = { name: name.trim(), cu_limit: value, allowed_models: models }
      if (editor === 'new') {
        const created = await createMcpKey(input)
        setSecret(created.token)
        pushToast('success', 'MCP 密钥已创建，请复制保存完整密钥。')
      } else {
        await updateMcpKey(editor.key_id, input)
        pushToast('success', '密钥额度与机型权限已更新。')
      }
      setEditor(null)
      await load()
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '保存密钥失败')
    } finally {
      setBusy(false)
    }
  }

  const toggle = async (key: McpKey) => {
    setBusy(true)
    try {
      await updateMcpKey(key.key_id, { enabled: !key.enabled })
      pushToast('success', key.enabled ? '密钥已停用，运行中任务将停止。' : '密钥已启用。')
      await load()
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '更新密钥失败')
    } finally {
      setBusy(false)
    }
  }

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      pushToast('success', '已复制。')
    } catch {
      pushToast('error', '无法访问剪贴板，请手动选择并复制。')
    }
  }

  const config = JSON.stringify({ mcpServers: { 'colab-pro-pool': {
    command: 'colab-pool-mcp', env: {
      COLAB_POOL_SERVER_URL: window.location.origin, COLAB_POOL_API_KEY: secret,
    },
  } } }, null, 2)

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="page-title">MCP 密钥</h1>
          <p className="page-description">为不同成员或 Agent 分配独立密钥，设置累计 CU 总额度与可用机型。</p>
        </div>
        <div className="flex gap-2">
          <button type="button" className="button-secondary min-h-11" disabled={loading} onClick={() => { setLoading(true); void load() }}>
            <RefreshCw aria-hidden="true" className={`h-4 w-4 ${loading ? 'animate-spin motion-reduce:animate-none' : ''}`} />刷新
          </button>
          <button type="button" className="button-primary min-h-11 disabled:opacity-50" disabled={busy || !!secret || !!editor} onClick={() => edit('new')}>
            <Plus aria-hidden="true" className="h-4 w-4" />创建密钥
          </button>
        </div>
      </div>

      {error && <p role="alert" className="rounded-xl border border-danger-line bg-danger-soft p-4 text-sm text-danger">{error}</p>}

      {secret && <SectionCard title="请保存新密钥" subtitle="完整密钥仅在创建时显示一次；服务端仅存储哈希。离开此页面后无法找回。">
        <code className="block break-all rounded-xl border border-control bg-inset p-4 text-sm text-ink select-all">{secret}</code>
        <div className="mt-4 flex flex-wrap gap-2">
          <button type="button" className="button-primary min-h-11" onClick={() => void copy(secret)}><Copy aria-hidden="true" className="h-4 w-4" />复制密钥</button>
          <button type="button" className="button-secondary min-h-11" onClick={() => void copy(config)}>复制 MCP 配置</button>
          <button type="button" className="button-secondary min-h-11" onClick={() => setSecret('')}>已保存，关闭</button>
        </div>
      </SectionCard>}

      {editor && <SectionCard className="[&_h2]:break-all" title={editor === 'new' ? '创建 MCP 密钥' : `编辑「${editor.name}」`} subtitle="总额度是累计上限：例如已用 20 CU，将总额度改为 120 后，还可使用 100 CU。">
        <form onSubmit={(event) => void save(event)}>
          <fieldset disabled={busy} className="space-y-5 disabled:opacity-60">
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="text-sm text-secondary">密钥名称
                <input ref={nameRef} className={inputClass} required maxLength={64} autoComplete="off" value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：实验室成员 A" />
              </label>
              <label className="text-sm text-secondary">累计 CU 总额度
                <input className={inputClass} type="number" required min="0" step="any" inputMode="decimal" value={limit} onChange={(event) => setLimit(event.target.value)} aria-describedby="key-quota-help" />
              </label>
            </div>
            <p id="key-quota-help" className="text-xs text-muted">额度设为 0 会阻止运行；增加总额度即可续额，已用 CU 保留。没有自动重置周期。</p>
            <fieldset>
              <legend className="text-sm font-medium text-ink">允许使用的机型（至少选择一种）</legend>
              <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
                {resources.map((resource) => <label key={resource.id} className="flex min-h-11 cursor-pointer items-center gap-2 rounded-xl border border-control bg-inset px-3 py-2 text-sm text-secondary">
                  <input type="checkbox" checked={models.includes(resource.id)} onChange={(event) => setModels((current) => event.target.checked ? [...current, resource.id] : current.filter((model) => model !== resource.id))} />
                  <span>{resource.id} <span className="text-xs text-muted">{resource.kind}</span></span>
                </label>)}
              </div>
              {!resources.length && <p className="mt-2 text-sm text-muted">请等待机型目录加载成功后再保存。</p>}
            </fieldset>
            <p className="text-xs text-muted">收回正在使用的机型权限或降低总额度至已用 CU 以下，会停止相关任务。</p>
            <div className="flex flex-wrap gap-2">
              <button type="submit" disabled={!models.length || !resources.length} className="button-primary min-h-11">{busy ? '保存中…' : editor === 'new' ? '生成密钥' : '保存修改'}</button>
              <button type="button" className="button-secondary min-h-11" onClick={() => setEditor(null)}>取消</button>
            </div>
          </fieldset>
        </form>
      </SectionCard>}

      <SectionCard title={`密钥列表 (${keys.length})`} subtitle="每个密钥只能查看、重试和取消自己的任务。管理员密钥继续用于控制台管理。">
        {loading && !keys.length ? <p role="status" className="py-8 text-center text-sm text-muted">正在加载密钥…</p>
          : !keys.length && !error ? <div className="py-8 text-center">
            <KeyRound aria-hidden="true" className="mx-auto h-6 w-6 text-muted" />
            <p className="mt-3 text-sm text-secondary">还没有 MCP 密钥</p>
            <p className="mt-1 text-xs text-muted">创建密钥后，将其填入 Agent 的 COLAB_POOL_API_KEY。</p>
          </div> : <div className="divide-y divide-line">{keys.map((key) => <div key={key.key_id} className="flex flex-wrap items-start justify-between gap-4 py-4 first:pt-0 last:pb-0">
            <div className="min-w-0 flex-1 basis-64">
              <div className="flex flex-wrap items-center gap-2">
                <span className="break-all font-medium text-ink">{key.name}</span>
                <span className={`text-xs ${!key.enabled ? 'text-muted' : key.cu_remaining <= 0 ? 'text-danger' : 'text-success'}`}>{!key.enabled ? '已停用' : key.cu_remaining <= 0 ? '额度耗尽' : '已启用'}</span>
              </div>
              <code className="mt-1 block text-xs text-muted">{key.token_prefix}…</code>
              <p className="mt-3 text-sm text-secondary tabular-nums">剩余 <strong className="font-semibold text-ink">{cu(key.cu_remaining)} CU</strong><span className="mx-2 text-muted">/</span>总额度 {cu(key.cu_limit)} CU</p>
              <p className="mt-1 text-xs text-muted">已用 {cu(key.cu_used)} CU · 允许机型：{key.allowed_models.join('、')}</p>
              <p className="mt-1 text-xs text-muted">最近使用：{key.last_used_at ? new Date(key.last_used_at * 1000).toLocaleString('zh-CN') : '尚未使用'}</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <button type="button" className="button-secondary min-h-11" disabled={busy || !!editor} onClick={() => edit(key)} aria-label={`编辑 ${key.name} 的额度和机型`}>编辑额度与机型</button>
              <button type="button" className="button-secondary min-h-11" disabled={busy} onClick={() => void toggle(key)} aria-label={`${key.enabled ? '停用' : '启用'} ${key.name}`}>{key.enabled ? '停用' : '启用'}</button>
            </div>
          </div>)}</div>}
      </SectionCard>

      <div className="flex items-start gap-3 rounded-xl border border-line bg-inset p-4 text-xs leading-relaxed text-muted">
        <ShieldCheck aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
        <p>CU 按官方 CLI 查询的消耗速率与实例占用时长估算，并按账号余额减少向上校正。额度耗尽、停用或权限收回后会停止任务；查询与停机存在延迟，并行任务可能超额。无法确认单实例用量时停止任务。密钥不具备账号、代理、密钥管理或管理员云盘档案的访问权限。</p>
      </div>
    </div>
  )
}

import { useState } from 'react'
import { Check, Copy, KeyRound, LogOut, ShieldCheck, Terminal, Wifi } from 'lucide-react'
import {
  fetchAuthCheck,
  getStoredApiKey,
  setStoredApiKey,
} from '../api/client'
import { SectionCard } from '../components/StatusBadge'
import { useToast } from '../components/Toast'

interface ConnectPageProps {
  authRequired: boolean
  onApiKeyUpdated: () => void
  onSignOut: () => void
}

function CopyBlock({ title, code }: { title: string; code: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    await navigator.clipboard.writeText(code)
    setCopied(true)
    setTimeout(() => setCopied(false), 1800)
  }
  return (
    <div className="rounded-xl border border-line bg-editor p-3.5">
      <div className="mb-2 flex items-center justify-between text-xs">
        <span className="font-medium text-secondary">{title}</span>
        <button
          type="button"
          onClick={copy}
          className="inline-flex items-center gap-1 rounded-lg border border-control bg-surface px-2.5 py-1 text-xs text-ink hover:bg-hover-surface"
        >
          {copied ? <Check className="h-3 w-3 text-success" /> : <Copy className="h-3 w-3" />}
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre className="overflow-x-auto font-mono text-xs leading-relaxed text-secondary">
        {code}
      </pre>
    </div>
  )
}

export function ConnectPage({
  authRequired,
  onApiKeyUpdated,
  onSignOut,
}: ConnectPageProps) {
  const { pushToast } = useToast()
  const [newKey, setNewKey] = useState(getStoredApiKey())
  const [saving, setSaving] = useState(false)

  const origin = typeof window !== 'undefined' ? window.location.origin : 'http://127.0.0.1:8080'
  const port = typeof window !== 'undefined' && window.location.port ? window.location.port : '8080'
  const apiKeyDisplay = 'cpool_替换为在_MCP密钥_页面创建的密钥'

  const sshCmd = `ssh -N -L ${port}:127.0.0.1:${port} user@your-linux-server-ip`
  const sshConfig = `Host colab-pool-srv
    HostName your-linux-server-ip
    User your-username
    LocalForward ${port} 127.0.0.1:${port}
    ServerAliveInterval 30`

  const caddyConfig = `colab-pool.yourdomain.com {
    reverse_proxy 127.0.0.1:${port} {
        flush_interval -1
    }
}`

  const mcpJson = JSON.stringify(
    {
      mcpServers: {
        'colab-pro-pool': {
          command: 'colab-pool-mcp',
          env: {
            COLAB_POOL_SERVER_URL: origin,
            COLAB_POOL_API_KEY: apiKeyDisplay,
          },
        },
      },
    },
    null,
    2
  )

  const handleSaveKey = async (e: React.FormEvent) => {
    e.preventDefault()
    setSaving(true)
    try {
      const check = await fetchAuthCheck(newKey.trim())
      if (check.auth_required && !check.authenticated) {
        pushToast('error', 'API Key 验证失败，请核对服务端配置')
        return
      }
      setStoredApiKey(newKey.trim())
      pushToast('success', '已更新本地存储的 API Key')
      onApiKeyUpdated()
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '验证失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="page-title">本地连接与 Agent 接入</h1>
        <p className="page-description">
          基于当前控制台地址（<code className="font-mono text-secondary">{origin}</code>）自动生成本地隧道与 MCP 配置
        </p>
      </div>

      <SectionCard
        title="1. 本地连接远端服务器的 3 种安全方式"
        subtitle="Web 控制台、HTTP REST API、SSE 日志流与 MCP 全部共用同一端口，打通端口即可"
      >
        <div className="space-y-4">
          <div className="rounded-xl border border-success-line bg-success-soft p-3.5 text-xs text-success">
            <div className="flex items-center gap-2 font-semibold text-success">
              <Wifi className="h-4 w-4" />
              推荐方案 A：SSH 端口转发（零公网暴露，自带加密）
            </div>
            <p className="mt-1 leading-relaxed text-secondary">
              服务端在 <code className="text-secondary">docker-compose.yml</code> 默认仅绑定{' '}
              <code className="text-secondary">127.0.0.1:{port}</code>。在本地终端建立 SSH 隧道后，浏览器直接打开{' '}
              <code className="text-secondary">http://127.0.0.1:{port}</code> 即可。
            </p>
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <CopyBlock title="单次 SSH 隧道命令 (Windows PowerShell / macOS / Linux)" code={sshCmd} />
            <CopyBlock title="写入 ~/.ssh/config 免手输命令" code={sshConfig} />
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div className="rounded-xl border border-line bg-inset p-4 text-xs text-secondary">
              <div className="font-semibold text-ink">方案 B：Tailscale / 内网组网（课题组多人共用）</div>
              <p className="mt-1.5 leading-relaxed text-muted">
                服务器和组内成员电脑均接入 Tailscale 后，在{' '}
                <code className="text-secondary">.env</code> 设置{' '}
                <code className="text-secondary">COLAB_POOL_BIND_ADDR=0.0.0.0</code> 并仅放行{' '}
                <code className="text-secondary">tailscale0</code> 网卡端口，浏览器与 MCP 统一填服务器 Tailscale IP（如{' '}
                <code className="text-secondary">http://100.x.y.z:{port}</code>）。
              </p>
            </div>
            <CopyBlock
              title="方案 C：公网域名 + Caddy HTTPS（开启 SSE 流式不缓冲）"
              code={caddyConfig}
            />
          </div>
        </div>
      </SectionCard>

      <SectionCard
        title="2. AI Agent MCP 接入配置"
        subtitle="先在「MCP 密钥」页面创建受限密钥，再替换配置中的 COLAB_POOL_API_KEY；适用于 Claude Code、Cursor、Codex、Hermes Agent 等客户端。"
      >
        <div className="space-y-4">
          <CopyBlock title="MCP Server 配置 JSON (~/.claude.json / mcp.json)" code={mcpJson} />
          <div className="rounded-xl border border-line bg-inset p-3.5 text-xs text-secondary">
            <div className="mb-1.5 flex items-center gap-1.5 font-medium text-ink">
              <Terminal className="h-3.5 w-3.5 text-accent" />
              可用 MCP 工具清单
            </div>
            <div className="grid grid-cols-1 gap-1.5 font-mono text-xs text-muted sm:grid-cols-2 lg:grid-cols-3">
              <div>· colab_pool_status (账号池与档案)</div>
              <div>· submit_colab_job (提交代码/目录)</div>
              <div>· list_colab_jobs (列举与过滤任务)</div>
              <div>· get_colab_job (实时状态与日志)</div>
              <div>· retry_colab_job (失败一键重试)</div>
              <div>· download_colab_artifacts (拉回产物)</div>
              <div>· cancel_colab_job (停止并销毁 VM)</div>
            </div>
          </div>
        </div>
      </SectionCard>

      <SectionCard
        title="3. 当前浏览器 API Key 会话"
        subtitle={authRequired ? '当前服务端要求 API Key 鉴权' : '当前服务端未强制开启 API Key（公开模式）'}
      >
        <form onSubmit={handleSaveKey} className="flex flex-wrap items-end gap-3">
          <div className="w-full min-w-0 flex-1 sm:min-w-[260px]">
            <label htmlFor="connection-api-key" className="mb-1 block text-xs text-muted">本地保存的 Bearer API Key</label>
            <div className="relative">
              <KeyRound className="pointer-events-none absolute top-1/2 left-3 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
              <input
                id="connection-api-key"
                type="password"
                value={newKey}
                onChange={(e) => setNewKey(e.target.value)}
                placeholder="输入 COLAB_POOL_API_KEY"
                className="w-full rounded-xl border border-control bg-inset py-2 pr-3 pl-9 font-mono text-xs text-ink outline-none focus:border-accent"
              />
            </div>
          </div>
          <button
            type="submit"
            disabled={saving}
            className="inline-flex items-center gap-1.5 rounded-xl bg-accent px-4 py-2 text-xs font-semibold text-on-accent hover:bg-accent-hover disabled:opacity-50"
          >
            <ShieldCheck className="h-3.5 w-3.5" />
            {saving ? '验证中...' : '保存并验证'}
          </button>
          {authRequired && (
            <button
              type="button"
              onClick={onSignOut}
              className="inline-flex items-center gap-1.5 rounded-xl border border-control bg-surface px-4 py-2 text-xs text-secondary hover:border-danger-line hover:text-danger"
            >
              <LogOut className="h-3.5 w-3.5" />
              退出解锁状态
            </button>
          )}
        </form>
      </SectionCard>
    </div>
  )
}

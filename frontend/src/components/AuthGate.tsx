import { useState } from 'react'
import { Cpu, KeyRound, Lock, ShieldCheck } from 'lucide-react'
import { fetchAuthCheck, setStoredApiKey } from '../api/client'

interface AuthGateProps {
  initialError?: string
  onAuthenticated: () => void
}

export function AuthGate({ initialError = '', onAuthenticated }: AuthGateProps) {
  const [apiKey, setApiKey] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(initialError)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    const trimmed = apiKey.trim()
    if (!trimmed) {
      setError('请输入服务端配置的 COLAB_POOL_API_KEY')
      return
    }
    setLoading(true)
    setError('')
    try {
      const check = await fetchAuthCheck(trimmed)
      if (!check.authenticated) {
        setError('API Key 不匹配，请核对服务端环境变量 COLAB_POOL_API_KEY')
        return
      }
      setStoredApiKey(trimmed)
      onAuthenticated()
    } catch (err) {
      setError(err instanceof Error ? err.message : '连接服务端失败，请检查服务是否已启动')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex min-h-dvh items-center justify-center bg-canvas px-5 py-12">
      <div className="w-full max-w-[420px] rounded-3xl border border-line bg-surface p-7 shadow-panel sm:p-10">
        <div className="mb-8 text-center">
          <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-2xl border border-line bg-inset text-secondary">
            <Cpu aria-hidden="true" className="h-7 w-7" strokeWidth={1.5} />
          </div>
          <p className="mb-2 text-xs font-medium text-muted">Colab Pro Pool</p>
          <h1 className="text-2xl font-semibold tracking-tight text-ink">连接你的工作区</h1>
          <p className="mt-3 text-sm leading-relaxed text-muted">输入访问密钥，开始管理云端算力与实验。</p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="api-key-input" className="mb-1.5 block text-xs font-medium text-secondary">
              API Key
            </label>
            <div className="relative">
              <KeyRound className="pointer-events-none absolute top-1/2 left-3.5 h-4 w-4 -translate-y-1/2 text-muted" />
              <input
                id="api-key-input"
                type="password"
                name="api-key"
                autoComplete="current-password"
                autoFocus
                disabled={loading}
                aria-invalid={!!error}
                aria-describedby={error ? 'api-key-error' : undefined}
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="输入 API Key"
                className="w-full rounded-xl border border-control bg-surface py-3 pr-3.5 pl-10 text-base text-ink outline-none focus:border-accent focus:ring-1 focus:ring-accent-ring"
              />
            </div>
          </div>

          {error && (
            <div id="api-key-error" role="alert" className="rounded-xl border border-danger-line bg-danger-soft px-3.5 py-2.5 text-xs text-danger">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="button-primary min-h-11 w-full disabled:cursor-not-allowed disabled:opacity-60"
          >
            <ShieldCheck aria-hidden="true" className="h-4 w-4" />
            {loading ? '正在连接…' : '连接工作区'}
          </button>
        </form>
        <p className="mt-6 flex items-center justify-center gap-1.5 text-xs text-muted">
          <Lock aria-hidden="true" className="h-3.5 w-3.5" />
          访问密钥仅保存在当前浏览器中
        </p>
      </div>
    </div>
  )
}

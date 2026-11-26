import { useState } from 'react'
import { RefreshCw } from 'lucide-react'
import type { Account } from '../types'
import { formatRelativeTime, formatTimestamp } from '../utils/format'
import { detectedGpu, hardwareSummary } from '../utils/hardware'
import { hourlyCost, readCuCost } from '../utils/pricing'

const cuFormat = new Intl.NumberFormat('zh-CN', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

export function AccountUsage({
  account,
  nowMs,
  onRefresh,
  requesting = false,
}: {
  account: Account
  nowMs: number
  onRefresh: () => void
  requesting?: boolean
}) {
  const usage = account.usage
  const [cost] = useState(readCuCost)
  const moneyRate = hourlyCost(usage?.consumption_rate_hourly, cost)
  const refreshing = requesting || usage?.refreshing
  const hasData = usage?.remaining_cu != null
  const stale = hasData && (usage.stale || !!usage.error || !account.auth_configured)
  const disabled = refreshing || !account.auth_configured || account.status === 'maintenance'
  const runtime = account.runtime
  const actualModel = runtime ? detectedGpu(runtime.hardware) || runtime.gpu_detected : ''
  const hardware = hardwareSummary(runtime?.hardware)

  let state = '等待查询'
  if (!account.auth_configured) state = hasData ? '待授权 · 上次数据' : '授权后可查询'
  else if (account.status === 'maintenance') state = '维护完成后更新'
  else if (refreshing) state = '正在更新…'
  else if (usage?.error) state = hasData ? '更新失败 · 上次数据' : '查询失败'
  else if (stale) state = '待更新 · 上次数据'
  else if (usage?.updated_at) state = `${formatRelativeTime(usage.updated_at, nowMs)}更新`

  return (
    <div className="mt-3 rounded-xl border border-line bg-surface px-3.5 py-3" aria-busy={refreshing}>
      {runtime && (
        <div className="mb-3 border-b border-line pb-3">
          <div className="text-xs text-muted">{runtime.status === 'provisioning' ? '正在分配机器' : '当前运行机器'}</div>
          <p className="mt-1 text-sm font-semibold text-ink">
            {actualModel || (runtime.hardware?.detected_at ? runtime.tpu ? 'TPU 运行时（芯片型号未确认）' : runtime.gpu === 'CPU' ? 'CPU 运行时' : '未探测到 GPU' : '等待硬件探测')}
          </p>
          <p className="mt-1 text-xs text-muted">
            请求 {runtime.tpu ? `TPU ${runtime.tpu}` : runtime.gpu}{runtime.high_mem ? ' · 高内存' : ''}
          </p>
          {hardware && <p className="mt-1 text-xs text-secondary">{hardware}</p>}
          <p className="mt-1 truncate text-xs text-muted" title={runtime.job_name}>任务：{runtime.job_name}</p>
        </div>
      )}
      <div className="grid grid-cols-2 gap-3">
        <div className="min-w-0">
          <div className="text-xs text-muted">剩余 CU</div>
          <div className="mt-1 flex flex-wrap items-baseline gap-1.5">
            <span className={`text-2xl font-semibold tracking-tight tabular-nums ${stale ? 'text-secondary' : 'text-ink'}`}>
              {usage?.remaining_cu == null ? '—' : cuFormat.format(usage.remaining_cu)}
            </span>
            <span className="text-xs text-muted">CU</span>
          </div>
        </div>
        <div className="min-w-0 border-l border-line pl-3.5">
          <div className="text-xs text-muted">每小时消耗</div>
          <div className="mt-1 flex flex-wrap items-baseline gap-1.5">
            <span className={`text-2xl font-semibold tracking-tight tabular-nums ${stale ? 'text-secondary' : 'text-ink'}`}>
              {usage?.consumption_rate_hourly == null ? '—' : cuFormat.format(usage.consumption_rate_hourly)}
            </span>
            <span className="text-xs text-muted">CU/h</span>
          </div>
          {moneyRate && <p className="mt-1 text-xs text-muted tabular-nums">约 {moneyRate} / 小时</p>}
        </div>
      </div>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-x-2 gap-y-1 text-xs">
        <div className="min-w-0 text-muted" title={usage?.updated_at ? `上次成功查询：${formatTimestamp(usage.updated_at)}` : undefined}>
          <span className={usage?.error || stale ? 'text-warning' : undefined}>{state}</span>
          {usage?.active_assignments != null && <span> · {usage.active_assignments} 个运行实例</span>}
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={disabled}
          aria-label={`刷新 ${account.name} 的 CU 用量`}
          className="inline-flex min-h-9 items-center gap-1.5 rounded-lg px-2 text-accent hover:bg-accent-soft focus-visible:outline-2 focus-visible:outline-accent disabled:text-muted disabled:opacity-60"
        >
          <RefreshCw aria-hidden="true" className={`h-3.5 w-3.5 ${refreshing ? 'animate-spin motion-reduce:animate-none' : ''}`} />
          {refreshing ? '更新中' : '刷新'}
        </button>
      </div>
      {usage?.updated_at && (stale || usage.error) && (
        <p className="mt-1 text-xs text-muted">上次更新：{formatTimestamp(usage.updated_at)}</p>
      )}
      {usage?.error && <p className="mt-1 text-xs text-warning">{usage.error}</p>}
    </div>
  )
}

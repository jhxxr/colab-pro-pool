import { useCallback, useEffect, useState } from 'react'
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Clock,
  Cpu,
  HardDrive,
  Power,
  RefreshCw,
  RotateCcw,
  Server,
} from 'lucide-react'
import {
  fetchAccounts,
  fetchJobs,
  fetchStats,
  refreshAccountUsage,
  updateAccount,
} from '../api/client'
import { AccountUsage } from '../components/AccountUsage'
import {
  AccountStatusBadge,
  GpuBadge,
  JobStatusBadge,
  SectionCard,
  TierBadge,
} from '../components/StatusBadge'
import { useToast } from '../components/Toast'
import type { Account, Job, PoolStats } from '../types'
import {
  formatBytes,
  formatDuration,
  formatRelativeTime,
  jobElapsedSeconds,
} from '../utils/format'

interface OverviewPageProps {
  onOpenJob: (jobId: string) => void
  onGoJobs: () => void
  onGoAccounts: () => void
  onGoSubmit: () => void
}

export function OverviewPage({
  onOpenJob,
  onGoJobs,
  onGoAccounts,
  onGoSubmit,
}: OverviewPageProps) {
  const { pushToast } = useToast()
  const [stats, setStats] = useState<PoolStats | null>(null)
  const [accounts, setAccounts] = useState<Account[]>([])
  const [recentJobs, setRecentJobs] = useState<Job[]>([])
  const [loading, setLoading] = useState(true)
  const [busyAcct, setBusyAcct] = useState<string | null>(null)
  const [nowMs, setNowMs] = useState(() => Date.now())
  const [refreshingNames, setRefreshingNames] = useState<string[]>([])

  const loadData = useCallback(async () => {
    try {
      const [s, a, j] = await Promise.all([
        fetchStats(),
        fetchAccounts(),
        fetchJobs({ limit: 8 }),
      ])
      setStats(s)
      setAccounts(a.accounts)
      setRecentJobs(j.jobs)
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '加载总览数据失败')
    } finally {
      setLoading(false)
    }
  }, [pushToast])

  useEffect(() => {
    loadData()
    const poll = setInterval(loadData, 3000)
    const tick = setInterval(() => setNowMs(Date.now()), 1000)
    return () => {
      clearInterval(poll)
      clearInterval(tick)
    }
  }, [loadData])

  const handleToggleAccount = async (acct: Account) => {
    setBusyAcct(acct.name)
    try {
      const nextEnabled = !acct.enabled
      await updateAccount(acct.name, { enabled: nextEnabled })
      pushToast('success', `账号 ${acct.name} 已${nextEnabled ? '启用' : '停用'}`)
      await loadData()
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '操作失败')
    } finally {
      setBusyAcct(null)
    }
  }

  const handleClearCooldown = async (acct: Account) => {
    setBusyAcct(acct.name)
    try {
      await updateAccount(acct.name, { clear_cooldown: true })
      pushToast('success', `已清除账号 ${acct.name} 冷却状态`)
      await loadData()
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '重置失败')
    } finally {
      setBusyAcct(null)
    }
  }

  const handleRefreshUsage = async (name: string) => {
    setRefreshingNames((names) => [...names, name])
    try {
      const updated = await refreshAccountUsage(name)
      setAccounts((current) => current.map((acct) => acct.name === name ? updated : acct))
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '刷新 CU 用量失败')
    } finally {
      setRefreshingNames((names) => names.filter((item) => item !== name))
    }
  }

  const gpuEntries = Object.entries(stats?.gpu_counts ?? {}).sort((a, b) => b[1] - a[1])
  const maxGpuCount = Math.max(1, ...gpuEntries.map(([, c]) => c))

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="page-title">算力池总览</h1>
          <p className="page-description">
            查看账号容量、实验进度与资源使用情况。
          </p>
        </div>
        <div className="flex items-center gap-2.5">
          <button
            type="button"
            onClick={loadData}
            className="button-secondary"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            刷新
          </button>
          <button
            type="button"
            onClick={onGoSubmit}
            className="button-primary"
          >
            新建实验
            <ArrowRight className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* Quiet, shared metric surface */}
      <div className="section-card grid grid-cols-1 divide-y divide-line sm:grid-cols-2 sm:divide-y-0 xl:grid-cols-4">
        {[
          { label: '可用账号', value: stats?.accounts.idle ?? 0, suffix: `/ ${stats?.accounts.total ?? 0}`, icon: Server,
            detail: `忙碌 ${stats?.accounts.busy ?? 0} · 冷却 ${stats?.accounts.cooldown ?? 0} · 停用 ${stats?.accounts.disabled ?? 0}` },
          { label: '运行中的实验', value: (stats?.jobs.running ?? 0) + (stats?.jobs.provisioning ?? 0), suffix: '', icon: Cpu,
            detail: `等待 ${stats?.jobs.queued ?? 0} · 累计 ${stats?.jobs.total ?? 0}` },
          { label: '任务成功率', value: stats?.success_rate != null ? `${(stats.success_rate * 100).toFixed(1)}%` : '—', suffix: '', icon: CheckCircle2,
            detail: `平均运行 ${formatDuration(stats?.avg_duration_seconds)}` },
          { label: '产物存储', value: formatBytes(stats?.jobs_disk_bytes ?? 0), suffix: '', icon: HardDrive,
            detail: `已连接 ${stats?.drive_profiles_count ?? 0} 个云盘档案` },
        ].map((metric, index) => {
          const Icon = metric.icon
          return (
            <div key={metric.label} className={`min-w-0 px-5 py-6 sm:px-6 ${index > 0 ? 'xl:border-l xl:border-line' : ''} ${index % 2 === 1 ? 'sm:border-l sm:border-line' : ''} ${index > 1 ? 'sm:border-t sm:border-line xl:border-t-0' : ''}`}>
              <div className="flex items-center justify-between gap-2 text-xs text-muted">
                <span>{metric.label}</span><Icon aria-hidden="true" className="h-4 w-4 text-muted" strokeWidth={1.6} />
              </div>
              <div className="mt-3 flex items-baseline gap-2">
                <span className="metric-value">{metric.value}</span>
                {metric.suffix && <span className="text-sm text-muted tabular-nums">{metric.suffix}</span>}
              </div>
              <p className="mt-3 text-xs text-muted">{metric.detail}</p>
            </div>
          )
        })}
      </div>

      {/* Account Pool Matrix */}
      <SectionCard
        title="账号池"
        subtitle="每个账号同时运行一个实验；CU 余额与每小时消耗每分钟自动更新。"
        action={
          <button
            type="button"
            onClick={onGoAccounts}
            className="text-xs font-medium text-accent hover:text-accent"
          >
            管理全部账号 →
          </button>
        }
      >
        {accounts.length === 0 ? (
          <div className="rounded-xl border border-dashed border-line py-10 text-center text-sm text-muted">
            暂无已注册账号。前往「账号与云盘」新增槽位或在服务器执行{' '}
            <code className="rounded bg-subtle px-1.5 py-0.5 text-secondary">
              ./scripts/init_pro_account.sh
            </code>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3.5 md:grid-cols-2 xl:grid-cols-3">
            {accounts.map((acct) => {
              const liveCooldown =
                acct.status === 'cooldown'
                  ? Math.max(0, Math.floor(acct.cooldown_until - nowMs / 1000))
                  : 0
              const isBusy = acct.status === 'busy' || acct.status === 'maintenance'
              const isUpdating = busyAcct === acct.name
              return (
                <div
                  key={acct.name}
                  className="flex flex-col justify-between rounded-xl border border-line bg-inset p-4 transition hover:border-control"
                >
                  <div>
                    <div className="flex items-start justify-between gap-2">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-sm font-semibold text-ink">
                          {acct.name}
                        </span>
                        <TierBadge tier={acct.tier} />
                        {!acct.auth_configured && (
                          <span
                            title="未在 .config/colab-cli 检测到登录凭据"
                            className="inline-flex items-center gap-1 rounded border border-warning-line bg-warning-soft px-1.5 py-0.5 text-[11px] text-warning"
                          >
                            <AlertTriangle className="h-3 w-3" />
                            待授权
                          </span>
                        )}
                      </div>
                      <AccountStatusBadge status={acct.status} enabled={acct.enabled} />
                    </div>

                    <AccountUsage
                      account={acct}
                      nowMs={nowMs}
                      onRefresh={() => void handleRefreshUsage(acct.name)}
                      requesting={refreshingNames.includes(acct.name)}
                    />

                    <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
                      <div className="rounded-lg bg-surface px-2.5 py-1.5">
                        <div className="text-[11px] text-muted">成功运行</div>
                        <div className="font-mono text-ink">{acct.total_runs}</div>
                      </div>
                      <div className="rounded-lg bg-surface px-2.5 py-1.5">
                        <div className="text-[11px] text-muted">失败次数</div>
                        <div className="font-mono text-ink">{acct.total_failures}</div>
                      </div>
                      <div className="rounded-lg bg-surface px-2.5 py-1.5">
                        <div className="text-[11px] text-muted">最近调度</div>
                        <div className="truncate text-secondary">
                          {formatRelativeTime(acct.last_used_at, nowMs)}
                        </div>
                      </div>
                    </div>

                    {acct.current_job_id && (
                      <button
                        type="button"
                        onClick={() => onOpenJob(acct.current_job_id!)}
                        className="mt-3 flex w-full items-center justify-between rounded-lg border border-accent-line bg-accent-soft px-3 py-1.5 text-xs text-accent transition hover:bg-accent-soft-hover"
                      >
                        <span className="truncate font-mono">运行任务: {acct.current_job_id}</span>
                        <ArrowRight className="h-3.5 w-3.5 shrink-0" />
                      </button>
                    )}

                    {acct.status === 'cooldown' && liveCooldown > 0 && (
                      <div className="mt-3 rounded-lg border border-warning-line bg-warning-soft px-3 py-2 text-xs text-warning">
                        <div className="flex items-center justify-between">
                          <span className="flex items-center gap-1">
                            <Clock className="h-3.5 w-3.5" />
                            冷却剩余
                          </span>
                          <span className="font-mono font-semibold">
                            {formatDuration(liveCooldown)}
                          </span>
                        </div>
                      </div>
                    )}

                    {acct.last_error && (
                      <p
                        title={acct.last_error}
                        className="mt-2.5 line-clamp-2 rounded-lg bg-surface px-2.5 py-1.5 font-mono text-xs text-danger"
                      >
                        {acct.last_error}
                      </p>
                    )}
                  </div>

                  <div className="mt-3.5 flex items-center justify-end gap-2 border-t border-line pt-3">
                    {acct.status === 'cooldown' && (
                      <button
                        type="button"
                        disabled={isUpdating}
                        onClick={() => handleClearCooldown(acct)}
                        className="inline-flex items-center gap-1 rounded-lg border border-warning-line bg-warning-soft px-2.5 py-1 text-xs text-warning transition hover:bg-warning-soft-hover disabled:opacity-50"
                      >
                        <RotateCcw className="h-3 w-3" />
                        解除冷却
                      </button>
                    )}
                    <button
                      type="button"
                      disabled={isUpdating || isBusy}
                      onClick={() => handleToggleAccount(acct)}
                      className={`inline-flex items-center gap-1 rounded-lg border px-2.5 py-1 text-xs transition disabled:cursor-not-allowed disabled:opacity-50 ${
                        acct.enabled
                          ? 'border-control bg-surface text-secondary hover:border-danger-line hover:text-danger'
                          : 'border-success-line bg-success-soft text-success hover:bg-success-soft-hover'
                      }`}
                    >
                      <Power className="h-3 w-3" />
                      {acct.enabled ? '停用' : '启用'}
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </SectionCard>

      {/* Bottom grid: Recent Jobs + GPU Distribution */}
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
        <SectionCard
          className="xl:col-span-2"
          title="最近任务"
          subtitle="继续查看最近的实验与运行结果。"
          action={
            <button
              type="button"
              onClick={onGoJobs}
              className="text-xs font-medium text-accent hover:text-accent"
            >
              全部任务 →
            </button>
          }
        >
          {recentJobs.length === 0 ? (
            <div className="py-10 text-center text-sm text-muted">暂无任务记录</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="data-table w-full text-left text-xs">
                <thead>
                  <tr className="border-b border-line text-muted">
                    <th className="pb-2.5 font-medium">任务</th>
                    <th className="pb-2.5 font-medium">加速器</th>
                    <th className="pb-2.5 font-medium">账号</th>
                    <th className="pb-2.5 font-medium">状态</th>
                    <th className="pb-2.5 text-right font-medium">耗时</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {recentJobs.map((job) => (
                    <tr
                      key={job.job_id}
                      onClick={() => onOpenJob(job.job_id)}
                      className="cursor-pointer transition hover:bg-hover-surface"
                    >
                      <td className="py-2.5 pr-3">
                        <div className="font-medium text-ink">{job.name}</div>
                        <div className="font-mono text-xs text-muted">
                          {job.job_id} · {job.entrypoint}
                        </div>
                      </td>
                      <td className="py-2.5 pr-3">
                        <GpuBadge gpu={job.gpu} tpu={job.tpu} highMem={job.high_mem} />
                      </td>
                      <td className="py-2.5 pr-3 font-mono text-secondary">
                        {job.assigned_account || '—'}
                      </td>
                      <td className="py-2.5 pr-3">
                        <JobStatusBadge status={job.status} />
                      </td>
                      <td className="py-2.5 text-right font-mono text-muted">
                        {formatDuration(jobElapsedSeconds(job, nowMs))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </SectionCard>

        <SectionCard title="GPU 请求分布" subtitle="按累计提交实验统计">
          {gpuEntries.length === 0 ? (
            <div className="py-10 text-center text-sm text-muted">暂无统计数据</div>
          ) : (
            <div className="space-y-3.5">
              {gpuEntries.map(([gpu, count]) => (
                <div key={gpu}>
                  <div className="mb-1.5 flex items-center justify-between text-xs">
                    <span className="font-mono font-medium text-ink">{gpu}</span>
                    <span className="font-mono text-muted">{count} 次</span>
                  </div>
                  <div className="h-2 overflow-hidden rounded-full bg-subtle">
                    <div
                      className="h-full rounded-full bg-secondary transition-[width]"
                      style={{ width: `${Math.round((count / maxGpuCount) * 100)}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>
          )}
        </SectionCard>
      </div>
    </div>
  )
}

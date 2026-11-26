import { useCallback, useEffect, useState } from 'react'
import {
  AlertTriangle,
  Eraser,
  HardDrive,
  Plus,
  Power,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Trash2,
} from 'lucide-react'
import {
  cleanupOrphanRuntimes,
  deleteAccount,
  fetchAccounts,
  getPendingAccountAuthId,
  refreshAccountUsage,
  refreshPoolUsage,
  syncPoolAccounts,
  updateAccount,
} from '../api/client'
import { AccountAuthDialog } from '../components/AccountAuthDialog'
import { AccountUsage } from '../components/AccountUsage'
import {
  AccountStatusBadge,
  SectionCard,
  TierBadge,
} from '../components/StatusBadge'
import { useToast } from '../components/Toast'
import type { Account, DriveProfile } from '../types'
import { formatDuration, formatRelativeTime } from '../utils/format'

export function AccountsPage() {
  const { pushToast } = useToast()
  const [accounts, setAccounts] = useState<Account[]>([])
  const [driveProfiles, setDriveProfiles] = useState<DriveProfile[]>([])
  const [loading, setLoading] = useState(true)
  const [busyName, setBusyName] = useState<string | null>(null)
  const [nowMs, setNowMs] = useState(() => Date.now())
  const [refreshingNames, setRefreshingNames] = useState<string[]>([])
  const [refreshingUsage, setRefreshingUsage] = useState(false)

  // Add modal
  const [showAdd, setShowAdd] = useState(() => !!getPendingAccountAuthId())
  const [authAccount, setAuthAccount] = useState<Account | null>(null)

  // Cleanup modal
  const [cleanupResult, setCleanupResult] = useState<Record<string, string> | null>(null)
  const [cleaning, setCleaning] = useState(false)

  const loadAccounts = useCallback(async () => {
    try {
      const data = await fetchAccounts()
      setAccounts(data.accounts)
      setDriveProfiles(data.drive_profiles)
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '加载账号池失败')
    } finally {
      setLoading(false)
    }
  }, [pushToast])

  useEffect(() => {
    loadAccounts()
    const poll = setInterval(loadAccounts, 4000)
    const tick = setInterval(() => setNowMs(Date.now()), 1000)
    return () => {
      clearInterval(poll)
      clearInterval(tick)
    }
  }, [loadAccounts])

  const handleAuthorized = useCallback((acct: Account) => {
    pushToast('success', `账号 ${acct.name} 授权成功`)
    void loadAccounts()
  }, [loadAccounts, pushToast])

  const handleSyncDisk = async () => {
    setLoading(true)
    try {
      const res = await syncPoolAccounts()
      setAccounts(res.accounts)
      pushToast('success', `已从磁盘同步 ${res.accounts.length} 个账号`)
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '同步失败')
    } finally {
      setLoading(false)
    }
  }

  const handleRefreshUsage = async (name?: string) => {
    if (name) setRefreshingNames((names) => [...names, name])
    else setRefreshingUsage(true)
    try {
      if (name) {
        const updated = await refreshAccountUsage(name)
        setAccounts((current) => current.map((acct) => acct.name === name ? updated : acct))
      } else {
        const result = await refreshPoolUsage()
        setAccounts(result.accounts)
      }
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '刷新 CU 用量失败')
    } finally {
      if (name) setRefreshingNames((names) => names.filter((item) => item !== name))
      else setRefreshingUsage(false)
    }
  }

  const handleCleanup = async () => {
    setCleaning(true)
    try {
      const res = await cleanupOrphanRuntimes()
      setCleanupResult(res.results)
      pushToast('success', '孤儿实例清理完成')
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '清理孤儿实例失败')
    } finally {
      setCleaning(false)
    }
  }

  const handlePatch = async (
    acct: Account,
    patch: { tier?: string; enabled?: boolean; clear_cooldown?: boolean },
    msg: string
  ) => {
    setBusyName(acct.name)
    try {
      await updateAccount(acct.name, patch)
      pushToast('success', msg)
      await loadAccounts()
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '更新账号失败')
    } finally {
      setBusyName(null)
    }
  }

  const handleDelete = async (acct: Account) => {
    const removeDisk = window.confirm(
      `确认移除账号槽位 "${acct.name}"？\n\n点击「确定」将同时清理其磁盘目录；若只想从数据库移除请取消并手动清理。`
    )
    if (!removeDisk) return
    setBusyName(acct.name)
    try {
      await deleteAccount(acct.name, true)
      pushToast('success', `已移除账号 ${acct.name}`)
      await loadAccounts()
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '删除账号失败')
    } finally {
      setBusyName(null)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="page-title">账号池与云盘档案</h1>
          <p className="page-description">
            查看剩余 CU 与每小时消耗，管理账号授权、订阅与云盘。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2.5">
          <button
            type="button"
            onClick={handleSyncDisk}
            className="inline-flex items-center gap-1.5 rounded-xl border border-control bg-surface px-3.5 py-2 text-xs font-medium text-ink hover:bg-hover-surface"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            扫描磁盘同步
          </button>
          <button
            type="button"
            disabled={cleaning}
            onClick={handleCleanup}
            className="inline-flex items-center gap-1.5 rounded-xl border border-warning-line bg-warning-soft px-3.5 py-2 text-xs font-medium text-warning hover:bg-warning-soft-hover disabled:opacity-50"
          >
            <Eraser className="h-3.5 w-3.5" />
            {cleaning ? '正在清理...' : '清理孤儿实例'}
          </button>
          <button
            type="button"
            onClick={() => { setAuthAccount(null); setShowAdd(true) }}
            className="inline-flex items-center gap-1.5 rounded-xl bg-accent px-3.5 py-2 text-xs font-semibold text-on-accent hover:bg-accent-hover"
          >
            <Plus className="h-3.5 w-3.5" />
            添加账号
          </button>
        </div>
      </div>

      {showAdd && (
        <AccountAuthDialog
          account={authAccount}
          onClose={() => { setShowAdd(false); void loadAccounts() }}
          onAuthorized={handleAuthorized}
        />
      )}

      {cleanupResult && (
        <SectionCard
          title="孤儿实例清理报告"
          subtitle="仅扫描非忙碌账号执行 colab stop，正在运行的任务已自动跳过"
          action={
            <button
              type="button"
              onClick={() => setCleanupResult(null)}
              className="text-xs text-muted hover:text-ink"
            >
              关闭
            </button>
          }
        >
          <div className="space-y-1.5 font-mono text-xs">
            {Object.keys(cleanupResult).length === 0 ? (
              <div className="text-muted">未发现需要清理的已授权空闲账号</div>
            ) : (
              Object.entries(cleanupResult).map(([acctName, msg]) => (
                <div
                  key={acctName}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-inset px-3 py-2"
                >
                  <span className="text-accent">{acctName}</span>
                  <span className="text-secondary">{msg}</span>
                </div>
              ))
            )}
          </div>
        </SectionCard>
      )}

      <SectionCard
        title={`账号池 (${accounts.length})`}
        subtitle="用量来自 Colab，每分钟自动更新。消耗速率包含该账号的全部运行实例。"
        action={
          <button
            type="button"
            onClick={() => void handleRefreshUsage()}
            disabled={refreshingUsage || accounts.length === 0 || accounts.some((acct) => acct.usage?.refreshing)}
            className="button-secondary"
          >
            <RefreshCw aria-hidden="true" className={`h-3.5 w-3.5 ${refreshingUsage || accounts.some((acct) => acct.usage?.refreshing) ? 'animate-spin motion-reduce:animate-none' : ''}`} />
            刷新 CU 用量
          </button>
        }
      >
        {accounts.length === 0 ? (
          <div className="py-12 text-center text-sm text-muted">暂无账号记录</div>
        ) : (
          <div className="space-y-3">
            {accounts.map((acct) => {
              const liveCooldown =
                acct.status === 'cooldown'
                  ? Math.max(0, Math.floor(acct.cooldown_until - nowMs / 1000))
                  : 0
              const isBusy = acct.status === 'busy' || acct.status === 'maintenance'
              const isUpdating = busyName === acct.name

              return (
                <div
                  key={acct.name}
                  className="rounded-xl border border-line bg-inset p-4"
                >
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="flex flex-wrap items-center gap-2.5">
                      <span className="font-mono text-sm font-semibold text-ink">
                        {acct.name}
                      </span>
                      <TierBadge tier={acct.tier} />
                      <AccountStatusBadge status={acct.status} enabled={acct.enabled} />
                      {acct.auth_configured ? (
                        <span className="inline-flex items-center gap-1 rounded-md border border-success-line bg-success-soft px-2 py-0.5 text-xs text-success">
                          <ShieldCheck className="h-3 w-3" />
                          已授权凭据
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 rounded-md border border-warning-line bg-warning-soft px-2 py-0.5 text-xs text-warning">
                          <AlertTriangle className="h-3 w-3" />
                          未检测到凭据
                        </span>
                      )}
                    </div>

                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        disabled={isUpdating || isBusy}
                        onClick={() => { setAuthAccount(acct); setShowAdd(true) }}
                        className="inline-flex min-h-9 items-center gap-1 rounded-lg border border-accent-line bg-accent-soft px-2.5 py-1 text-xs text-accent hover:bg-accent-soft-hover focus-visible:outline-2 focus-visible:outline-accent disabled:opacity-50"
                      >
                        <ShieldCheck aria-hidden="true" className="h-3 w-3" />
                        {acct.auth_configured ? '重新授权' : '网页授权'}
                      </button>
                      <select
                        aria-label={`${acct.name} 的订阅等级`}
                        value={acct.tier}
                        disabled={isUpdating || isBusy}
                        onChange={(e) =>
                          handlePatch(
                            acct,
                            { tier: e.target.value },
                            `已修改 ${acct.name} 等级为 ${e.target.value}`
                          )
                        }
                        className="rounded-lg border border-control bg-surface px-2 py-1 text-xs text-ink outline-none disabled:opacity-50"
                      >
                        <option value="pro">pro</option>
                        <option value="pro+">pro+</option>
                        <option value="free">free</option>
                      </select>

                      {acct.status === 'cooldown' && (
                        <button
                          type="button"
                          disabled={isUpdating || isBusy}
                          onClick={() =>
                            handlePatch(
                              acct,
                              { clear_cooldown: true },
                              `已解除 ${acct.name} 冷却`
                            )
                          }
                          className="inline-flex items-center gap-1 rounded-lg border border-warning-line bg-warning-soft px-2.5 py-1 text-xs text-warning hover:bg-warning-soft-hover disabled:opacity-50"
                        >
                          <RotateCcw className="h-3 w-3" />
                          清冷却 ({formatDuration(liveCooldown)})
                        </button>
                      )}

                      <button
                        type="button"
                        disabled={isUpdating || isBusy}
                        onClick={() =>
                          handlePatch(
                            acct,
                            { enabled: !acct.enabled },
                            `${acct.name} 已${!acct.enabled ? '启用' : '停用'}`
                          )
                        }
                        className="inline-flex items-center gap-1 rounded-lg border border-control bg-surface px-2.5 py-1 text-xs text-ink hover:bg-hover-surface disabled:opacity-50"
                      >
                        <Power className="h-3 w-3" />
                        {acct.enabled ? '停用' : '启用'}
                      </button>

                      <button
                        type="button"
                        disabled={isUpdating || isBusy}
                        onClick={() => handleDelete(acct)}
                        className="inline-flex items-center gap-1 rounded-lg border border-control bg-surface px-2.5 py-1 text-xs text-muted hover:border-danger-line hover:text-danger disabled:opacity-50"
                      >
                        <Trash2 className="h-3 w-3" />
                        删除
                      </button>
                    </div>
                  </div>

                  <AccountUsage
                    account={acct}
                    nowMs={nowMs}
                    onRefresh={() => void handleRefreshUsage(acct.name)}
                    requesting={refreshingNames.includes(acct.name)}
                  />

                  <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1 font-mono text-xs text-muted">
                    <span className="account-home">账号目录：{acct.home_dir}</span>
                    <span>成功: {acct.total_runs}</span>
                    <span>失败: {acct.total_failures}</span>
                    <span>最近使用: {formatRelativeTime(acct.last_used_at, nowMs)}</span>
                  </div>

                  {!acct.auth_configured && (
                    <div className="mt-2.5 rounded-lg border border-warning-line bg-warning-soft px-3 py-2 text-xs text-warning">
                      点击“网页授权”打开 Google 链接，完成后将授权码贴回弹窗。
                    </div>
                  )}

                  {acct.last_error && (
                    <div className="mt-2 rounded-lg bg-surface px-3 py-1.5 font-mono text-xs text-danger">
                      最近错误：{acct.last_error}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </SectionCard>

      <SectionCard
        title={`服务端已检测的 Google Drive 档案 (${driveProfiles.length})`}
        subtitle="如需配置请在服务器运行 ./scripts/init_drive_profile.sh <profile_name>"
      >
        {driveProfiles.length === 0 ? (
          <div className="text-xs text-muted">
            暂未在服务端 drives 目录检测到 <code className="text-secondary">*.conf</code> 档案。
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {driveProfiles.map((dp) => (
              <div
                key={dp.profile}
                className="flex items-center gap-3 rounded-xl border border-line bg-inset p-3.5 text-xs"
              >
                <HardDrive className="h-5 w-5 shrink-0 text-accent" />
                <div className="min-w-0">
                  <div className="font-mono font-semibold text-ink">{dp.profile}</div>
                  <div className="truncate font-mono text-xs text-muted">
                    remote: {dp.remote_name}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </SectionCard>
    </div>
  )
}

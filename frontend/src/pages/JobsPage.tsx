import { useCallback, useEffect, useState } from 'react'
import {
  ChevronLeft,
  ChevronRight,
  Eye,
  RefreshCw,
  RotateCw,
  Search,
  StopCircle,
  Trash2,
} from 'lucide-react'
import { cancelJob, deleteJob, fetchJobs, retryJob } from '../api/client'
import { GpuBadge, JobStatusBadge, SectionCard } from '../components/StatusBadge'
import { useToast } from '../components/Toast'
import type { Job } from '../types'
import { formatDuration, formatTimestamp, jobElapsedSeconds } from '../utils/format'

interface JobsPageProps {
  onOpenJob: (jobId: string) => void
  onGoSubmit: () => void
}

const STATUS_TABS = [
  { id: '', label: '全部' },
  { id: 'running', label: '运行中' },
  { id: 'provisioning', label: '分配中' },
  { id: 'queued', label: '排队中' },
  { id: 'succeeded', label: '已完成' },
  { id: 'failed', label: '失败' },
  { id: 'cancelled', label: '已取消' },
]

const GPU_OPTIONS = ['', 'T4', 'L4', 'A100', 'H100', 'G4', 'CPU']
const PAGE_SIZE = 20

export function JobsPage({ onOpenJob, onGoSubmit }: JobsPageProps) {
  const { pushToast } = useToast()
  const [statusFilter, setStatusFilter] = useState('')
  const [gpuFilter, setGpuFilter] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const [jobs, setJobs] = useState<Job[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [actionJobId, setActionJobId] = useState<string | null>(null)
  const [nowMs, setNowMs] = useState(() => Date.now())

  useEffect(() => {
    const timer = setTimeout(() => {
      setOffset(0)
      setSearchQuery(searchInput.trim())
    }, 280)
    return () => clearTimeout(timer)
  }, [searchInput])

  const loadJobs = useCallback(async () => {
    try {
      const data = await fetchJobs({
        status: statusFilter || undefined,
        gpu: gpuFilter || undefined,
        search: searchQuery || undefined,
        limit: PAGE_SIZE,
        offset,
      })
      setJobs(data.jobs)
      setTotal(data.total)
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '加载任务列表失败')
    } finally {
      setLoading(false)
    }
  }, [statusFilter, gpuFilter, searchQuery, offset, pushToast])

  useEffect(() => {
    loadJobs()
    const poll = setInterval(loadJobs, 3500)
    const tick = setInterval(() => setNowMs(Date.now()), 1000)
    return () => {
      clearInterval(poll)
      clearInterval(tick)
    }
  }, [loadJobs])

  const handleCancel = async (e: React.MouseEvent, jobId: string) => {
    e.stopPropagation()
    setActionJobId(jobId)
    try {
      await cancelJob(jobId)
      pushToast('info', `已向任务 ${jobId} 发送取消信号`)
      await loadJobs()
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '取消失败')
    } finally {
      setActionJobId(null)
    }
  }

  const handleRetry = async (e: React.MouseEvent, jobId: string) => {
    e.stopPropagation()
    setActionJobId(jobId)
    try {
      const created = await retryJob(jobId)
      pushToast('success', `已克隆重新提交为 ${created.job_id}`)
      onOpenJob(created.job_id)
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '重试失败')
    } finally {
      setActionJobId(null)
    }
  }

  const handleDelete = async (e: React.MouseEvent, jobId: string) => {
    e.stopPropagation()
    if (!window.confirm(`确认删除任务 ${jobId} 及其磁盘日志/产物目录吗？`)) return
    setActionJobId(jobId)
    try {
      await deleteJob(jobId)
      pushToast('success', `已删除任务 ${jobId}`)
      await loadJobs()
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '删除失败')
    } finally {
      setActionJobId(null)
    }
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="page-title">任务中心</h1>
          <p className="page-description">
            按状态、GPU 类型与关键字检索全部历史与活跃实验任务
          </p>
        </div>
        <div className="flex items-center gap-2.5">
          <button
            type="button"
            onClick={loadJobs}
            className="inline-flex items-center gap-1.5 rounded-xl border border-control bg-surface px-3.5 py-2 text-xs font-medium text-ink hover:bg-hover-surface"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            刷新
          </button>
          <button
            type="button"
            onClick={onGoSubmit}
            className="rounded-xl bg-accent px-3.5 py-2 text-xs font-semibold text-on-accent hover:bg-accent-hover"
          >
            + 提交实验
          </button>
        </div>
      </div>

      <SectionCard>
        {/* Filter bar */}
        <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex flex-wrap gap-1.5">
            {STATUS_TABS.map((tab) => {
              const active = statusFilter === tab.id
              return (
                <button
                  key={tab.id || 'all'}
                  type="button"
                  aria-pressed={active}
                  onClick={() => {
                    setOffset(0)
                    setStatusFilter(tab.id)
                  }}
                  className={`rounded-lg px-3 py-1.5 text-xs font-medium transition ${
                    active
                      ? 'bg-accent-soft text-accent ring-1 ring-accent-ring'
                      : 'bg-surface text-muted hover:bg-hover-surface hover:text-ink'
                  }`}
                >
                  {tab.label}
                </button>
              )
            })}
          </div>

          <div className="flex flex-wrap items-center gap-2.5">
            <select
              aria-label="按 GPU 筛选"
              value={gpuFilter}
              onChange={(e) => {
                setOffset(0)
                setGpuFilter(e.target.value)
              }}
              className="rounded-xl border border-control bg-inset px-3 py-1.5 text-xs text-ink outline-none focus:border-accent"
            >
              <option value="">全部 GPU</option>
              {GPU_OPTIONS.filter(Boolean).map((g) => (
                <option key={g} value={g}>
                  {g}
                </option>
              ))}
            </select>

            <div className="relative">
              <Search className="pointer-events-none absolute top-1/2 left-3 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
              <input
                aria-label="搜索任务"
                type="text"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                placeholder="搜索任务名 / ID / 账号 / 入口..."
                className="w-60 rounded-xl border border-control bg-inset py-1.5 pr-3 pl-8 text-xs text-ink placeholder-placeholder outline-none focus:border-accent"
              />
            </div>
          </div>
        </div>

        {/* Table */}
        {jobs.length === 0 ? (
          <div className="rounded-xl border border-dashed border-line py-14 text-center text-sm text-muted">
            没有匹配的任务记录
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="data-table w-full text-left text-xs">
              <thead>
                <tr className="border-b border-line text-muted">
                  <th className="pb-3 font-medium">任务 / 入口</th>
                  <th className="pb-3 font-medium">硬件</th>
                  <th className="pb-3 font-medium">账号与容灾链路</th>
                  <th className="pb-3 font-medium">状态</th>
                  <th className="pb-3 font-medium">创建时间 / 耗时</th>
                  <th className="pb-3 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {jobs.map((job) => {
                  const isActive = ['queued', 'provisioning', 'running'].includes(job.status)
                  const isTerminal = ['succeeded', 'failed', 'cancelled'].includes(job.status)
                  const isBusy = actionJobId === job.job_id
                  return (
                    <tr
                      key={job.job_id}
                      onClick={() => onOpenJob(job.job_id)}
                      className="cursor-pointer transition hover:bg-hover-surface"
                    >
                      <td className="py-3 pr-3">
                        <div className="flex items-center gap-2">
                          <span className="font-medium text-ink">{job.name}</span>
                          {job.priority !== 0 && (
                            <span className="rounded bg-subtle px-1.5 py-0.5 font-mono text-[11px] text-secondary">
                              P{job.priority}
                            </span>
                          )}
                        </div>
                        <div className="mt-0.5 font-mono text-xs text-muted">
                          {job.job_id} · {job.entrypoint}
                        </div>
                      </td>

                      <td className="py-3 pr-3">
                        <GpuBadge gpu={job.gpu} tpu={job.tpu} highMem={job.high_mem} />
                        {job.gpu_detected && (
                          <div className="mt-1 max-w-[180px] truncate font-mono text-[11px] text-success">
                            {job.gpu_detected}
                          </div>
                        )}
                      </td>

                      <td className="py-3 pr-3">
                        <div className="font-mono text-ink">
                          {job.assigned_account || '等待分配'}
                        </div>
                        {job.attempted_accounts.length > 1 && (
                          <div className="mt-0.5 font-mono text-[11px] text-warning">
                            容灾: {job.attempted_accounts.join(' → ')}
                          </div>
                        )}
                      </td>

                      <td className="py-3 pr-3">
                        <JobStatusBadge status={job.status} />
                      </td>

                      <td className="py-3 pr-3 font-mono text-xs text-muted">
                        <div>{formatTimestamp(job.created_at)}</div>
                        <div className="text-muted">
                          耗时 {formatDuration(jobElapsedSeconds(job, nowMs))}
                        </div>
                      </td>

                      <td className="py-3 text-right">
                        <div className="inline-flex items-center gap-1.5">
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation()
                              onOpenJob(job.job_id)
                            }}
                            title="查看详情与日志"
                            className="rounded-lg border border-control bg-surface p-1.5 text-secondary hover:border-accent-line hover:text-accent"
                          >
                            <Eye className="h-3.5 w-3.5" />
                          </button>

                          {isActive && (
                            <button
                              type="button"
                              disabled={isBusy}
                              onClick={(e) => handleCancel(e, job.job_id)}
                              title="取消任务"
                              className="rounded-lg border border-danger-line bg-danger-soft p-1.5 text-danger hover:bg-danger-soft-hover disabled:opacity-50"
                            >
                              <StopCircle className="h-3.5 w-3.5" />
                            </button>
                          )}

                          {isTerminal && (
                            <>
                              <button
                                type="button"
                                disabled={isBusy}
                                onClick={(e) => handleRetry(e, job.job_id)}
                                title="重试任务"
                                className="rounded-lg border border-accent-line bg-accent-soft p-1.5 text-accent hover:bg-accent-soft-hover disabled:opacity-50"
                              >
                                <RotateCw className="h-3.5 w-3.5" />
                              </button>
                              <button
                                type="button"
                                disabled={isBusy}
                                onClick={(e) => handleDelete(e, job.job_id)}
                                title="删除任务"
                                className="rounded-lg border border-control bg-surface p-1.5 text-muted hover:border-danger-line hover:text-danger disabled:opacity-50"
                              >
                                <Trash2 className="h-3.5 w-3.5" />
                              </button>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}

        {/* Pagination */}
        <div className="mt-4 flex items-center justify-between border-t border-line pt-3.5 text-xs text-muted">
          <span>
            共 <strong className="font-mono text-ink">{total}</strong> 个任务 · 第{' '}
            <strong className="font-mono text-ink">{currentPage}</strong> / {totalPages} 页
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={offset === 0}
              onClick={() => setOffset((prev) => Math.max(0, prev - PAGE_SIZE))}
              className="inline-flex items-center gap-1 rounded-lg border border-control bg-surface px-2.5 py-1 text-ink hover:bg-hover-surface disabled:cursor-not-allowed disabled:opacity-40"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              上一页
            </button>
            <button
              type="button"
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset((prev) => prev + PAGE_SIZE)}
              className="inline-flex items-center gap-1 rounded-lg border border-control bg-surface px-2.5 py-1 text-ink hover:bg-hover-surface disabled:cursor-not-allowed disabled:opacity-40"
            >
              下一页
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </SectionCard>
    </div>
  )
}

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowLeft,
  Check,
  Copy,
  Download,
  Eye,
  EyeOff,
  FileCode2,
  FolderArchive,
  FolderOpen,
  Image as ImageIcon,
  Radio,
  RotateCw,
  Search,
  StopCircle,
  Terminal,
  Trash2,
} from 'lucide-react'
import {
  cancelJob,
  deleteJob,
  fetchAuthenticatedBlob,
  fetchJob,
  fetchJobLogs,
  fetchJobWorkspace,
  getArtifactsArchiveUrl,
  getOutputFileUrl,
  retryJob,
  streamJobLogs,
  triggerAuthenticatedDownload,
} from '../api/client'
import { GpuBadge, JobStatusBadge, SectionCard } from '../components/StatusBadge'
import { useToast } from '../components/Toast'
import type { Job, JobSubmitPayload, OutputFileDetail } from '../types'
import {
  formatBytes,
  formatDuration,
  formatTimestamp,
  isImageFile,
  isTextPreviewable,
  jobElapsedSeconds,
} from '../utils/format'
import { detectedGpu, hardwareSummary } from '../utils/hardware'

interface JobDetailPageProps {
  jobId: string
  onBack: () => void
  onOpenJob: (jobId: string) => void
  onCloneToSubmit: (preset: Partial<JobSubmitPayload>) => void
}

type DetailTab = 'logs' | 'artifacts' | 'workspace'

export function JobDetailPage({
  jobId,
  onBack,
  onOpenJob,
  onCloneToSubmit,
}: JobDetailPageProps) {
  const { pushToast } = useToast()
  const [job, setJob] = useState<Job | null>(null)
  const [tab, setTab] = useState<DetailTab>('logs')
  const [loading, setLoading] = useState(true)
  const [actionBusy, setActionBusy] = useState(false)
  const [nowMs, setNowMs] = useState(() => Date.now())

  // Logs state
  const [logText, setLogText] = useState('')
  const [logOffset, setLogOffset] = useState(0)
  const [logFileSize, setLogFileSize] = useState(0)
  const [isStreaming, setIsStreaming] = useState(false)
  const [autoScroll, setAutoScroll] = useState(true)
  const [logFilter, setLogFilter] = useState('')
  const [copiedLogs, setCopiedLogs] = useState(false)
  const logContainerRef = useRef<HTMLPreElement | null>(null)

  // Artifacts state
  const [selectedOutput, setSelectedOutput] = useState<string | null>(null)
  const [previewImageUrl, setPreviewImageUrl] = useState<string | null>(null)
  const [previewText, setPreviewText] = useState<string | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)

  // Workspace state
  const [wsFiles, setWsFiles] = useState<OutputFileDetail[]>([])
  const [selectedWsFile, setSelectedWsFile] = useState<string | null>(null)
  const [wsContent, setWsContent] = useState<string | null>(null)
  const [wsTruncated, setWsTruncated] = useState(false)
  const [showEnvValues, setShowEnvValues] = useState(false)

  const refreshJob = useCallback(async () => {
    try {
      const data = await fetchJob(jobId)
      setJob(data)
      return data
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '读取任务详情失败')
      return null
    } finally {
      setLoading(false)
    }
  }, [jobId, pushToast])

  // Initial load + SSE log streaming
  useEffect(() => {
    const controller = new AbortController()
    let active = true

    const initAndStream = async () => {
      setLoading(true)
      setLogText('')
      setLogOffset(0)
      setSelectedOutput(null)
      setPreviewText(null)
      setPreviewImageUrl((prev) => {
        if (prev) URL.revokeObjectURL(prev)
        return null
      })

      const j = await refreshJob()
      if (!active || !j) return

      try {
        const tailResp = await fetchJobLogs(jobId, { tail_lines: 1000 })
        if (!active) return
        setLogText(tailResp.logs ? `${tailResp.logs}\n` : '')
        setLogOffset(tailResp.next_offset)
        setLogFileSize(tailResp.file_size)

        const isTerminal = ['succeeded', 'failed', 'cancelled'].includes(j.status)
        if (!isTerminal) {
          setIsStreaming(true)
          await streamJobLogs(
            jobId,
            tailResp.next_offset,
            {
              onLog: (chunk, nextOff) => {
                if (!active) return
                setLogText((prev) => prev + chunk)
                setLogOffset(nextOff)
                setLogFileSize((prev) => Math.max(prev, nextOff))
              },
              onStatus: (st) => {
                if (!active) return
                setJob((prev) =>
                  prev
                    ? {
                        ...prev,
                        status: st.status,
                        assigned_account: st.assigned_account,
                        gpu_detected: st.gpu_detected,
                        runtime_info: st.runtime_info,
                        exit_code: st.exit_code,
                        error_message: st.error_message,
                      }
                    : prev
                )
              },
              onDone: async () => {
                if (!active) return
                setIsStreaming(false)
                await refreshJob()
              },
              onError: () => {
                if (!active) return
                setIsStreaming(false)
              },
            },
            controller.signal
          )
        }
      } catch (err) {
        if (active) {
          setIsStreaming(false)
          pushToast('error', err instanceof Error ? err.message : '加载日志失败')
        }
      }
    }

    initAndStream()
    const clock = setInterval(() => setNowMs(Date.now()), 1000)

    return () => {
      active = false
      controller.abort()
      clearInterval(clock)
    }
  }, [jobId, refreshJob, pushToast])

  // Auto scroll terminal
  useEffect(() => {
    if (autoScroll && tab === 'logs' && logContainerRef.current) {
      logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight
    }
  }, [logText, autoScroll, tab])

  // Revoke object URL on unmount or change
  useEffect(() => {
    return () => {
      if (previewImageUrl) URL.revokeObjectURL(previewImageUrl)
    }
  }, [previewImageUrl])

  // Load workspace files when workspace tab opens
  useEffect(() => {
    if (tab !== 'workspace' || !job) return
    let mounted = true
    const loadWs = async () => {
      try {
        const target = selectedWsFile ?? job.entrypoint
        const res = await fetchJobWorkspace(jobId, target)
        if (!mounted) return
        setWsFiles(res.files)
        setSelectedWsFile(res.selected_file)
        setWsContent(res.content)
        setWsTruncated(res.truncated)
      } catch {
        // Fallback listing without specific file
        try {
          const res = await fetchJobWorkspace(jobId)
          if (!mounted) return
          setWsFiles(res.files)
        } catch (err) {
          pushToast('error', err instanceof Error ? err.message : '加载工作区失败')
        }
      }
    }
    loadWs()
    return () => {
      mounted = false
    }
  }, [tab, job, jobId, selectedWsFile, pushToast])

  const handleLoadFullLogs = async () => {
    try {
      let cur = 0
      let collected = ''
      let total = logFileSize
      while (true) {
        const res = await fetchJobLogs(jobId, { offset: cur })
        total = res.file_size
        if (!res.logs) break
        collected += res.logs
        cur = res.next_offset
        if (cur >= res.file_size) break
      }
      setLogText(collected)
      setLogOffset(cur)
      setLogFileSize(total)
      pushToast('success', '已加载完整日志文件')
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '加载完整日志失败')
    }
  }

  const handlePreviewOutput = async (relPath: string) => {
    setSelectedOutput(relPath)
    setPreviewText(null)
    setPreviewImageUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev)
      return null
    })

    const url = getOutputFileUrl(jobId, relPath)
    if (!isImageFile(relPath) && !isTextPreviewable(relPath)) {
      return
    }
    setPreviewLoading(true)
    try {
      const blob = await fetchAuthenticatedBlob(url)
      if (isImageFile(relPath)) {
        setPreviewImageUrl(URL.createObjectURL(blob))
      } else {
        const txt = await blob.text()
        if (relPath.toLowerCase().endsWith('.json')) {
          try {
            setPreviewText(JSON.stringify(JSON.parse(txt), null, 2))
          } catch {
            setPreviewText(txt)
          }
        } else {
          setPreviewText(txt)
        }
      }
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '预览文件失败')
    } finally {
      setPreviewLoading(false)
    }
  }

  const handleCancel = async () => {
    setActionBusy(true)
    try {
      const updated = await cancelJob(jobId)
      setJob(updated)
      pushToast('info', '已发送取消请求，正在释放 Colab 实例')
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '取消任务失败')
    } finally {
      setActionBusy(false)
    }
  }

  const handleRetry = async () => {
    setActionBusy(true)
    try {
      const created = await retryJob(jobId)
      pushToast('success', `已重新提交为新任务 ${created.job_id}`)
      onOpenJob(created.job_id)
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '重试失败')
    } finally {
      setActionBusy(false)
    }
  }

  const handleClone = async () => {
    if (!job) return
    let scriptContent: string | undefined
    try {
      const ws = await fetchJobWorkspace(jobId, job.entrypoint)
      scriptContent = ws.content ?? undefined
    } catch {
      scriptContent = undefined
    }
    onCloneToSubmit({
      name: `${job.name}-copy`,
      gpu: job.gpu,
      tpu: job.tpu,
      high_mem: job.high_mem,
      priority: job.priority,
      entrypoint: job.entrypoint,
      script_content: scriptContent,
      script_args: job.script_args,
      pip_packages: job.pip_packages,
      dataset_commands: job.dataset_commands,
      env_vars: job.env_vars,
      drive_profile: job.drive_profile,
      drive_subpath: job.drive_subpath,
      drive_sync_outputs_to: job.drive_sync_outputs_to,
      timeout_seconds: job.timeout_seconds,
    })
  }

  const handleDelete = async () => {
    if (!window.confirm(`确认删除任务 ${jobId} 及其全部本地产物？`)) return
    setActionBusy(true)
    try {
      await deleteJob(jobId)
      pushToast('success', `已删除任务 ${jobId}`)
      onBack()
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '删除失败')
    } finally {
      setActionBusy(false)
    }
  }

  const filteredLogText = useMemo(() => {
    if (!logFilter.trim()) return logText
    const needle = logFilter.trim().toLowerCase()
    return logText
      .split('\n')
      .filter((line) => line.toLowerCase().includes(needle))
      .join('\n')
  }, [logText, logFilter])

  if (loading && !job) {
    return (
      <div className="py-16 text-center text-sm text-muted">正在加载任务 {jobId}...</div>
    )
  }

  if (!job) {
    return (
      <div className="space-y-4">
        <button
          type="button"
          onClick={onBack}
          className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-ink"
        >
          <ArrowLeft className="h-4 w-4" />
          返回任务中心
        </button>
        <div className="rounded-2xl border border-line bg-surface p-10 text-center text-sm text-muted">
          未找到任务 <code className="font-mono text-ink">{jobId}</code>
        </div>
      </div>
    )
  }

  const isActive = ['queued', 'provisioning', 'running'].includes(job.status)
  const isTerminal = ['succeeded', 'failed', 'cancelled'].includes(job.status)
  const outputs = job.output_file_details ?? (job.output_files ?? []).map((p) => ({ path: p, size: 0 }))

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <button
            type="button"
            onClick={onBack}
            className="mb-2 inline-flex items-center gap-1.5 text-xs font-medium text-muted hover:text-ink"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            返回任务列表
          </button>
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="page-title">{job.name}</h1>
            <JobStatusBadge status={job.status} />
            <GpuBadge gpu={job.gpu} tpu={job.tpu} highMem={job.high_mem} />
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-xs text-muted">
            <span>ID: {job.job_id}</span>
            <span>入口: {job.entrypoint}</span>
            {job.session_name && <span>Session: {job.session_name}</span>}
            {(detectedGpu(job.runtime_info) || job.gpu_detected) && <span className="text-success">实测 GPU: {detectedGpu(job.runtime_info) || job.gpu_detected}</span>}
            {hardwareSummary(job.runtime_info) && <span>{hardwareSummary(job.runtime_info)}</span>}
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {isActive && (
            <button
              type="button"
              disabled={actionBusy}
              onClick={handleCancel}
              className="inline-flex items-center gap-1.5 rounded-xl border border-danger-line bg-danger-soft px-3 py-2 text-xs font-medium text-danger hover:bg-danger-soft-hover disabled:opacity-50"
            >
              <StopCircle className="h-3.5 w-3.5" />
              取消并销毁 VM
            </button>
          )}
          {isTerminal && (
            <button
              type="button"
              disabled={actionBusy}
              onClick={handleRetry}
              className="inline-flex items-center gap-1.5 rounded-xl border border-accent-line bg-accent-soft px-3 py-2 text-xs font-medium text-accent hover:bg-accent-soft-hover disabled:opacity-50"
            >
              <RotateCw className="h-3.5 w-3.5" />
              一键重试
            </button>
          )}
          <button
            type="button"
            onClick={handleClone}
            className="inline-flex items-center gap-1.5 rounded-xl border border-control bg-surface px-3 py-2 text-xs font-medium text-ink hover:bg-hover-surface"
          >
            <Copy className="h-3.5 w-3.5" />
            克隆配置提交
          </button>
          {job.artifacts_archive && (
            <button
              type="button"
              onClick={() =>
                triggerAuthenticatedDownload(
                  getArtifactsArchiveUrl(jobId),
                  `colab_job_${jobId}_outputs.tar.gz`
                ).catch((e) => pushToast('error', e.message))
              }
              className="inline-flex items-center gap-1.5 rounded-xl bg-success px-3 py-2 text-xs font-semibold text-on-accent hover:bg-success-hover"
            >
              <FolderArchive className="h-3.5 w-3.5" />
              下载产物包 (.tar.gz)
            </button>
          )}
          {isTerminal && (
            <button
              type="button"
              disabled={actionBusy}
              onClick={handleDelete}
              className="inline-flex items-center gap-1.5 rounded-xl border border-control bg-surface px-3 py-2 text-xs text-muted hover:border-danger-line hover:text-danger disabled:opacity-50"
            >
              <Trash2 className="h-3.5 w-3.5" />
              删除
            </button>
          )}
        </div>
      </div>

      {/* Metadata summary strip */}
      <div className="grid grid-cols-2 gap-3 rounded-2xl border border-line bg-surface p-4 text-xs sm:grid-cols-3 lg:grid-cols-6">
        <div>
          <div className="text-muted">当前分配账号</div>
          <div className="mt-1 font-mono font-medium text-ink">
            {job.assigned_account || '—'}
          </div>
        </div>
        <div>
          <div className="text-muted">Failover 尝试路径</div>
          <div className="mt-1 truncate font-mono text-ink" title={job.attempted_accounts.join(' → ')}>
            {job.attempted_accounts.length ? job.attempted_accounts.join(' → ') : '—'}
          </div>
        </div>
        <div>
          <div className="text-muted">运行时长</div>
          <div className="mt-1 font-mono text-ink">
            {formatDuration(jobElapsedSeconds(job, nowMs))}
          </div>
        </div>
        <div>
          <div className="text-muted">退出码</div>
          <div className="mt-1 font-mono text-ink">
            {job.exit_code != null ? job.exit_code : '—'}
          </div>
        </div>
        <div>
          <div className="text-muted">Drive 档案</div>
          <div className="mt-1 font-mono text-ink">{job.drive_profile || '未挂载'}</div>
        </div>
        <div>
          <div className="text-muted">提交时间</div>
          <div className="mt-1 font-mono text-secondary">{formatTimestamp(job.created_at)}</div>
        </div>
      </div>

      {job.error_message && (
        <div className="rounded-xl border border-danger-line bg-danger-soft px-4 py-3 text-xs text-danger">
          <span className="font-semibold">状态说明：</span>
          <span className="font-mono">{job.error_message}</span>
        </div>
      )}

      {/* Tabs */}
      <div className="flex flex-wrap gap-2 border-b border-line pb-2">
        <button
          type="button"
          onClick={() => setTab('logs')}
          className={`inline-flex items-center gap-2 rounded-xl px-4 py-2 text-xs font-medium transition ${
            tab === 'logs'
              ? 'bg-accent-soft text-accent ring-1 ring-accent-ring'
              : 'text-muted hover:bg-hover-surface hover:text-ink'
          }`}
        >
          <Terminal className="h-3.5 w-3.5" />
          实时终端日志
          {isStreaming && (
            <span className="inline-flex items-center gap-1 rounded-full bg-success-soft px-1.5 py-0.5 text-[11px] text-success">
              <Radio className="h-2.5 w-2.5 animate-pulse" />
              LIVE
            </span>
          )}
        </button>

        <button
          type="button"
          onClick={() => setTab('artifacts')}
          className={`inline-flex items-center gap-2 rounded-xl px-4 py-2 text-xs font-medium transition ${
            tab === 'artifacts'
              ? 'bg-accent-soft text-accent ring-1 ring-accent-ring'
              : 'text-muted hover:bg-hover-surface hover:text-ink'
          }`}
        >
          <FolderOpen className="h-3.5 w-3.5" />
          产物浏览器 ({outputs.length})
        </button>

        <button
          type="button"
          onClick={() => setTab('workspace')}
          className={`inline-flex items-center gap-2 rounded-xl px-4 py-2 text-xs font-medium transition ${
            tab === 'workspace'
              ? 'bg-accent-soft text-accent ring-1 ring-accent-ring'
              : 'text-muted hover:bg-hover-surface hover:text-ink'
          }`}
        >
          <FileCode2 className="h-3.5 w-3.5" />
          工作区与配置
        </button>
      </div>

      {/* Tab 1: Logs */}
      {tab === 'logs' && (
        <SectionCard>
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative">
                <Search className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
                <input
                  type="text"
                  value={logFilter}
                  onChange={(e) => setLogFilter(e.target.value)}
                  placeholder="过滤日志关键词..."
                  className="w-52 rounded-lg border border-control bg-inset py-1.5 pr-2.5 pl-8 text-xs text-ink placeholder-placeholder outline-none focus:border-accent"
                />
              </div>
              <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-line bg-inset px-2.5 py-1.5 text-xs text-secondary">
                <input
                  type="checkbox"
                  checked={autoScroll}
                  onChange={(e) => setAutoScroll(e.target.checked)}
                  className="accent-accent"
                />
                自动滚底
              </label>
              <span className="font-mono text-xs text-muted">
                字节 {formatBytes(logOffset)} / {formatBytes(logFileSize)}
              </span>
            </div>

            <div className="flex items-center gap-2">
              {logFileSize > logText.length && (
                <button
                  type="button"
                  onClick={handleLoadFullLogs}
                  className="rounded-lg border border-control bg-surface px-2.5 py-1.5 text-xs text-ink hover:bg-hover-surface"
                >
                  加载全部历史
                </button>
              )}
              <button
                type="button"
                onClick={async () => {
                  await navigator.clipboard.writeText(filteredLogText)
                  setCopiedLogs(true)
                  setTimeout(() => setCopiedLogs(false), 1800)
                }}
                className="inline-flex items-center gap-1 rounded-lg border border-control bg-surface px-2.5 py-1.5 text-xs text-ink hover:bg-hover-surface"
              >
                {copiedLogs ? <Check className="h-3.5 w-3.5 text-success" /> : <Copy className="h-3.5 w-3.5" />}
                复制
              </button>
              <button
                type="button"
                onClick={() => {
                  const blob = new Blob([logText], { type: 'text/plain;charset=utf-8' })
                  const u = URL.createObjectURL(blob)
                  const a = document.createElement('a')
                  a.href = u
                  a.download = `job_${jobId}.log`
                  a.click()
                  setTimeout(() => URL.revokeObjectURL(u), 1000)
                }}
                className="inline-flex items-center gap-1 rounded-lg border border-control bg-surface px-2.5 py-1.5 text-xs text-ink hover:bg-hover-surface"
              >
                <Download className="h-3.5 w-3.5" />
                下载 .log
              </button>
            </div>
          </div>

          <pre
            ref={logContainerRef}
            className="h-[520px] overflow-auto rounded-xl border border-line bg-editor p-4 font-mono text-xs leading-relaxed whitespace-pre-wrap text-ink"
          >
            {filteredLogText || (isActive ? '等待调度器输出日志...' : '暂无日志输出')}
          </pre>
        </SectionCard>
      )}

      {/* Tab 2: Artifacts */}
      {tab === 'artifacts' && (
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-3">
          <SectionCard title="输出文件列表" subtitle="/content/outputs 回收文件">
            {outputs.length === 0 ? (
              <div className="py-10 text-center text-xs text-muted">
                暂无输出文件（训练脚本可将产物写入{' '}
                <code className="rounded bg-subtle px-1 py-0.5 text-secondary">$COLAB_OUTPUT_DIR</code>
                ）
              </div>
            ) : (
              <div className="space-y-1.5">
                {outputs.map((item) => {
                  const active = selectedOutput === item.path
                  return (
                    <div
                      key={item.path}
                      className={`flex items-center justify-between gap-2 rounded-xl border px-3 py-2 text-xs transition ${
                        active
                          ? 'border-accent-line bg-accent-soft text-accent'
                          : 'border-line bg-inset text-secondary hover:border-control'
                      }`}
                    >
                      <button
                        type="button"
                        onClick={() => handlePreviewOutput(item.path)}
                        className="flex min-w-0 flex-1 items-center gap-2 text-left"
                      >
                        {isImageFile(item.path) ? (
                          <ImageIcon className="h-3.5 w-3.5 shrink-0 text-secondary" />
                        ) : (
                          <FileCode2 className="h-3.5 w-3.5 shrink-0 text-accent" />
                        )}
                        <span className="truncate font-mono">{item.path}</span>
                      </button>
                      <div className="flex shrink-0 items-center gap-2">
                        {item.size > 0 && (
                          <span className="font-mono text-[11px] text-muted">
                            {formatBytes(item.size)}
                          </span>
                        )}
                        <button
                          type="button"
                          onClick={() =>
                            triggerAuthenticatedDownload(
                              getOutputFileUrl(jobId, item.path),
                              item.path.split('/').pop() || 'output'
                            ).catch((e) => pushToast('error', e.message))
                          }
                          title="下载此文件"
                          className="rounded p-1 text-muted hover:bg-hover-surface hover:text-ink"
                        >
                          <Download className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </SectionCard>

          <SectionCard
            className="lg:col-span-2"
            title={selectedOutput ? `预览：${selectedOutput}` : '文件在线预览'}
            action={
              selectedOutput ? (
                <button
                  type="button"
                  onClick={() =>
                    triggerAuthenticatedDownload(
                      getOutputFileUrl(jobId, selectedOutput),
                      selectedOutput.split('/').pop() || 'output'
                    ).catch((e) => pushToast('error', e.message))
                  }
                  className="inline-flex items-center gap-1 rounded-lg border border-control bg-surface px-2.5 py-1 text-xs text-ink hover:bg-hover-surface"
                >
                  <Download className="h-3.5 w-3.5" />
                  下载文件
                </button>
              ) : undefined
            }
          >
            {!selectedOutput ? (
              <div className="py-20 text-center text-xs text-muted">
                从左侧点击图片（PNG/JPG/SVG）或文本指标文件（JSON/CSV/TXT/MD）在线预览
              </div>
            ) : previewLoading ? (
              <div className="py-20 text-center text-xs text-muted">正在加载预览...</div>
            ) : previewImageUrl ? (
              <div className="flex justify-center rounded-xl border border-line bg-editor p-4">
                <img
                  src={previewImageUrl}
                  alt={selectedOutput}
                  className="max-h-[500px] max-w-full rounded-lg object-contain"
                />
              </div>
            ) : previewText !== null ? (
              <pre className="max-h-[500px] overflow-auto rounded-xl border border-line bg-editor p-4 font-mono text-xs text-ink">
                {previewText}
              </pre>
            ) : (
              <div className="py-16 text-center text-xs text-muted">
                该二进制格式暂不支持网页直接预览，请点击右上角下载查看。
              </div>
            )}
          </SectionCard>
        </div>
      )}

      {/* Tab 3: Workspace & Config */}
      {tab === 'workspace' && (
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-3">
          <SectionCard title="任务参数配置">
            <div className="space-y-3.5 text-xs">
              <div>
                <div className="text-muted">优先级 / 超时</div>
                <div className="mt-1 font-mono text-ink">
                  priority={job.priority} · timeout={job.timeout_seconds}s
                </div>
              </div>
              <div>
                <div className="text-muted">CLI 启动参数 (script_args)</div>
                <div className="mt-1 font-mono text-ink">
                  {job.script_args.length ? job.script_args.join(' ') : '无'}
                </div>
              </div>
              <div>
                <div className="text-muted">Pip 依赖</div>
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {job.pip_packages.length ? (
                    job.pip_packages.map((p) => (
                      <span
                        key={p}
                        className="rounded-md border border-control bg-surface px-2 py-0.5 font-mono text-accent"
                      >
                        {p}
                      </span>
                    ))
                  ) : (
                    <span className="text-muted">无额外依赖</span>
                  )}
                </div>
              </div>
              <div>
                <div className="text-muted">数据集下载钩子 (dataset_commands)</div>
                {job.dataset_commands.length ? (
                  <pre className="mt-1 overflow-x-auto rounded-lg bg-inset p-2.5 font-mono text-xs text-secondary">
                    {job.dataset_commands.join('\n')}
                  </pre>
                ) : (
                  <div className="mt-1 text-muted">无</div>
                )}
              </div>
              <div>
                <div className="flex items-center justify-between text-muted">
                  <span>环境变量 (env_vars)</span>
                  {Object.keys(job.env_vars).length > 0 && (
                    <button
                      type="button"
                      onClick={() => setShowEnvValues((v) => !v)}
                      className="inline-flex items-center gap-1 text-xs text-accent hover:text-accent"
                    >
                      {showEnvValues ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
                      {showEnvValues ? '隐藏值' : '显示值'}
                    </button>
                  )}
                </div>
                {Object.keys(job.env_vars).length ? (
                  <div className="mt-1 space-y-1 rounded-lg bg-inset p-2.5 font-mono text-xs">
                    {Object.entries(job.env_vars).map(([k, v]) => (
                      <div key={k} className="flex justify-between gap-2">
                        <span className="text-accent">{k}</span>
                        <span className="truncate text-secondary">
                          {showEnvValues ? v : '••••••••'}
                        </span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="mt-1 text-muted">无</div>
                )}
              </div>
              {(job.drive_profile || job.drive_subpath || job.drive_sync_outputs_to) && (
                <div>
                  <div className="text-muted">Drive 参数</div>
                  <div className="mt-1 space-y-0.5 font-mono text-xs text-secondary">
                    <div>profile: {job.drive_profile || '—'}</div>
                    <div>subpath: {job.drive_subpath || '—'}</div>
                    <div>sync_to: {job.drive_sync_outputs_to || '—'}</div>
                  </div>
                </div>
              )}
            </div>
          </SectionCard>

          <SectionCard
            className="lg:col-span-2"
            title={`工作区文件 (${wsFiles.length})`}
            subtitle={selectedWsFile ? `当前查看: ${selectedWsFile}` : '选择文件查看源码'}
          >
            <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
              <div className="space-y-1 md:col-span-1">
                {wsFiles.map((wf) => (
                  <button
                    key={wf.path}
                    type="button"
                    onClick={() => setSelectedWsFile(wf.path)}
                    className={`flex w-full items-center justify-between rounded-lg border px-2.5 py-1.5 text-left text-xs transition ${
                      selectedWsFile === wf.path
                        ? 'border-accent-line bg-accent-soft text-accent'
                        : 'border-line bg-inset text-secondary hover:border-control'
                    }`}
                  >
                    <span className="truncate font-mono">{wf.path}</span>
                    <span className="ml-2 shrink-0 text-[11px] text-muted">
                      {formatBytes(wf.size)}
                    </span>
                  </button>
                ))}
              </div>
              <div className="md:col-span-3">
                {wsContent !== null ? (
                  <div>
                    {wsTruncated && (
                      <div className="mb-2 rounded-lg border border-warning-line bg-warning-soft px-3 py-1.5 text-xs text-warning">
                        文件超过 256 KB，仅展示前 256 KB 预览内容
                      </div>
                    )}
                    <pre className="max-h-[480px] overflow-auto rounded-xl border border-line bg-editor p-4 font-mono text-xs text-ink">
                      {wsContent}
                    </pre>
                  </div>
                ) : (
                  <div className="py-16 text-center text-xs text-muted">
                    从左侧选择文件查看源码
                  </div>
                )}
              </div>
            </div>
          </SectionCard>
        </div>
      )}
    </div>
  )
}

import type {
  Account,
  AccountAuthSession,
  AccountsResponse,
  AuthCheckResponse,
  HealthStatus,
  Job,
  JobListResponse,
  JobLogsResponse,
  JobSubmitPayload,
  PoolStats,
  ProxyConfig,
  ResourcesResponse,
  RuntimeHardware,
  WorkspaceInspectResponse,
} from '../types'
import { encodePathSegments } from '../utils/format'

const API_KEY_STORAGE_KEY = 'colab_pool_api_key'
const ACCOUNT_AUTH_STORAGE_KEY = 'colab_pool_pending_account_auth'

export function getPendingAccountAuthId(): string {
  try { return window.sessionStorage.getItem(ACCOUNT_AUTH_STORAGE_KEY) ?? '' }
  catch { return '' }
}

export function setPendingAccountAuthId(sessionId: string | null): void {
  try {
    if (sessionId) window.sessionStorage.setItem(ACCOUNT_AUTH_STORAGE_KEY, sessionId)
    else window.sessionStorage.removeItem(ACCOUNT_AUTH_STORAGE_KEY)
  } catch {
    // Authorization still works when browser storage is unavailable.
  }
}

export class ApiError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.status = status
    this.detail = detail
  }
}

let unauthorizedHandler: (() => void) | null = null

export function setUnauthorizedHandler(fn: (() => void) | null): void {
  unauthorizedHandler = fn
}

export function getStoredApiKey(): string {
  try {
    return window.localStorage.getItem(API_KEY_STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

export function setStoredApiKey(key: string): void {
  try {
    if (key.trim()) {
      window.localStorage.setItem(API_KEY_STORAGE_KEY, key.trim())
    } else {
      window.localStorage.removeItem(API_KEY_STORAGE_KEY)
    }
  } catch {
    // ignore storage errors
  }
}

function buildAuthHeaders(extra?: HeadersInit, explicitKey?: string): Headers {
  const headers = new Headers(extra)
  const key = explicitKey !== undefined ? explicitKey.trim() : getStoredApiKey()
  if (key) {
    headers.set('Authorization', `Bearer ${key}`)
  }
  return headers
}

async function requestJson<T>(
  url: string,
  init: RequestInit = {},
  options?: { skipUnauthorizedTrigger?: boolean; apiKeyOverride?: string }
): Promise<T> {
  const headers = buildAuthHeaders(init.headers, options?.apiKeyOverride)
  const res = await fetch(url, { ...init, headers })
  if (res.status === 401 && !options?.skipUnauthorizedTrigger) {
    unauthorizedHandler?.()
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const data = await res.json()
      if (data && typeof data.detail === 'string') {
        detail = data.detail
      } else if (data && Array.isArray(data.detail)) {
        detail = data.detail.map((d: { msg?: string }) => d.msg ?? JSON.stringify(d)).join('; ')
      }
    } catch {
      const text = await res.text().catch(() => '')
      if (text) detail = text.slice(0, 300)
    }
    throw new ApiError(res.status, detail)
  }
  return (await res.json()) as T
}

export async function fetchAuthCheck(apiKeyOverride?: string): Promise<AuthCheckResponse> {
  return requestJson<AuthCheckResponse>(
    '/api/v1/auth/check',
    {},
    { skipUnauthorizedTrigger: true, apiKeyOverride }
  )
}

export async function fetchHealth(): Promise<HealthStatus> {
  return requestJson<HealthStatus>('/healthz', {}, { skipUnauthorizedTrigger: true })
}

export async function fetchStats(): Promise<PoolStats> {
  return requestJson<PoolStats>('/api/v1/stats')
}

export async function fetchAccounts(): Promise<AccountsResponse> {
  return requestJson<AccountsResponse>('/api/v1/accounts')
}

export async function fetchProxies(): Promise<{ proxies: ProxyConfig[] }> {
  return requestJson<{ proxies: ProxyConfig[] }>('/api/v1/proxies')
}

export async function createProxy(input: { name: string; url: string }): Promise<ProxyConfig> {
  return requestJson<ProxyConfig>('/api/v1/proxies', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
}

export async function updateProxy(
  proxyId: string,
  patch: { name?: string; url?: string }
): Promise<ProxyConfig> {
  return requestJson<ProxyConfig>(`/api/v1/proxies/${encodeURIComponent(proxyId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
}

export async function deleteProxy(proxyId: string): Promise<{ deleted: string }> {
  return requestJson<{ deleted: string }>(`/api/v1/proxies/${encodeURIComponent(proxyId)}`, {
    method: 'DELETE',
  })
}

export async function fetchResources(): Promise<ResourcesResponse> {
  return requestJson<ResourcesResponse>('/api/v1/resources')
}

export async function refreshAccountUsage(name: string): Promise<Account> {
  return requestJson<Account>(`/api/v1/accounts/${encodeURIComponent(name)}/usage/refresh`, {
    method: 'POST',
  })
}

export async function refreshPoolUsage(): Promise<{ accounts: Account[] }> {
  return requestJson<{ accounts: Account[] }>('/api/v1/pool/usage/refresh', { method: 'POST' })
}

export async function registerAccount(name: string, tier: string): Promise<Account> {
  return requestJson<Account>('/api/v1/accounts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, tier }),
  })
}

export async function startAccountAuth(
  name: string, tier: string, proxyId?: string | null
): Promise<AccountAuthSession> {
  const body: { name: string; tier: string; proxy_id?: string | null } = { name, tier }
  if (proxyId !== undefined) body.proxy_id = proxyId
  return requestJson<AccountAuthSession>('/api/v1/account-auth', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export async function fetchAccountAuth(sessionId: string): Promise<AccountAuthSession> {
  return requestJson<AccountAuthSession>(`/api/v1/account-auth/${encodeURIComponent(sessionId)}`)
}

export async function submitAccountAuthCode(sessionId: string, code: string): Promise<AccountAuthSession> {
  return requestJson<AccountAuthSession>(`/api/v1/account-auth/${encodeURIComponent(sessionId)}/code`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  })
}

export async function cancelAccountAuth(sessionId: string): Promise<AccountAuthSession> {
  return requestJson<AccountAuthSession>(`/api/v1/account-auth/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  })
}

export async function updateAccount(
  name: string,
  patch: { tier?: string; enabled?: boolean; clear_cooldown?: boolean; proxy_id?: string | null }
): Promise<Account> {
  return requestJson<Account>(`/api/v1/accounts/${encodeURIComponent(name)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
}

export async function deleteAccount(name: string, removeDisk = false): Promise<{ deleted: string }> {
  const qs = removeDisk ? '?remove_disk=true' : ''
  return requestJson<{ deleted: string }>(`/api/v1/accounts/${encodeURIComponent(name)}${qs}`, {
    method: 'DELETE',
  })
}

export async function syncPoolAccounts(): Promise<{ accounts: Account[] }> {
  return requestJson<{ accounts: Account[] }>('/api/v1/pool/sync', { method: 'POST' })
}

export async function cleanupOrphanRuntimes(): Promise<{ results: Record<string, string> }> {
  return requestJson<{ results: Record<string, string> }>('/api/v1/pool/cleanup', {
    method: 'POST',
  })
}

export async function fetchJobs(params: {
  status?: string
  gpu?: string
  account?: string
  search?: string
  limit?: number
  offset?: number
}): Promise<JobListResponse> {
  const sp = new URLSearchParams()
  if (params.status) sp.set('status', params.status)
  if (params.gpu) sp.set('gpu', params.gpu)
  if (params.account) sp.set('account', params.account)
  if (params.search) sp.set('search', params.search)
  if (params.limit != null) sp.set('limit', String(params.limit))
  if (params.offset != null) sp.set('offset', String(params.offset))
  const qs = sp.toString()
  return requestJson<JobListResponse>(`/api/v1/jobs${qs ? `?${qs}` : ''}`)
}

export async function fetchJob(jobId: string): Promise<Job> {
  return requestJson<Job>(`/api/v1/jobs/${encodeURIComponent(jobId)}`)
}

export async function submitJobJson(payload: JobSubmitPayload): Promise<Job> {
  return requestJson<Job>('/api/v1/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export async function submitJobMultipart(
  files: File[],
  spec: Omit<JobSubmitPayload, 'script_content' | 'extra_files'>
): Promise<Job> {
  const fd = new FormData()
  for (const file of files) {
    fd.append('files', file, file.name)
  }
  fd.append('spec_json', JSON.stringify(spec))
  return requestJson<Job>('/api/v1/jobs/upload', {
    method: 'POST',
    body: fd,
  })
}

export async function cancelJob(jobId: string): Promise<Job> {
  return requestJson<Job>(`/api/v1/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: 'POST',
  })
}

export async function retryJob(
  jobId: string,
  overrides?: { gpu?: string; priority?: number }
): Promise<Job> {
  return requestJson<Job>(`/api/v1/jobs/${encodeURIComponent(jobId)}/retry`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(overrides ?? {}),
  })
}

export async function deleteJob(jobId: string): Promise<{ deleted: string }> {
  return requestJson<{ deleted: string }>(`/api/v1/jobs/${encodeURIComponent(jobId)}`, {
    method: 'DELETE',
  })
}

export async function fetchJobLogs(
  jobId: string,
  options: { tail_lines?: number; offset?: number } = {}
): Promise<JobLogsResponse> {
  const sp = new URLSearchParams()
  if (options.offset != null) sp.set('offset', String(options.offset))
  if (options.tail_lines != null) sp.set('tail_lines', String(options.tail_lines))
  const qs = sp.toString()
  return requestJson<JobLogsResponse>(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/logs${qs ? `?${qs}` : ''}`
  )
}

export async function fetchJobWorkspace(
  jobId: string,
  file?: string
): Promise<WorkspaceInspectResponse> {
  const qs = file ? `?file=${encodeURIComponent(file)}` : ''
  return requestJson<WorkspaceInspectResponse>(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/workspace${qs}`
  )
}

export async function fetchAuthenticatedBlob(url: string): Promise<Blob> {
  const res = await fetch(url, { headers: buildAuthHeaders() })
  if (res.status === 401) {
    unauthorizedHandler?.()
  }
  if (!res.ok) {
    throw new ApiError(res.status, `下载失败 (HTTP ${res.status})`)
  }
  return await res.blob()
}

export function getOutputFileUrl(jobId: string, relPath: string): string {
  return `/api/v1/jobs/${encodeURIComponent(jobId)}/files/${encodePathSegments(relPath)}`
}

export function getArtifactsArchiveUrl(jobId: string): string {
  return `/api/v1/jobs/${encodeURIComponent(jobId)}/artifacts`
}

export async function triggerAuthenticatedDownload(url: string, filename: string): Promise<void> {
  const blob = await fetchAuthenticatedBlob(url)
  const objectUrl = URL.createObjectURL(blob)
  try {
    const a = document.createElement('a')
    a.href = objectUrl
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
  } finally {
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
  }
}

export interface LogStreamCallbacks {
  onLog: (chunk: string, offset: number) => void
  onStatus?: (payload: {
    status: string
    assigned_account: string
    gpu_detected: string
    runtime_info: RuntimeHardware
    exit_code: number | null
    error_message: string
  }) => void
  onDone?: (payload: { status: string; offset: number }) => void
  onError?: (err: Error) => void
}

export async function streamJobLogs(
  jobId: string,
  offset: number,
  callbacks: LogStreamCallbacks,
  signal: AbortSignal
): Promise<void> {
  try {
    const res = await fetch(
      `/api/v1/jobs/${encodeURIComponent(jobId)}/logs/stream?offset=${offset}`,
      {
        headers: buildAuthHeaders({ Accept: 'text/event-stream' }),
        signal,
      }
    )
    if (res.status === 401) {
      unauthorizedHandler?.()
      throw new ApiError(401, 'API Key 无效或已过期')
    }
    if (!res.ok || !res.body) {
      throw new ApiError(res.status, `日志流连接失败 (HTTP ${res.status})`)
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''

    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      buffer = buffer.replace(/\r\n/g, '\n')

      let sepIdx = buffer.indexOf('\n\n')
      while (sepIdx !== -1) {
        const rawBlock = buffer.slice(0, sepIdx)
        buffer = buffer.slice(sepIdx + 2)
        sepIdx = buffer.indexOf('\n\n')

        if (!rawBlock.trim() || rawBlock.startsWith(':')) continue

        let eventName = 'message'
        const dataLines: string[] = []
        for (const line of rawBlock.split('\n')) {
          if (line.startsWith(':')) continue
          if (line.startsWith('event:')) {
            eventName = line.slice(6).trim()
          } else if (line.startsWith('data:')) {
            dataLines.push(line.slice(5).trimStart())
          }
        }
        if (dataLines.length === 0) continue

        const dataRaw = dataLines.join('\n')
        try {
          const parsed = JSON.parse(dataRaw)
          if (eventName === 'log') {
            callbacks.onLog(parsed.chunk ?? '', Number(parsed.offset ?? offset))
          } else if (eventName === 'status') {
            callbacks.onStatus?.(parsed)
          } else if (eventName === 'done') {
            callbacks.onDone?.(parsed)
          }
        } catch {
          // ignore malformed SSE chunk
        }
      }
    }
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') return
    callbacks.onError?.(err instanceof Error ? err : new Error(String(err)))
  }
}

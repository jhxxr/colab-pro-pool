export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let idx = 0
  let value = bytes
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024
    idx += 1
  }
  return `${value >= 10 || idx === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[idx]}`
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return '—'
  const total = Math.floor(seconds)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h}h ${m}m ${s}s`
  if (m > 0) return `${m}m ${s}s`
  return `${s}s`
}

export function formatTimestamp(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return '—'
  const d = new Date(epochSeconds * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

export function formatRelativeTime(epochSeconds: number | null | undefined, nowMs = Date.now()): string {
  if (!epochSeconds) return '从未'
  const diff = Math.max(0, Math.floor(nowMs / 1000 - epochSeconds))
  if (diff < 60) return `${diff} 秒前`
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}

export function jobElapsedSeconds(
  job: { started_at: number | null; finished_at: number | null; created_at: number; status: string },
  nowMs = Date.now()
): number | null {
  if (!job.started_at) return null
  const end = job.finished_at ?? nowMs / 1000
  return Math.max(0, end - job.started_at)
}

export function isImageFile(path: string): boolean {
  const lower = path.toLowerCase()
  return ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'].some((ext) => lower.endsWith(ext))
}

export function isTextPreviewable(path: string): boolean {
  const lower = path.toLowerCase()
  return [
    '.json',
    '.csv',
    '.tsv',
    '.txt',
    '.log',
    '.md',
    '.yaml',
    '.yml',
    '.py',
    '.sh',
    '.toml',
    '.ini',
    '.cfg',
    '.html',
  ].some((ext) => lower.endsWith(ext))
}

export function encodePathSegments(relPath: string): string {
  return relPath
    .split('/')
    .filter(Boolean)
    .map((seg) => encodeURIComponent(seg))
    .join('/')
}

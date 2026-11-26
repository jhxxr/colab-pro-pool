import type { ReactNode } from 'react'

interface BadgeStyle { label: string; cls: string; dot: string }

const JOB_STATUS_STYLES: Record<string, BadgeStyle> = {
  queued: { label: '排队中', cls: 'bg-subtle text-secondary', dot: 'bg-muted' },
  provisioning: { label: '分配中', cls: 'bg-warning-soft text-warning', dot: 'bg-warning' },
  running: { label: '运行中', cls: 'bg-accent-soft text-accent', dot: 'bg-accent' },
  succeeded: { label: '已完成', cls: 'bg-success-soft text-success', dot: 'bg-success' },
  failed: { label: '失败', cls: 'bg-danger-soft text-danger', dot: 'bg-danger' },
  cancelled: { label: '已取消', cls: 'bg-subtle text-secondary', dot: 'bg-muted' },
}

const ACCOUNT_STATUS_STYLES: Record<string, BadgeStyle> = {
  idle: { label: '空闲', cls: 'bg-success-soft text-success', dot: 'bg-success' },
  busy: { label: '忙碌', cls: 'bg-accent-soft text-accent', dot: 'bg-accent' },
  cooldown: { label: '冷却中', cls: 'bg-warning-soft text-warning', dot: 'bg-warning' },
  maintenance: { label: '维护中', cls: 'bg-warning-soft text-warning', dot: 'bg-warning' },
  disabled: { label: '已停用', cls: 'bg-subtle text-muted', dot: 'bg-muted' },
}

function StatusBadge({ config }: { config: BadgeStyle }) {
  return (
    <span className={`status-badge ${config.cls}`}>
      <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${config.dot}`} />
      {config.label}
    </span>
  )
}

export function JobStatusBadge({ status }: { status: string }) {
  return <StatusBadge config={JOB_STATUS_STYLES[status] ?? { label: status, cls: 'bg-subtle text-secondary', dot: 'bg-muted' }} />
}

export function AccountStatusBadge({ status, enabled = true }: { status: string; enabled?: boolean | number }) {
  const effective = enabled ? status : 'disabled'
  return <StatusBadge config={ACCOUNT_STATUS_STYLES[effective] ?? ACCOUNT_STATUS_STYLES.idle} />
}

export function TierBadge({ tier }: { tier: string }) {
  return (
    <span className="inline-flex items-center whitespace-nowrap rounded-md border border-line bg-inset px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-secondary">
      {tier.toLowerCase()}
    </span>
  )
}

export function GpuBadge({ gpu, tpu, highMem }: { gpu: string; tpu?: string; highMem?: boolean }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <span className="inline-flex items-center whitespace-nowrap rounded-md border border-line bg-inset px-2 py-0.5 font-mono text-xs font-medium text-secondary">
        {tpu ? `TPU ${tpu}` : gpu.toUpperCase()}
      </span>
      {highMem && <span className="whitespace-nowrap rounded border border-line bg-inset px-1.5 py-0.5 text-[11px] text-muted">高内存</span>}
    </span>
  )
}

export function SectionCard({ title, subtitle, action, children, className = '' }: {
  title?: string
  subtitle?: string
  action?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`section-card ${className}`}>
      {(title || action) && (
        <div className="section-heading">
          <div className="min-w-0">
            {title && <h2 className="text-[15px] font-semibold tracking-tight text-ink">{title}</h2>}
            {subtitle && <p className="mt-1 text-xs leading-relaxed text-muted">{subtitle}</p>}
          </div>
          {action}
        </div>
      )}
      <div className="section-body">{children}</div>
    </section>
  )
}

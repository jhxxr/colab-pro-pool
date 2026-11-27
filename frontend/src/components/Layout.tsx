import { useEffect, useRef, useState } from 'react'
import {
  ArrowUpRight,
  ChevronRight,
  Cpu,
  FolderKanban,
  LayoutDashboard,
  KeyRound,
  Menu,
  Microchip,
  Network,
  Plus,
  Settings2,
  SquareTerminal,
  Users,
  X,
} from 'lucide-react'
import type { HealthStatus } from '../types'

export type NavTab = 'overview' | 'jobs' | 'submit' | 'resources' | 'accounts' | 'proxies' | 'keys' | 'connect'

interface LayoutProps {
  activeTab: NavTab
  onNavigate: (tab: NavTab) => void
  health: HealthStatus | null
  healthError: boolean
  children: React.ReactNode
}

const NAV_ITEMS: { id: NavTab; label: string; icon: typeof LayoutDashboard }[] = [
  { id: 'overview', label: '算力总览', icon: LayoutDashboard },
  { id: 'jobs', label: '任务中心', icon: FolderKanban },
  { id: 'submit', label: '提交实验', icon: SquareTerminal },
  { id: 'resources', label: '型号与价格', icon: Microchip },
  { id: 'accounts', label: '账号与云盘', icon: Users },
  { id: 'proxies', label: '代理管理', icon: Network },
  { id: 'keys', label: 'MCP 密钥', icon: KeyRound },
  { id: 'connect', label: '连接与接入', icon: Settings2 },
]

export function Layout({ activeTab, onNavigate, health, healthError, children }: LayoutProps) {
  const [mobileOpen, setMobileOpen] = useState(false)
  const drawerRef = useRef<HTMLDialogElement>(null)
  const activeLabel = NAV_ITEMS.find((item) => item.id === activeTab)?.label ?? '算力总览'

  useEffect(() => {
    const drawer = drawerRef.current
    if (mobileOpen) drawer?.showModal()
    else drawer?.close()
  }, [mobileOpen])

  useEffect(() => {
    const desktop = window.matchMedia('(min-width: 1024px)')
    const closeDrawer = () => { if (desktop.matches) setMobileOpen(false) }
    desktop.addEventListener('change', closeDrawer)
    return () => desktop.removeEventListener('change', closeDrawer)
  }, [])

  const handleSelect = (tab: NavTab) => {
    setMobileOpen(false)
    onNavigate(tab)
  }

  const sidebarContent = (mobile = false) => (
    <div className="flex min-h-full flex-col">
      <div className="flex items-center justify-between px-5 pt-7 pb-6">
        <button type="button" onClick={() => handleSelect('overview')} className="flex items-center gap-3 text-left">
          <span className="flex h-10 w-10 items-center justify-center rounded-xl border border-line bg-surface shadow-panel">
            <Cpu aria-hidden="true" className="h-5 w-5 text-secondary" strokeWidth={1.6} />
          </span>
          <span>
            <span className="block text-sm font-semibold tracking-tight text-ink">Colab Pro Pool</span>
            <span className="mt-0.5 block text-xs text-muted">云端算力工作区</span>
          </span>
        </button>
        {mobile && (
          <button type="button" onClick={() => setMobileOpen(false)} aria-label="关闭菜单" className="flex h-10 w-10 items-center justify-center rounded-lg text-muted hover:bg-subtle">
            <X aria-hidden="true" className="h-5 w-5" />
          </button>
        )}
      </div>

      <div className="px-4">
        <button type="button" onClick={() => handleSelect('submit')} className="button-secondary w-full justify-start border-control/60 bg-surface/80">
          <Plus aria-hidden="true" className="h-4 w-4" />
          新建实验
        </button>
      </div>

      <div className="px-6 pt-8 pb-2 text-[11px] font-medium tracking-wide text-muted">工作区</div>
      <nav aria-label={mobile ? '移动端导航' : '主导航'} className="space-y-1 px-3">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon
          return (
            <button key={item.id} type="button" aria-current={activeTab === item.id ? 'page' : undefined}
              onClick={() => handleSelect(item.id)} className="nav-item">
              <Icon aria-hidden="true" className="h-[18px] w-[18px] shrink-0" strokeWidth={1.7} />
              {item.label}
            </button>
          )
        })}
      </nav>

      <div className="mt-auto px-5 pt-12 pb-6">
        <div className="border-t border-line pt-4">
          <div className="flex items-center justify-between text-xs">
            <span className="text-secondary">调度服务</span>
            <span className={`inline-flex items-center gap-1.5 ${healthError ? 'text-danger' : health ? 'text-success' : 'text-muted'}`}>
              <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${healthError ? 'bg-danger' : health ? 'bg-success' : 'bg-muted'}`} />
              {healthError ? '离线' : health ? '已连接' : '连接中'}
            </span>
          </div>
          {health && (
            <div className="mt-4 grid grid-cols-2 gap-3 text-xs text-muted">
              <div><span className="block">可用账号</span><span className="mt-1 block text-sm font-medium text-ink tabular-nums">{health.accounts_idle} <span className="font-normal text-muted">/ {health.accounts_total}</span></span></div>
              <div><span className="block">运行 / 等待</span><span className="mt-1 block text-sm font-medium text-ink tabular-nums">{health.jobs_running + health.jobs_provisioning} <span className="font-normal text-muted">/ {health.jobs_queued}</span></span></div>
            </div>
          )}
          <div className="mt-5 text-[11px] text-muted">Colab Pro Pool <span className="ml-1">{health?.version ?? '0.3.0'}</span></div>
        </div>
      </div>
    </div>
  )

  return (
    <div className="flex min-h-dvh bg-canvas text-ink">
      <a href="#main-content" className="sr-only fixed top-3 left-3 z-50 rounded-lg bg-surface px-4 py-2 text-accent shadow-floating focus:not-sr-only">跳到主要内容</a>
      <aside className="hidden w-60 shrink-0 border-r border-line bg-sidebar lg:block">
        <div className="sticky top-0 h-dvh">{sidebarContent()}</div>
      </aside>
      <dialog ref={drawerRef} aria-label="工作区导航" onCancel={(event) => { event.preventDefault(); setMobileOpen(false) }}
        onClick={(event) => { if (event.target === event.currentTarget) setMobileOpen(false) }}
        className="fixed inset-y-0 left-0 m-0 h-dvh max-h-dvh w-72 max-w-[85vw] overflow-y-auto border-0 border-r border-line bg-sidebar p-0 text-ink shadow-floating backdrop:bg-scrim">
        {sidebarContent(true)}
      </dialog>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-16 items-center justify-between gap-3 border-b border-line bg-canvas/90 px-4 backdrop-blur-xl sm:px-7 lg:px-9">
          <div className="flex min-w-0 items-center gap-3 text-xs">
            <button type="button" onClick={() => setMobileOpen(true)} aria-label="打开菜单" aria-expanded={mobileOpen}
              className="flex h-10 w-10 items-center justify-center rounded-lg text-secondary hover:bg-subtle lg:hidden">
              <Menu aria-hidden="true" className="h-[18px] w-[18px]" />
            </button>
            <span className="hidden text-muted sm:inline">工作区</span>
            <ChevronRight aria-hidden="true" className="hidden h-3.5 w-3.5 text-muted sm:block" />
            <span className="truncate font-medium text-secondary">{activeLabel}</span>
          </div>
          <button type="button" onClick={() => handleSelect('submit')} className="inline-flex min-h-10 shrink-0 items-center gap-1.5 text-xs font-medium text-accent hover:text-accent-hover">
            新建实验 <ArrowUpRight aria-hidden="true" className="h-3.5 w-3.5" />
          </button>
        </header>
        <main id="main-content" tabIndex={-1} className="workspace-content mx-auto w-full max-w-[1440px] min-w-0 flex-1 px-4 py-7 focus:outline-none sm:px-7 sm:py-9 lg:px-9 lg:py-10">
          {children}
        </main>
      </div>
    </div>
  )
}

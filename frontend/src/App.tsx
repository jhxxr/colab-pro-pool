import { useCallback, useEffect, useState } from 'react'
import {
  fetchAuthCheck,
  fetchHealth,
  setStoredApiKey,
  setUnauthorizedHandler,
} from './api/client'
import { AuthGate } from './components/AuthGate'
import { Layout, type NavTab } from './components/Layout'
import { ToastProvider } from './components/Toast'
import { AccountsPage } from './pages/AccountsPage'
import { ConnectPage } from './pages/ConnectPage'
import { JobDetailPage } from './pages/JobDetailPage'
import { JobsPage } from './pages/JobsPage'
import { OverviewPage } from './pages/OverviewPage'
import { ProxiesPage } from './pages/ProxiesPage'
import { ResourcesPage } from './pages/ResourcesPage'
import { SubmitPage } from './pages/SubmitPage'
import type { HealthStatus, JobSubmitPayload } from './types'

type RouteState =
  | { tab: 'overview' }
  | { tab: 'jobs'; jobId?: string }
  | { tab: 'submit' }
  | { tab: 'resources' }
  | { tab: 'accounts' }
  | { tab: 'proxies' }
  | { tab: 'connect' }

function parseLocationRoute(): RouteState {
  const path = window.location.pathname.replace(/\/+$/, '') || '/'
  if (path === '/jobs') return { tab: 'jobs' }
  if (path.startsWith('/jobs/')) {
    const id = decodeURIComponent(path.slice('/jobs/'.length))
    if (id) return { tab: 'jobs', jobId: id }
    return { tab: 'jobs' }
  }
  if (path === '/submit') return { tab: 'submit' }
  if (path === '/accounts') return { tab: 'accounts' }
  if (path === '/proxies') return { tab: 'proxies' }
  if (path === '/resources') return { tab: 'resources' }
  if (path === '/connect') return { tab: 'connect' }
  return { tab: 'overview' }
}

function routeToPath(route: RouteState): string {
  if (route.tab === 'overview') return '/'
  if (route.tab === 'jobs') {
    return route.jobId ? `/jobs/${encodeURIComponent(route.jobId)}` : '/jobs'
  }
  return `/${route.tab}`
}

function AppShell() {
  const [route, setRoute] = useState<RouteState>(() => parseLocationRoute())
  const [authChecked, setAuthChecked] = useState(false)
  const [authRequired, setAuthRequired] = useState(false)
  const [authenticated, setAuthenticated] = useState(false)
  const [authError, setAuthError] = useState('')
  const [health, setHealth] = useState<HealthStatus | null>(null)
  const [healthError, setHealthError] = useState(false)
  const [submitPreset, setSubmitPreset] = useState<Partial<JobSubmitPayload> | null>(null)

  const navigate = useCallback((next: RouteState) => {
    const targetPath = routeToPath(next)
    if (window.location.pathname !== targetPath) {
      window.history.pushState(null, '', targetPath)
    }
    setRoute(next)
  }, [])

  useEffect(() => {
    const onPopState = () => setRoute(parseLocationRoute())
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const verifyAuth = useCallback(async () => {
    try {
      const res = await fetchAuthCheck()
      setAuthRequired(res.auth_required)
      setAuthenticated(res.authenticated)
      setAuthError('')
    } catch (err) {
      setAuthenticated(false)
      setAuthError(err instanceof Error ? err.message : '无法连接到调度服务端')
    } finally {
      setAuthChecked(true)
    }
  }, [])

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setAuthRequired(true)
      setAuthenticated(false)
      setAuthError('会话未授权或 API Key 已变更，请重新输入')
    })
    verifyAuth()
    return () => setUnauthorizedHandler(null)
  }, [verifyAuth])

  useEffect(() => {
    const pollHealth = async () => {
      try {
        const h = await fetchHealth()
        setHealth(h)
        setHealthError(false)
      } catch {
        setHealthError(true)
      }
    }
    pollHealth()
    const timer = setInterval(pollHealth, 5000)
    return () => clearInterval(timer)
  }, [])

  if (!authChecked) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-canvas text-sm text-muted">
        正在连接 Colab Pro Pool 服务端...
      </div>
    )
  }

  if (authRequired && !authenticated) {
    return (
      <AuthGate
        initialError={authError}
        onAuthenticated={() => {
          setAuthenticated(true)
          setAuthError('')
        }}
      />
    )
  }

  const activeTab: NavTab = route.tab

  return (
    <Layout
      activeTab={activeTab}
      onNavigate={(tab) => navigate({ tab })}
      health={health}
      healthError={healthError}
    >
      {route.tab === 'overview' && (
        <OverviewPage
          onOpenJob={(jobId) => navigate({ tab: 'jobs', jobId })}
          onGoJobs={() => navigate({ tab: 'jobs' })}
          onGoAccounts={() => navigate({ tab: 'accounts' })}
          onGoSubmit={() => navigate({ tab: 'submit' })}
        />
      )}
      {route.tab === 'jobs' && !route.jobId && (
        <JobsPage
          onOpenJob={(jobId) => navigate({ tab: 'jobs', jobId })}
          onGoSubmit={() => navigate({ tab: 'submit' })}
        />
      )}
      {route.tab === 'jobs' && route.jobId && (
        <JobDetailPage
          jobId={route.jobId}
          onBack={() => navigate({ tab: 'jobs' })}
          onOpenJob={(jobId) => navigate({ tab: 'jobs', jobId })}
          onCloneToSubmit={(preset) => {
            setSubmitPreset(preset)
            navigate({ tab: 'submit' })
          }}
        />
      )}
      {route.tab === 'submit' && (
        <SubmitPage
          initialPreset={submitPreset}
          onClearPreset={() => setSubmitPreset(null)}
          onSubmitted={(jobId) => navigate({ tab: 'jobs', jobId })}
        />
      )}
      {route.tab === 'accounts' && <AccountsPage />}
      {route.tab === 'proxies' && <ProxiesPage />}
      {route.tab === 'resources' && <ResourcesPage onSelect={(preset) => {
        setSubmitPreset(preset)
        navigate({ tab: 'submit' })
      }} />}
      {route.tab === 'connect' && (
        <ConnectPage
          authRequired={authRequired}
          onApiKeyUpdated={verifyAuth}
          onSignOut={() => {
            setStoredApiKey('')
            setAuthenticated(false)
          }}
        />
      )}
    </Layout>
  )
}

export default function App() {
  return (
    <ToastProvider>
      <AppShell />
    </ToastProvider>
  )
}

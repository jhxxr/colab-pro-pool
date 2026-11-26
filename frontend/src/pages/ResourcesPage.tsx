import { useCallback, useEffect, useState } from 'react'
import { ArrowUpRight, Cpu, ExternalLink, RefreshCw } from 'lucide-react'
import { fetchResources } from '../api/client'
import { SectionCard } from '../components/StatusBadge'
import { useToast } from '../components/Toast'
import type { ComputeResource, JobSubmitPayload, ResourceProfile, ResourcesResponse } from '../types'
import { formatTimestamp } from '../utils/format'
import { detectedGpu, hardwareSummary } from '../utils/hardware'
import { CU_COST_STORAGE_KEY, hourlyCost, readCuCost, validCuCost } from '../utils/pricing'

export function ResourcesPage({ onSelect }: { onSelect: (preset: Partial<JobSubmitPayload>) => void }) {
  const { pushToast } = useToast()
  const [data, setData] = useState<ResourcesResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('all')
  const [cost, setCost] = useState(readCuCost)

  const load = useCallback(async () => {
    try { setData(await fetchResources()) }
    catch (err) { pushToast('error', err instanceof Error ? err.message : '读取算力型号失败') }
    finally { setLoading(false) }
  }, [pushToast])

  useEffect(() => {
    void load()
    const interval = setInterval(load, 15000)
    return () => clearInterval(interval)
  }, [load])

  useEffect(() => {
    try { localStorage.setItem(CU_COST_STORAGE_KEY, JSON.stringify(cost)) } catch { /* Optional local preference. */ }
  }, [cost])

  const validCost = validCuCost(cost)
  const items = (data?.resources ?? []).filter((item) => filter === 'all' || item.kind === filter)

  const price = (profile?: ResourceProfile, kind?: ComputeResource['kind']) => (
    <div>
      <div className="font-semibold text-ink tabular-nums">
        {profile ? <>{profile.consumption_rate_hourly.toFixed(2)} <span className="text-xs font-normal text-muted">CU/h</span></> : <span className="text-sm font-normal text-muted">待实测</span>}
      </div>
      {profile && validCost && <div className="mt-1 text-xs text-muted tabular-nums">约 {hourlyCost(profile.consumption_rate_hourly, cost)} / 小时</div>}
      <p className="mt-1 text-xs text-muted">{profile ? `采集于 ${formatTimestamp(profile.observed_at)}` : kind === 'TPU' ? '需确认芯片型号后采集' : '运行后自动记录单实例费率'}</p>
    </div>
  )

  const configuration = (item: ComputeResource) => {
    const latest = item.profiles[0]
    return (
      <div>
        <div className="text-sm text-ink">{latest ? hardwareSummary(latest.runtime_info) || '主机配置未采集' : 'CPU / 内存 / 磁盘：运行后采集'}</div>
        {latest && <p className="mt-1 text-xs text-secondary">{detectedGpu(latest.runtime_info) || latest.actual_model}</p>}
        <p className="mt-1 text-xs text-muted">{latest?.high_mem ? '高内存请求' : latest ? '标准内存请求' : '具体资源随分配变化'}</p>
        {item.profiles.length > 1 && (
          <details className="mt-2 text-xs text-secondary">
            <summary className="cursor-pointer text-accent">另有 {item.profiles.length - 1} 种实测配置</summary>
            <div className="mt-2 space-y-2">
              {item.profiles.slice(1).map((profile) => (
                <div key={profile.job_id} className="rounded-lg border border-line bg-inset p-2">
                  <p>{profile.high_mem ? '高内存请求' : '标准内存请求'} · {hardwareSummary(profile.runtime_info) || profile.actual_model}</p>
                  <p className="mt-1">{detectedGpu(profile.runtime_info) || profile.actual_model}</p>
                  <div className="mt-1">{price(profile)}</div>
                </div>
              ))}
            </div>
          </details>
        )}
      </div>
    )
  }

  const select = (item: ComputeResource) => (
    <button type="button" onClick={() => onSelect({ gpu: item.gpu, tpu: item.tpu, high_mem: item.profiles[0]?.high_mem ?? false })}
      aria-label={`使用 ${item.id} 新建实验`} className="button-secondary whitespace-nowrap">
      使用 <ArrowUpRight aria-hidden="true" className="h-3.5 w-3.5" />
    </button>
  )

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div><h1 className="page-title">算力型号与价格</h1><p className="page-description">比较硬件配置和每小时消耗，选择适合实验的机器。</p></div>
        <button type="button" onClick={() => { setLoading(true); void load() }} disabled={loading} className="button-secondary">
          <RefreshCw aria-hidden="true" className={`h-3.5 w-3.5 ${loading ? 'animate-spin motion-reduce:animate-none' : ''}`} />刷新目录
        </button>
      </div>

      <SectionCard title="可请求的机器型号" subtitle="显存 / HBM 是芯片参考值；主机配置与费率按实际分配采集。">
        <div className="mb-4 flex flex-wrap items-center gap-2 rounded-xl bg-inset px-3 py-2.5 text-xs text-muted">
          <label htmlFor="cu-unit-cost">可选换算 · 每 CU 成本</label>
          <input id="cu-unit-cost" type="number" min="0" max="1000000000" step="any" inputMode="decimal" value={cost.amount}
            onChange={(event) => setCost((current) => ({ ...current, amount: event.target.value }))}
            placeholder="实付金额 ÷ CU 数" className="w-40 rounded-lg border border-control bg-surface px-2.5 py-2 text-xs text-ink" />
          <select aria-label="费用换算币种" value={cost.currency} onChange={(event) => setCost((current) => ({ ...current, currency: event.target.value }))}
            className="rounded-lg border border-control bg-surface px-2.5 py-2 text-xs text-ink"><option value="CNY">人民币</option><option value="USD">美元</option></select>
          <span>仅在此浏览器保存</span>
        </div>
        {cost.amount && !validCost && <p className="mb-4 text-xs text-danger" role="alert">请输入有效的非负成本。</p>}
        <div className="mb-5 flex flex-wrap gap-2" role="group" aria-label="筛选算力类型">
          {[['all', '全部'], ['GPU', 'GPU'], ['TPU', 'TPU'], ['CPU', 'CPU']].map(([value, label]) => (
            <button type="button" key={value} aria-pressed={filter === value} onClick={() => setFilter(value)}
              className={`min-h-10 rounded-lg border px-4 py-2 text-xs font-medium ${filter === value ? 'border-control bg-subtle text-ink' : 'border-line bg-surface text-muted hover:bg-hover-surface'}`}>{label}</button>
          ))}
        </div>
        {!data && <p className="py-12 text-center text-sm text-muted">{loading ? '正在读取型号目录…' : '暂时无法读取型号，请刷新重试。'}</p>}
        <div className="hidden overflow-x-auto md:block">
          {data && <table className="data-table w-full min-w-[850px] text-left text-sm">
            <thead><tr className="border-b border-line text-xs text-muted">
              {['型号 / 推荐账号', '参考显存 / HBM', '最近实测配置', '参考小时价格', ''].map((label, index) => <th key={index} scope="col" className="px-3 pb-3 font-medium">{label || <span className="sr-only">操作</span>}</th>)}
            </tr></thead>
            <tbody className="divide-y divide-line">{items.map((item) => <tr key={item.id}>
              <th scope="row" className="px-3 py-5 align-top font-normal"><div className="font-semibold text-ink">{item.name}</div><p className="mt-1 text-xs text-muted">{item.kind} · {item.recommended_tier}</p><p className="mt-2 max-w-48 text-xs text-muted">{item.description}</p></th>
              <td className="px-3 py-5 align-top"><div className="text-ink">{item.device_memory}</div><a href={item.source_url} target="_blank" rel="noreferrer" className="mt-1 inline-flex items-center gap-1 text-xs text-accent">参考资料<ExternalLink aria-hidden="true" className="h-3 w-3" /></a></td>
              <td className="max-w-64 px-3 py-5 align-top">{configuration(item)}</td>
              <td className="px-3 py-5 align-top">{price(item.profiles[0], item.kind)}</td><td className="px-3 py-5 align-top">{select(item)}</td>
            </tr>)}</tbody>
          </table>}
        </div>
        <div className="space-y-3 md:hidden">{items.map((item) => (
          <article key={item.id} className="rounded-xl border border-line bg-inset p-4">
            <div className="flex items-start justify-between gap-2"><div><h3 className="font-semibold text-ink">{item.name}</h3><p className="mt-1 text-xs text-muted">{item.kind} · 推荐 {item.recommended_tier}</p></div><Cpu aria-hidden="true" className="h-5 w-5 shrink-0 text-muted" /></div>
            <p className="mt-3 text-sm text-secondary">参考显存 / HBM：{item.device_memory}</p>
            <div className="mt-3 border-t border-line pt-3">{configuration(item)}</div>
            <div className="mt-4 flex flex-wrap items-end justify-between gap-3">{price(item.profiles[0], item.kind)}{select(item)}</div>
            <a href={item.source_url} target="_blank" rel="noreferrer" className="mt-3 inline-flex min-h-9 items-center gap-1 text-xs text-accent">参考资料<ExternalLink aria-hidden="true" className="h-3 w-3" /></a>
          </article>
        ))}</div>
        {data && <div className="mt-5 space-y-2 border-t border-line pt-4 text-xs text-muted"><p>{data.pricing_note}</p><p>{data.configuration_note}</p><p>多实例账号只展示合计消耗，不据此计算单台机器价格。这里的价格用于选型参考。</p></div>}
      </SectionCard>
    </div>
  )
}

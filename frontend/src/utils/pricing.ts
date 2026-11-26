export const CU_COST_STORAGE_KEY = 'colab_pool_cu_cost'

export interface CuCost { amount: string; currency: string }

export function readCuCost(): CuCost {
  try {
    const parsed = JSON.parse(localStorage.getItem(CU_COST_STORAGE_KEY) ?? '{}')
    return { amount: typeof parsed.amount === 'string' ? parsed.amount : '', currency: ['CNY', 'USD'].includes(parsed.currency) ? parsed.currency : 'CNY' }
  } catch { return { amount: '', currency: 'CNY' } }
}

export function validCuCost(cost: CuCost): boolean {
  const value = cost.amount.trim() ? Number(cost.amount) : NaN
  return Number.isFinite(value) && value >= 0 && value <= 1e9
}

export function hourlyCost(rate: number | null | undefined, cost: CuCost): string | null {
  if (rate == null || !Number.isFinite(rate) || rate < 0 || !validCuCost(cost)) return null
  return new Intl.NumberFormat('zh-CN', { style: 'currency', currency: cost.currency }).format(rate * Number(cost.amount))
}

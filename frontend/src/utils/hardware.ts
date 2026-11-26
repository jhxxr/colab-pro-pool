import type { RuntimeHardware } from '../types'

export function hardwareSummary(hardware?: RuntimeHardware): string {
  if (!hardware) return ''
  return [
    hardware.cpu_count ? `${hardware.cpu_count} vCPU` : '',
    hardware.memory_total_bytes ? `${(hardware.memory_total_bytes / 1024 ** 3).toFixed(1)} GiB 内存` : '',
    hardware.disk_total_bytes ? `${(hardware.disk_total_bytes / 1024 ** 3).toFixed(0)} GiB 磁盘` : '',
  ].filter(Boolean).join(' · ')
}

export function detectedGpu(hardware?: RuntimeHardware): string {
  const devices = hardware?.gpu_devices ?? []
  if (!devices.length) return ''
  const first = devices[0]
  const memory = first.memory_total_mib ? ` · ${(first.memory_total_mib / 1024).toFixed(1)} GiB 显存` : ''
  return `${first.name}${devices.length > 1 ? ` × ${devices.length}` : ''}${memory}`
}

export type AccountTier = 'free' | 'pro' | 'pro+'
export type AccountStatus = 'idle' | 'busy' | 'cooldown' | 'maintenance'
export type JobStatus =
  | 'queued'
  | 'provisioning'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

export interface AccountUsage {
  remaining_cu: number | null
  consumption_rate_hourly: number | null
  active_assignments: number | null
  updated_at: number | null
  checked_at: number | null
  error: string
  refreshing: boolean
  stale: boolean
}

export interface RuntimeHardware {
  cpu_count?: number
  memory_total_bytes?: number
  disk_total_bytes?: number
  detected_at?: number
  gpu_devices?: { name: string; memory_total_mib?: number }[]
}

export interface AccountRuntime {
  job_id: string
  job_name: string
  status: string
  gpu: string
  tpu: string
  high_mem: boolean
  gpu_detected: string
  hardware: RuntimeHardware
}

export interface Account {
  name: string
  home_dir: string
  tier: AccountTier | string
  status: AccountStatus | string
  current_job_id: string | null
  cooldown_until: number
  cooldown_remaining_seconds: number
  last_used_at: number
  total_runs: number
  total_failures: number
  last_error: string
  enabled: number | boolean
  proxy_id: string | null
  proxy_name: string | null
  auth_configured: boolean
  usage: AccountUsage
  runtime: AccountRuntime | null
}

export interface ProxyConfig {
  proxy_id: string
  name: string
  endpoint: string
  has_auth: boolean
  assigned_accounts: number
  created_at: number
  updated_at: number
}

export interface DriveProfile {
  profile: string
  remote_name: string
  path: string
}

export interface OutputFileDetail {
  path: string
  size: number
}

export interface Job {
  job_id: string
  name: string
  status: JobStatus | string
  gpu: string
  tpu: string
  high_mem: boolean
  priority: number
  gpu_detected: string
  runtime_info: RuntimeHardware
  entrypoint: string
  script_args: string[]
  pip_packages: string[]
  dataset_commands: string[]
  env_vars: Record<string, string>
  drive_profile: string
  drive_subpath: string
  drive_sync_outputs_to: string
  timeout_seconds: number
  assigned_account: string
  session_name: string
  attempted_accounts: string[]
  workspace_dir: string
  log_path: string
  artifacts_archive: string
  exit_code: number | null
  error_message: string
  created_at: number
  started_at: number | null
  finished_at: number | null
  output_files?: string[]
  output_file_details?: OutputFileDetail[]
}

export interface ResourceProfile {
  job_id: string
  resource_id: string
  high_mem: boolean
  actual_model: string
  runtime_info: RuntimeHardware
  consumption_rate_hourly: number
  observed_at: number
}

export interface ComputeResource {
  id: string
  kind: 'GPU' | 'CPU' | 'TPU'
  name: string
  device_memory: string
  recommended_tier: string
  description: string
  source_url: string
  gpu: string
  tpu: string
  profiles: ResourceProfile[]
}

export interface ResourcesResponse {
  resources: ComputeResource[]
  pricing_note: string
  configuration_note: string
}

export interface PoolStats {
  accounts: {
    total: number
    idle: number
    busy: number
    cooldown: number
    disabled: number
  }
  jobs: {
    queued: number
    provisioning: number
    running: number
    succeeded: number
    failed: number
    cancelled: number
    total: number
  }
  gpu_counts: Record<string, number>
  success_rate: number | null
  avg_duration_seconds: number | null
  jobs_disk_bytes: number
  drive_profiles_count: number
}

export interface HealthStatus {
  status: string
  version: string
  accounts_total: number
  accounts_idle: number
  accounts_busy: number
  accounts_cooldown: number
  jobs_queued: number
  jobs_running: number
  jobs_provisioning: number
}

export interface JobLogsResponse {
  job_id: string
  status: string
  assigned_account: string
  gpu_detected: string
  total_lines: number
  next_offset: number
  file_size: number
  logs: string
}

export interface WorkspaceInspectResponse {
  files: OutputFileDetail[]
  selected_file: string | null
  content: string | null
  truncated: boolean
}

export interface InlineFileItem {
  path: string
  content: string
  encoding: 'utf-8' | 'base64'
}

export interface JobSubmitPayload {
  name: string
  gpu: string
  tpu: string
  high_mem: boolean
  priority: number
  entrypoint: string
  script_content?: string
  extra_files?: InlineFileItem[]
  script_args: string[]
  pip_packages: string[]
  dataset_commands: string[]
  env_vars: Record<string, string>
  drive_profile: string
  drive_subpath: string
  drive_sync_outputs_to: string
  timeout_seconds: number
}

export interface JobListResponse {
  jobs: Job[]
  total: number
  limit: number
  offset: number
}

export interface AccountsResponse {
  accounts: Account[]
  drive_profiles: DriveProfile[]
}

export interface AccountAuthSession {
  session_id: string
  name: string
  tier: string
  proxy_id: string | null
  status: 'starting' | 'awaiting_code' | 'verifying' | 'succeeded' | 'failed' | 'expired' | 'cancelled'
  authorization_url: string | null
  created_at: number
  expires_at: number
  error: string
  account: Account | null
}

export interface AuthCheckResponse {
  auth_required: boolean
  authenticated: boolean
}

import { useEffect, useState } from 'react'
import {
  ChevronDown,
  ChevronRight,
  Code2,
  Cpu,
  FileUp,
  Plus,
  ArrowUpRight,
  FileCode2,
  Check,
  Trash2,
  Upload,
} from 'lucide-react'
import { fetchAccounts, fetchResources, submitJobJson, submitJobMultipart } from '../api/client'
import { SectionCard } from '../components/StatusBadge'
import { useToast } from '../components/Toast'
import type { ComputeResource, DriveProfile, InlineFileItem, JobSubmitPayload } from '../types'
import { formatBytes } from '../utils/format'
import { hourlyCost, readCuCost } from '../utils/pricing'

interface SubmitPageProps {
  initialPreset?: Partial<JobSubmitPayload> | null
  onClearPreset?: () => void
  onSubmitted: (jobId: string) => void
}

const GPU_CARDS = [
  { id: 'T4', label: 'T4', vram: '16 GB', tier: 'Pro', desc: '轻量推理 / 调试' },
  { id: 'L4', label: 'L4', vram: '24 GB', tier: 'Pro', desc: '均衡性价比训练' },
  { id: 'A100', label: 'A100', vram: '40/80 GB', tier: 'Pro', desc: '主力大模型 / 多折训练' },
  { id: 'H100', label: 'H100', vram: '80 GB', tier: 'Pro+', desc: '仅调度 Pro+ 槽位' },
  { id: 'G4', label: 'G4', vram: '运行后实测', tier: 'Pro+', desc: '大显存任务 · Pro+ 槽位' },
  { id: 'CPU', label: 'CPU', vram: '无 GPU', tier: 'Free/Pro', desc: '纯数据预处理' },
]

const TEMPLATES: { label: string; entrypoint: string; code: string; pip: string[] }[] = [
  {
    label: 'GPU 验卡与产物测试',
    entrypoint: 'probe.py',
    pip: [],
    code: `import json
import os
import platform
import subprocess
from pathlib import Path

print("Python:", platform.python_version())
res = subprocess.run(["nvidia-smi"], capture_output=True, text=True)
print(res.stdout or res.stderr)

out_dir = Path(os.environ.get("COLAB_OUTPUT_DIR", "/content/outputs"))
out_dir.mkdir(parents=True, exist_ok=True)
report = {"status": "ok", "python": platform.python_version()}
(out_dir / "probe_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print("Saved probe_report.json to", out_dir)
`,
  },
  {
    label: 'PyTorch 训练模板',
    entrypoint: 'train.py',
    pip: ['einops'],
    code: `import json
import os
from pathlib import Path
import torch

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print("Using device:", device)
if device.type == "cuda":
    print("GPU:", torch.cuda.get_device_name(0))

x = torch.randn(1024, 1024, device=device)
y = x @ x.T
loss = float(y.mean().item())
print(f"Dummy forward done, metric={loss:.4f}")

out_dir = Path(os.environ.get("COLAB_OUTPUT_DIR", "/content/outputs"))
out_dir.mkdir(parents=True, exist_ok=True)
(out_dir / "metrics.json").write_text(json.dumps({"loss": loss}, indent=2), encoding="utf-8")
`,
  },
]

export function SubmitPage({
  initialPreset,
  onClearPreset,
  onSubmitted,
}: SubmitPageProps) {
  const { pushToast } = useToast()

  const [submitMode, setSubmitMode] = useState<'inline' | 'upload'>('inline')
  const [name, setName] = useState('colab-exp')
  const [gpu, setGpu] = useState('T4')
  const [tpu, setTpu] = useState('')
  const [highMem, setHighMem] = useState(false)
  const [priority, setPriority] = useState(0)
  const [timeoutSeconds, setTimeoutSeconds] = useState(43200)

  const [entrypoint, setEntrypoint] = useState('train.py')
  const [scriptContent, setScriptContent] = useState(TEMPLATES[1].code)
  const [extraFiles, setExtraFiles] = useState<InlineFileItem[]>([])
  const [uploadFiles, setUploadFiles] = useState<File[]>([])
  const [isDragging, setIsDragging] = useState(false)

  const [pipInput, setPipInput] = useState('')
  const [pipPackages, setPipPackages] = useState<string[]>([])
  const [scriptArgsText, setScriptArgsText] = useState('')
  const [datasetCommandsText, setDatasetCommandsText] = useState('')
  const [envRows, setEnvRows] = useState<{ key: string; value: string }[]>([])

  const [driveProfiles, setDriveProfiles] = useState<DriveProfile[]>([])
  const [resources, setResources] = useState<ComputeResource[]>([])
  const [cost] = useState(readCuCost)
  const [driveExpanded, setDriveExpanded] = useState(false)
  const [driveProfile, setDriveProfile] = useState('')
  const [driveSubpath, setDriveSubpath] = useState('')
  const [driveSyncOutputsTo, setDriveSyncOutputsTo] = useState('')

  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    fetchAccounts()
      .then((res) => setDriveProfiles(res.drive_profiles))
      .catch(() => {})
    fetchResources().then((res) => setResources(res.resources)).catch(() => {})
  }, [])

  useEffect(() => {
    if (!initialPreset) return
    setSubmitMode('inline')
    if (initialPreset.name) setName(initialPreset.name)
    if (initialPreset.gpu) setGpu(initialPreset.gpu.toUpperCase())
    if (initialPreset.tpu !== undefined) setTpu(initialPreset.tpu)
    if (initialPreset.high_mem !== undefined) setHighMem(initialPreset.high_mem)
    if (initialPreset.priority !== undefined) setPriority(initialPreset.priority)
    if (initialPreset.timeout_seconds !== undefined) setTimeoutSeconds(initialPreset.timeout_seconds)
    if (initialPreset.entrypoint) setEntrypoint(initialPreset.entrypoint)
    if (initialPreset.script_content !== undefined) setScriptContent(initialPreset.script_content)
    if (initialPreset.pip_packages) setPipPackages(initialPreset.pip_packages)
    if (initialPreset.script_args) setScriptArgsText(initialPreset.script_args.join(' '))
    if (initialPreset.dataset_commands) setDatasetCommandsText(initialPreset.dataset_commands.join('\n'))
    if (initialPreset.env_vars) {
      setEnvRows(Object.entries(initialPreset.env_vars).map(([key, value]) => ({ key, value })))
    }
    if (initialPreset.drive_profile) {
      setDriveExpanded(true)
      setDriveProfile(initialPreset.drive_profile)
      setDriveSubpath(initialPreset.drive_subpath ?? '')
      setDriveSyncOutputsTo(initialPreset.drive_sync_outputs_to ?? '')
    }
    onClearPreset?.()
  }, [initialPreset, onClearPreset])

  const addPipTags = () => {
    const tokens = pipInput
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean)
    if (!tokens.length) return
    setPipPackages((prev) => Array.from(new Set([...prev, ...tokens])))
    setPipInput('')
  }

  const handleFilesSelected = (list: FileList | null) => {
    if (!list) return
    const next = Array.from(list)
    setUploadFiles((prev) => [...prev, ...next])
    if (next.length === 1 && next[0].name.endsWith('.py')) {
      setEntrypoint(next[0].name)
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!entrypoint.trim()) {
      pushToast('error', '请填写入口脚本文件名（如 train.py）')
      return
    }

    const envVars: Record<string, string> = {}
    for (const row of envRows) {
      const k = row.key.trim()
      if (k) envVars[k] = row.value
    }

    const scriptArgs = scriptArgsText
      .split(/\s+/)
      .map((s) => s.trim())
      .filter(Boolean)

    const datasetCommands = datasetCommandsText
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean)

    const baseSpec = {
      name: name.trim() || 'colab-exp',
      gpu: gpu.toUpperCase(),
      tpu: tpu.trim(),
      high_mem: highMem,
      priority,
      entrypoint: entrypoint.trim(),
      script_args: scriptArgs,
      pip_packages: pipPackages,
      dataset_commands: datasetCommands,
      env_vars: envVars,
      drive_profile: driveProfile.trim(),
      drive_subpath: driveSubpath.trim(),
      drive_sync_outputs_to: driveSyncOutputsTo.trim(),
      timeout_seconds: timeoutSeconds,
    }

    setSubmitting(true)
    try {
      let created
      if (submitMode === 'inline') {
        created = await submitJobJson({
          ...baseSpec,
          script_content: scriptContent,
          extra_files: extraFiles.filter((f) => f.path.trim()),
        })
      } else {
        if (uploadFiles.length === 0) {
          pushToast('error', '请先选择或拖拽要上传的文件 / .tar.gz 压缩包')
          setSubmitting(false)
          return
        }
        created = await submitJobMultipart(uploadFiles, baseSpec)
      }
      pushToast('success', `实验已进入调度队列 (${created.job_id})`)
      onSubmitted(created.job_id)
    } catch (err) {
      pushToast('error', err instanceof Error ? err.message : '提交任务失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="page-title">新建实验</h1>
          <p className="page-description">
            选择运行环境、添加代码，其余交给账号池。
          </p>
        </div>
        <button
          type="submit"
          disabled={submitting}
          className="button-primary px-5 disabled:cursor-not-allowed disabled:opacity-60"
        >
          <ArrowUpRight aria-hidden="true" className="h-4 w-4" />
          {submitting ? '正在提交...' : '提交并实时跟踪'}
        </button>
      </div>

      {/* Hardware Selection */}
      <SectionCard title="1. 选择算力规格与调度参数">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          {GPU_CARDS.map((card) => {
            const selected = !tpu.trim() && gpu === card.id
            const profile = resources.find((item) => item.id === card.id)?.profiles.find((item) => item.high_mem === highMem)
            const moneyRate = hourlyCost(profile?.consumption_rate_hourly, cost)
            return (
              <button
                key={card.id}
                type="button"
                aria-pressed={selected}
                onClick={() => {
                  setTpu('')
                  setGpu(card.id)
                }}
                className={`flex flex-col justify-between rounded-xl border p-3.5 text-left transition ${
                  selected
                    ? 'border-accent bg-accent-soft ring-1 ring-accent-ring'
                    : 'border-line bg-inset hover:border-control'
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="inline-flex items-center gap-1.5 text-sm font-semibold text-ink">
                    {card.label}
                    {selected && <Check aria-hidden="true" className="h-3.5 w-3.5 text-accent" />}
                  </span>
                  <span
                    className={`rounded px-1.5 py-0.5 text-[11px] font-semibold ${
                      card.tier === 'Pro+'
                        ? 'bg-subtle text-secondary'
                        : 'bg-subtle text-secondary'
                    }`}
                  >
                    {card.tier}
                  </span>
                </div>
                <div className="mt-2 text-xs text-secondary">{card.vram}</div>
                <div className="mt-1 text-xs text-muted">{card.desc}</div>
                <div className="mt-3 border-t border-line pt-2 text-xs text-secondary tabular-nums">
                  {profile ? `实测 ${profile.consumption_rate_hourly.toFixed(2)} CU/h` : '小时费率待实测'}
                </div>
                {moneyRate && <div className="mt-1 text-xs text-muted">约 {moneyRate} / 小时</div>}
              </button>
            )
          })}
        </div>

        <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-5">
          <div>
            <label htmlFor="experiment-name" className="mb-1 block text-xs text-muted">实验名称</label>
            <input
              id="experiment-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full rounded-xl border border-control bg-inset px-3 py-2 text-xs text-ink outline-none focus:border-accent"
            />
          </div>
          <div>
            <label htmlFor="experiment-entrypoint" className="mb-1 block text-xs text-muted">入口文件 (Entrypoint)</label>
            <input
              id="experiment-entrypoint"
              type="text"
              value={entrypoint}
              onChange={(e) => setEntrypoint(e.target.value)}
              placeholder="train.py 或 exp.ipynb"
              className="w-full rounded-xl border border-control bg-inset px-3 py-2 font-mono text-xs text-ink outline-none focus:border-accent"
            />
          </div>
          <div>
            <label htmlFor="experiment-tpu" className="mb-1 block text-xs text-muted">可选 TPU（填则覆盖 GPU）</label>
            <input
              id="experiment-tpu"
              type="text"
              value={tpu}
              onChange={(e) => setTpu(e.target.value)}
              placeholder="如 v5e1, v6e1"
              className="w-full rounded-xl border border-control bg-inset px-3 py-2 font-mono text-xs text-ink outline-none focus:border-accent"
            />
          </div>
          <div>
            <label htmlFor="experiment-priority" className="mb-1 block text-xs text-muted">调度优先级 (-10 ~ 100)</label>
            <input
              id="experiment-priority"
              type="number"
              min={-10}
              max={100}
              value={priority}
              onChange={(e) => setPriority(Number(e.target.value))}
              className="w-full rounded-xl border border-control bg-inset px-3 py-2 font-mono text-xs text-ink outline-none focus:border-accent"
            />
          </div>
          <div className="flex flex-col justify-between">
            <label htmlFor="experiment-timeout" className="mb-1 block text-xs text-muted">超时（秒）与高内存</label>
            <div className="flex items-center gap-2">
              <input
                id="experiment-timeout"
                type="number"
                min={60}
                max={86400}
                value={timeoutSeconds}
                onChange={(e) => setTimeoutSeconds(Number(e.target.value))}
                className="w-full rounded-xl border border-control bg-inset px-3 py-2 font-mono text-xs text-ink outline-none focus:border-accent"
              />
              <label className="inline-flex shrink-0 cursor-pointer items-center gap-1.5 rounded-xl border border-control bg-inset px-3 py-2 text-xs text-ink">
                <input
                  type="checkbox"
                  checked={highMem}
                  onChange={(e) => setHighMem(e.target.checked)}
                  className="accent-accent"
                />
                高内存
              </label>
            </div>
          </div>
        </div>
      </SectionCard>

      {/* Code or File Upload */}
      <SectionCard
        title="2. 代码与工作区内容"
        action={
          <div className="inline-flex rounded-xl border border-line bg-inset p-1 text-xs">
            <button
              type="button"
              aria-pressed={submitMode === 'inline'}
              onClick={() => setSubmitMode('inline')}
              className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 font-medium transition ${
                submitMode === 'inline'
                  ? 'bg-accent-soft text-accent'
                  : 'text-muted hover:text-ink'
              }`}
            >
              <Code2 className="h-3.5 w-3.5" />
              在线编写代码
            </button>
            <button
              type="button"
              aria-pressed={submitMode === 'upload'}
              onClick={() => setSubmitMode('upload')}
              className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 font-medium transition ${
                submitMode === 'upload'
                  ? 'bg-accent-soft text-accent'
                  : 'text-muted hover:text-ink'
              }`}
            >
              <FileUp className="h-3.5 w-3.5" />
              上传文件 / .tar.gz
            </button>
          </div>
        }
      >
        {submitMode === 'inline' ? (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="inline-flex items-center gap-1 text-muted">
                  <FileCode2 aria-hidden="true" className="h-3.5 w-3.5 text-muted" />
                  快速模板:
                </span>
                {TEMPLATES.map((tpl) => (
                  <button
                    key={tpl.label}
                    type="button"
                    onClick={() => {
                      setEntrypoint(tpl.entrypoint)
                      setScriptContent(tpl.code)
                      setPipPackages(tpl.pip)
                    }}
                    className="rounded-lg border border-control bg-surface px-2.5 py-1 text-xs text-ink hover:border-accent-line hover:text-accent"
                  >
                    {tpl.label}
                  </button>
                ))}
              </div>
              <span className="text-xs text-muted">
                输出请保存到 <code className="text-secondary">os.environ[&quot;COLAB_OUTPUT_DIR&quot;]</code>
              </span>
            </div>

            <textarea
              aria-label="入口脚本代码"
              rows={14}
              value={scriptContent}
              onChange={(e) => setScriptContent(e.target.value)}
              spellCheck={false}
              className="w-full rounded-xl border border-line bg-editor p-4 font-mono text-xs leading-relaxed text-ink outline-none focus:border-accent"
            />

            {/* Extra inline files */}
            <div>
              <div className="mb-2 flex items-center justify-between">
                <span className="text-xs font-medium text-secondary">
                  附加工作区文件 ({extraFiles.length})
                </span>
                <button
                  type="button"
                  onClick={() =>
                    setExtraFiles((prev) => [
                      ...prev,
                      { path: `utils/helper_${prev.length + 1}.py`, content: '', encoding: 'utf-8' },
                    ])
                  }
                  className="inline-flex items-center gap-1 rounded-lg border border-control bg-surface px-2.5 py-1 text-xs text-ink hover:bg-hover-surface"
                >
                  <Plus className="h-3.5 w-3.5" />
                  添加辅助文件
                </button>
              </div>
              {extraFiles.map((item, idx) => (
                <div
                  key={idx}
                  className="mb-3 rounded-xl border border-line bg-inset p-3"
                >
                  <div className="mb-2 flex items-center gap-2">
                    <input
                      aria-label={`附加文件 ${idx + 1} 的路径`}
                      type="text"
                      value={item.path}
                      onChange={(e) =>
                        setExtraFiles((prev) =>
                          prev.map((x, i) => (i === idx ? { ...x, path: e.target.value } : x))
                        )
                      }
                      placeholder="相对路径，如 models/net.py"
                      className="flex-1 rounded-lg border border-control bg-inset px-2.5 py-1.5 font-mono text-xs text-ink outline-none focus:border-accent"
                    />
                    <button
                      type="button"
                      aria-label={`移除附加文件 ${idx + 1}`}
                      onClick={() => setExtraFiles((prev) => prev.filter((_, i) => i !== idx))}
                      className="rounded-lg border border-control p-1.5 text-muted hover:text-danger"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                  <textarea
                    aria-label={`${item.path || `附加文件 ${idx + 1}`} 的代码`}
                    rows={4}
                    value={item.content}
                    onChange={(e) =>
                      setExtraFiles((prev) =>
                        prev.map((x, i) => (i === idx ? { ...x, content: e.target.value } : x))
                      )
                    }
                    className="w-full rounded-lg border border-line bg-editor p-2.5 font-mono text-xs text-ink outline-none focus:border-accent"
                  />
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            <div
              onDragOver={(e) => {
                e.preventDefault()
                setIsDragging(true)
              }}
              onDragLeave={() => setIsDragging(false)}
              onDrop={(e) => {
                e.preventDefault()
                setIsDragging(false)
                handleFilesSelected(e.dataTransfer.files)
              }}
              className={`flex flex-col items-center justify-center rounded-2xl border-2 border-dashed p-10 text-center transition ${
                isDragging
                  ? 'border-accent bg-accent-soft'
                  : 'border-control bg-inset hover:border-control'
              }`}
            >
              <Upload className="mb-3 h-8 w-8 text-accent" />
              <p className="text-sm font-medium text-ink">
                拖拽 <code className="text-secondary">.py</code>、
                <code className="text-secondary">.ipynb</code> 或项目{' '}
                <code className="text-secondary">.tar.gz</code> 到此处
              </p>
              <p className="mt-1 text-xs text-muted">
                上传的压缩包会自动解压到工作区；请确保入口文件与上方 Entrypoint 一致
              </p>
              <label className="mt-4 cursor-pointer rounded-xl border border-control bg-surface px-4 py-2 text-xs font-medium text-ink hover:bg-hover-surface">
                选择本地文件
                <input
                  type="file"
                  multiple
                  className="hidden"
                  onChange={(e) => handleFilesSelected(e.target.files)}
                />
              </label>
            </div>

            {uploadFiles.length > 0 && (
              <div className="space-y-1.5">
                {uploadFiles.map((f, idx) => (
                  <div
                    key={`${f.name}-${idx}`}
                    className="flex items-center justify-between rounded-xl border border-line bg-inset px-3.5 py-2 text-xs"
                  >
                    <div className="flex items-center gap-2">
                      <Cpu className="h-3.5 w-3.5 text-accent" />
                      <span className="font-mono text-ink">{f.name}</span>
                      <span className="text-muted">({formatBytes(f.size)})</span>
                    </div>
                    <button
                      type="button"
                      onClick={() =>
                        setUploadFiles((prev) => prev.filter((_, i) => i !== idx))
                      }
                      aria-label={`移除上传文件 ${f.name}`}
                      className="text-muted hover:text-danger"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </SectionCard>

      {/* Dependencies & Env */}
      <SectionCard title="3. 运行依赖、数据集拉取与环境变量">
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
          <div className="space-y-4">
            <div>
              <label htmlFor="experiment-pip" className="mb-1.5 block text-xs font-medium text-secondary">
                Pip 安装依赖（如 einops timm）
              </label>
              <div className="flex gap-2">
                <input
                  id="experiment-pip"
                  type="text"
                  value={pipInput}
                  onChange={(e) => setPipInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault()
                      addPipTags()
                    }
                  }}
                  placeholder="输入包名按回车添加..."
                  className="flex-1 rounded-xl border border-control bg-inset px-3 py-2 font-mono text-xs text-ink outline-none focus:border-accent"
                />
                <button
                  type="button"
                  onClick={addPipTags}
                  className="rounded-xl border border-control bg-surface px-3 py-2 text-xs text-ink hover:bg-hover-surface"
                >
                  添加
                </button>
              </div>
              {pipPackages.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {pipPackages.map((pkg) => (
                    <span
                      key={pkg}
                      className="inline-flex items-center gap-1 rounded-lg border border-accent-line bg-accent-soft px-2.5 py-1 font-mono text-xs text-accent"
                    >
                      {pkg}
                      <button
                        type="button"
                        aria-label={`移除依赖 ${pkg}`}
                        onClick={() => setPipPackages((prev) => prev.filter((p) => p !== pkg))}
                        className="text-accent hover:text-danger"
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              )}
            </div>

            <div>
              <label htmlFor="experiment-args" className="mb-1.5 block text-xs font-medium text-secondary">
                启动命令行参数 (script_args，空格分隔)
              </label>
              <input
                id="experiment-args"
                type="text"
                value={scriptArgsText}
                onChange={(e) => setScriptArgsText(e.target.value)}
                placeholder="--epochs 20 --batch-size 64"
                className="w-full rounded-xl border border-control bg-inset px-3 py-2 font-mono text-xs text-ink outline-none focus:border-accent"
              />
            </div>

            <div>
              <div className="mb-1.5 flex items-center justify-between">
                <label className="text-xs font-medium text-secondary">环境变量 (env_vars)</label>
                <button
                  type="button"
                  onClick={() => setEnvRows((prev) => [...prev, { key: '', value: '' }])}
                  className="text-xs text-accent hover:text-accent"
                >
                  + 添加变量
                </button>
              </div>
              <div className="space-y-2">
                {envRows.map((row, idx) => (
                  <div key={idx} className="flex items-center gap-2">
                    <input
                      aria-label={`环境变量 ${idx + 1} 的名称`}
                      type="text"
                      value={row.key}
                      onChange={(e) =>
                        setEnvRows((prev) =>
                          prev.map((r, i) => (i === idx ? { ...r, key: e.target.value } : r))
                        )
                      }
                      placeholder="KEY"
                      className="w-2/5 rounded-lg border border-control bg-inset px-2.5 py-1.5 font-mono text-xs text-ink outline-none focus:border-accent"
                    />
                    <input
                      aria-label={`环境变量 ${idx + 1} 的值`}
                      type="text"
                      value={row.value}
                      onChange={(e) =>
                        setEnvRows((prev) =>
                          prev.map((r, i) => (i === idx ? { ...r, value: e.target.value } : r))
                        )
                      }
                      placeholder="VALUE"
                      className="flex-1 rounded-lg border border-control bg-inset px-2.5 py-1.5 font-mono text-xs text-ink outline-none focus:border-accent"
                    />
                    <button
                      type="button"
                      aria-label={`移除环境变量 ${idx + 1}`}
                      onClick={() => setEnvRows((prev) => prev.filter((_, i) => i !== idx))}
                      className="p-1.5 text-muted hover:text-danger"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          </div>

          <div>
            <label htmlFor="experiment-dataset" className="mb-1.5 block text-xs font-medium text-secondary">
              机房直连数据集命令 (dataset_commands，每行一条 Shell 命令)
            </label>
            <textarea
              id="experiment-dataset"
              rows={7}
              value={datasetCommandsText}
              onChange={(e) => setDatasetCommandsText(e.target.value)}
              placeholder={`# 示例：在 Colab 高速内网直接拉取公开数据或 HF 权重\n# mkdir -p /content/data && curl -fsSL https://.../data.tar.gz | tar -xz -C /content/data`}
              spellCheck={false}
              className="w-full rounded-xl border border-line bg-editor p-3 font-mono text-xs text-ink placeholder-placeholder outline-none focus:border-accent"
            />

            {/* Collapsible optional existing drive_profile config */}
            <div className="mt-4 rounded-xl border border-line bg-inset">
              <button
                type="button"
                aria-expanded={driveExpanded}
                onClick={() => setDriveExpanded((v) => !v)}
                className="flex w-full items-center justify-between px-3.5 py-2.5 text-xs font-medium text-secondary hover:text-ink"
              >
                <span>可选：挂载已有服务端 rclone Drive 档案</span>
                {driveExpanded ? (
                  <ChevronDown className="h-4 w-4 text-muted" />
                ) : (
                  <ChevronRight className="h-4 w-4 text-muted" />
                )}
              </button>
              {driveExpanded && (
                <div className="space-y-3 border-t border-line p-3.5 text-xs">
                  <div>
                    <label htmlFor="experiment-drive" className="mb-1 block text-muted">Drive 档案名</label>
                    <select
                      id="experiment-drive"
                      value={driveProfile}
                      onChange={(e) => setDriveProfile(e.target.value)}
                      className="w-full rounded-lg border border-control bg-inset px-2.5 py-1.5 font-mono text-ink outline-none focus:border-accent"
                    >
                      <option value="">不挂载 Google Drive</option>
                      {driveProfiles.map((dp) => (
                        <option key={dp.profile} value={dp.profile}>
                          {dp.profile} ({dp.remote_name})
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    <div>
                      <label htmlFor="experiment-drive-path" className="mb-1 block text-muted">子目录 (drive_subpath)</label>
                      <input
                        id="experiment-drive-path"
                        type="text"
                        value={driveSubpath}
                        onChange={(e) => setDriveSubpath(e.target.value)}
                        placeholder="如 Datasets/EEG"
                        className="w-full rounded-lg border border-control bg-inset px-2.5 py-1.5 font-mono text-ink outline-none focus:border-accent"
                      />
                    </div>
                    <div>
                      <label htmlFor="experiment-drive-output" className="mb-1 block text-muted">回传目录 (sync_outputs_to)</label>
                      <input
                        id="experiment-drive-output"
                        type="text"
                        value={driveSyncOutputsTo}
                        onChange={(e) => setDriveSyncOutputsTo(e.target.value)}
                        placeholder="如 Results/exp01"
                        className="w-full rounded-lg border border-control bg-inset px-2.5 py-1.5 font-mono text-ink outline-none focus:border-accent"
                      />
                    </div>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      </SectionCard>
    </form>
  )
}

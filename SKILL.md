---
name: colab-pro-pool
description: Dispatch Python training scripts, Jupyter notebooks, and ML experiments to a remote multi-account Google Colab Pro/Pro+ GPU/TPU pool server. Supports non-interactive mounting of the user's own Google Drive via rclone and direct public dataset downloads inside Colab.
---

# Colab Pro Pool — AI Agent Operator Skill

Use this skill whenever you need to run GPU/TPU training scripts, hyperparameter sweeps, or notebooks on the remote **Colab Pro Pool** server (`COLAB_POOL_SERVER_URL`).

## 1. Environment Configuration

Ensure the following environment variables are set (ask the user only if missing):
- `COLAB_POOL_SERVER_URL`: Base URL of the pool server (e.g. `http://10.0.0.5:8080`)
- `COLAB_POOL_API_KEY`: Bearer token matching the server's `COLAB_POOL_API_KEY`

## 2. Calling via MCP Tools (Preferred if `colab-pro-pool` MCP is loaded)

1. **Check available Pro accounts & Drive profiles**:
   Call `colab_pool_status` to inspect idle/busy/cooldown accounts (`T4`, `L4`, `A100` on `pro`; `H100`, `G4` on `pro+`) and pre-authorized `drive_profiles`.
2. **Submit an experiment**:
   Call `submit_colab_job` with:
   - `entrypoint`: e.g. `"train.py"` or `"exp.ipynb"`
   - `local_workspace_dir`: path to local project directory (auto-packed excluding `.git`/`venv`), OR `script_content` for a standalone script
   - `gpu`: `"T4"` | `"L4"` | `"A100"` | `"H100"` | `"CPU"`
   - `pip_packages`: e.g. `["einops", "timm", "wandb"]`
   - `dataset_commands`: shell commands executed inside Colab's gigabit network before training (e.g. `["mkdir -p /content/data && curl -L https://.../data.tar.gz | tar -xz -C /content/data"]`)
   - `drive_profile`: set to the user's rclone profile name (e.g. `"default"`) if the job needs `/content/drive/MyDrive` mounted non-interactively
   - `drive_sync_outputs_to`: optional Drive path (e.g. `"Experiments/run_01"`) to sync `/content/outputs` upon exit
3. **Monitor & collect artifacts**:
   - Poll `get_colab_job(job_id=..., tail_lines=120)` every 15–30 seconds until `status` is `"succeeded"` or `"failed"`.
   - Call `download_colab_artifacts(job_id=..., dest_dir="./outputs")` to fetch everything the script wrote into `/content/outputs` (`$COLAB_OUTPUT_DIR`).

## 3. Calling via `cpool` CLI (Fallback when MCP is not attached, or for shell automation)

The unified `cpool` (alias `colab-pool`) CLI supports persistent profiles (`cpool login`) and `--json` output on every command for reliable machine parsing.

```bash
# 1. Inspect pool & accounts as JSON:
cpool status --json

# 2. Submit a local directory (honors .cpoolignore), stream SSE logs, and pull outputs:
cpool run ./my_exp_dir \
  -e train.py \
  --gpu A100 \
  --priority 10 \
  --pip einops --pip timm \
  --dataset-cmd "wget -q https://example.com/public_data.zip -O /tmp/d.zip && unzip -q /tmp/d.zip -d /content/data" \
  --wait --out-dir ./results

# 3. Quick inline GPU test / snippet:
cpool exec -c "import torch; print(torch.cuda.get_device_name(0))" --gpu T4

# 4. Follow logs or pull a single file from an existing job:
cpool logs <job_id> -f
cpool pull <job_id> ./results --file metrics.json
cpool jobs --status running --json
```

(Legacy `colab-pool-client` also remains supported for backwards compatibility.)

## 4. Writing Compatible Training Scripts

- Save checkpoints, CSV metrics, and figures to `os.environ.get("COLAB_OUTPUT_DIR", "/content/outputs")` so `colab-pro-pool` archives and pulls them back automatically before tearing down the VM.
- Even if a legacy script calls `from google.colab import drive; drive.mount('/content/drive')`, `colab-pro-pool` automatically shims `drive.mount()` into a non-interactive no-op whenever `drive_profile` is set—never wait for browser OAuth codes.

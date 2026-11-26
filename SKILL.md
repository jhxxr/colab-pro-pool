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

## 2. Calling via MCP Tools

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

If the MCP server is not attached, install this package locally and configure your Agent to launch `colab-pool-mcp` with the environment variables above. There is no standalone client CLI. For local projects, `local_workspace_dir` honors `.cpoolignore` as well as default exclusions for `.git`, virtual environments, and dependency caches.

## 3. Writing Compatible Training Scripts

- Save checkpoints, CSV metrics, and figures to `os.environ.get("COLAB_OUTPUT_DIR", "/content/outputs")` so `colab-pro-pool` archives and pulls them back automatically before tearing down the VM.
- Even if a legacy script calls `from google.colab import drive; drive.mount('/content/drive')`, `colab-pro-pool` automatically shims `drive.mount()` into a non-interactive no-op whenever `drive_profile` is set—never wait for browser OAuth codes.

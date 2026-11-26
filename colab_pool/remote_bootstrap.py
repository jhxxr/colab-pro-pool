"""Remote bootstrap generator for Google Colab VMs.

Handles:
1. Non-interactive personal Google Drive mounting via rclone (FUSE mount at
   /content/drive/MyDrive with automatic fallback to rclone copy sync if /dev/fuse
   is unavailable in the container).
2. Monkey-patching `google.colab.drive.mount` via `sitecustomize.py` so AI-generated
   training scripts that call `drive.mount('/content/drive')` never block on OAuth.
3. Running public dataset download hooks (`wget`, `curl`, `gdown`, `hf`, etc.) inside WORK_DIR.
4. Installing Python dependencies (`uv pip` with `pip` fallback).
5. Running `.py` scripts or `.ipynb` notebooks while streaming logs and emitting
   periodic heartbeats so long silent training steps never drop the CLI WebSocket.
6. Flushing Drive writes, archiving `/content/outputs` into `/tmp/_job_artifacts.tar.gz`,
   and securely wiping temporary rclone credentials.
"""

from __future__ import annotations

import base64
import configparser
import json
from pathlib import Path
from typing import Any


def parse_first_rclone_remote(conf_text: str) -> str:
    """Return the first [remote] section name in an rclone.conf file."""
    parser = configparser.RawConfigParser()
    parser.read_string(conf_text)
    sections = parser.sections()
    if not sections:
        raise ValueError("No [remote] section found in rclone configuration file.")
    return sections[0]


REMOTE_POOL_RUNNER_PY = r'''#!/usr/bin/env python3
"""Executed inside the remote Google Colab VM."""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

SPEC_PATH = Path("/content/_job/job_spec.json")
WORK_DIR = Path("/content/workspace")
OUTPUT_DIR = Path("/content/outputs")
DRIVE_ROOT = Path("/content/drive")
MYDRIVE_DIR = DRIVE_ROOT / "MyDrive"
MY_SPACE_DRIVE_LINK = DRIVE_ROOT / "My Drive"
RCLONE_CONF_PATH = Path("/tmp/.pool_rclone.conf")
SITECUSTOMIZE_DIR = Path("/tmp/_pool_sitecustomize")
ARTIFACTS_TAR = Path("/tmp/_job_artifacts.tar.gz")


def log(msg: str) -> None:
    print(f"[colab-pool-vm] {msg}", flush=True)


def run_cmd(cmd: str | list[str], *, shell: bool = False, env: dict | None = None, check: bool = True) -> int:
    proc = subprocess.run(cmd, shell=shell, env=env)
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed (exit={proc.returncode}): {cmd}")
    return proc.returncode


def _rclone_bin() -> str:
    return shutil.which("rclone") or "rclone"


def ensure_rclone_installed() -> None:
    if shutil.which("rclone"):
        return
    log("Installing rclone inside Colab VM...")
    rc = subprocess.run(
        "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rclone fuse3 || "
        "(curl -fsSL https://rclone.org/install.sh | bash)",
        shell=True,
    ).returncode
    if rc != 0 or not shutil.which("rclone"):
        raise RuntimeError("Failed to install rclone in Colab VM.")


def install_drive_shim() -> None:
    """Monkey-patch google.colab.drive.mount so scripts calling it do not block."""
    SITECUSTOMIZE_DIR.mkdir(parents=True, exist_ok=True)
    shim_code = """
import os
import sys

def _patch_colab_drive():
    try:
        import google.colab.drive as _gcd
    except Exception:
        return
    _orig_mount = getattr(_gcd, "mount", None)
    def _non_interactive_mount(mountpoint="/content/drive", force_remount=False, timeout_ms=120000, readonly=False):
        mydrive = os.path.join(mountpoint, "MyDrive")
        if os.environ.get("COLAB_POOL_DRIVE_READY") == "1" or os.path.isdir(mydrive):
            print(f"[colab-pro-pool] Drive already mounted at {mountpoint} (non-interactive rclone).", flush=True)
            return
        if _orig_mount is not None:
            return _orig_mount(mountpoint, force_remount=force_remount, timeout_ms=timeout_ms, readonly=readonly)
    _gcd.mount = _non_interactive_mount
    _gcd._mount = _non_interactive_mount

_patch_colab_drive()
"""
    (SITECUSTOMIZE_DIR / "sitecustomize.py").write_text(shim_code.strip() + "\n", encoding="utf-8")


def setup_personal_drive(spec: dict) -> tuple[str, str]:
    """Mount or sync user's Google Drive via rclone. Returns (mode, remote_target)."""
    conf_b64 = spec.get("rclone_conf_b64", "")
    remote_name = spec.get("rclone_remote_name", "")
    if not conf_b64 or not remote_name:
        return ("none", "")

    ensure_rclone_installed()
    raw_conf = base64.b64decode(conf_b64)
    RCLONE_CONF_PATH.write_bytes(raw_conf)
    os.chmod(RCLONE_CONF_PATH, 0o600)

    subpath = spec.get("drive_subpath", "").strip("/")
    remote_target = f"{remote_name}:{subpath}" if subpath else f"{remote_name}:"

    MYDRIVE_DIR.mkdir(parents=True, exist_ok=True)
    if not MY_SPACE_DRIVE_LINK.exists():
        try:
            MY_SPACE_DRIVE_LINK.symlink_to(MYDRIVE_DIR)
        except OSError:
            pass

    install_drive_shim()

    if Path("/dev/fuse").exists():
        log(f"Mounting Google Drive ({remote_target}) to {MYDRIVE_DIR} via rclone FUSE...")
        mount_cmd = [
            _rclone_bin(), "mount", remote_target, str(MYDRIVE_DIR),
            "--config", str(RCLONE_CONF_PATH),
            "--daemon",
            "--vfs-cache-mode", "writes",
            "--dir-cache-time", "5m",
            "--transfers", "4",
        ]
        rc = subprocess.run(mount_cmd).returncode
        time.sleep(2.0)
        if rc == 0 and os.path.ismount(str(MYDRIVE_DIR)):
            log("rclone FUSE mount succeeded.")
            return ("fuse", remote_target)
        log("FUSE mount unavailable in this container; falling back to rclone directory sync.")

    if subpath:
        log(f"Pulling {remote_target} into {MYDRIVE_DIR} via rclone copy...")
        run_cmd(
            [
                _rclone_bin(), "copy", remote_target, str(MYDRIVE_DIR),
                "--config", str(RCLONE_CONF_PATH),
                "--transfers", "8",
            ],
            check=True,
        )
    else:
        log("Drive root configured in sync-back mode (writes under /content/drive/MyDrive will sync back on exit).")
    return ("sync", remote_target)


def finalize_personal_drive(mode: str, remote_target: str, spec: dict) -> None:
    try:
        sync_outputs_to = spec.get("drive_sync_outputs_to", "").strip("/")
        remote_name = spec.get("rclone_remote_name", "")
        if remote_name and RCLONE_CONF_PATH.exists() and sync_outputs_to and OUTPUT_DIR.exists():
            dest = f"{remote_name}:{sync_outputs_to}"
            log(f"Syncing {OUTPUT_DIR} -> {dest} via rclone copy...")
            subprocess.run(
                [
                    _rclone_bin(), "copy", str(OUTPUT_DIR), dest,
                    "--config", str(RCLONE_CONF_PATH),
                    "--transfers", "8",
                ],
                check=False,
            )

        if mode == "fuse":
            log("Flushing and unmounting rclone FUSE mount...")
            time.sleep(2.0)
            subprocess.run(["fusermount", "-uz", str(MYDRIVE_DIR)], check=False)
            subprocess.run(["umount", "-l", str(MYDRIVE_DIR)], check=False)
        elif mode == "sync" and remote_target and RCLONE_CONF_PATH.exists():
            if any(MYDRIVE_DIR.iterdir()):
                log(f"Syncing modified files from {MYDRIVE_DIR} -> {remote_target}...")
                subprocess.run(
                    [
                        _rclone_bin(), "copy", str(MYDRIVE_DIR), remote_target,
                        "--config", str(RCLONE_CONF_PATH),
                        "--transfers", "8",
                    ],
                    check=False,
                )
    finally:
        if RCLONE_CONF_PATH.exists():
            try:
                RCLONE_CONF_PATH.write_bytes(b"\x00" * 128)
                RCLONE_CONF_PATH.unlink()
            except OSError:
                pass


def archive_outputs(entrypoint_path: Path) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if entrypoint_path.suffix.lower() == ".ipynb" and entrypoint_path.exists():
        shutil.copy2(entrypoint_path, OUTPUT_DIR / entrypoint_path.name)
    with tarfile.open(ARTIFACTS_TAR, "w:gz") as tar:
        for item in OUTPUT_DIR.iterdir():
            tar.add(item, arcname=item.name)
    log(f"Packaged outputs into {ARTIFACTS_TAR} ({ARTIFACTS_TAR.stat().st_size} bytes)")


def probe_runtime() -> None:
    """Report VM hardware without importing a training framework or allocating an accelerator."""
    hardware = {"gpu_devices": []}
    try:
        hardware["cpu_count"] = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    except Exception:
        pass
    try:
        meminfo = Path("/proc/meminfo").read_text()
        match = re.search(r"^MemTotal:\s+(\d+)\s+kB", meminfo, re.MULTILINE)
        if match:
            memory = int(match.group(1)) * 1024
            for cgroup in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
                try:
                    limit = int(Path(cgroup).read_text().strip())
                    if limit > 0:
                        memory = min(memory, limit)
                except (OSError, ValueError):
                    pass
            hardware["memory_total_bytes"] = memory
    except Exception:
        pass
    try:
        hardware["disk_total_bytes"] = shutil.disk_usage("/content").total
    except Exception:
        pass
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            gpu_probe = subprocess.run(
                [nvidia_smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            if gpu_probe.returncode == 0:
                for line in gpu_probe.stdout.strip().splitlines()[:16]:
                    name, separator, memory = line.rpartition(",")
                    if separator and name.strip():
                        value = re.search(r"\d+(?:\.\d+)?", memory)
                        device = {"name": name.strip()}
                        if value:
                            device["memory_total_mib"] = float(value.group(0))
                        hardware["gpu_devices"].append(device)
                if hardware["gpu_devices"]:
                    first = hardware["gpu_devices"][0]
                    detected = first["name"]
                    if first.get("memory_total_mib"):
                        detected += f", {first['memory_total_mib']:g} MiB"
                    print(f"__COLAB_POOL_GPU__={detected}", flush=True)
        except Exception:
            pass
    print("__COLAB_POOL_RUNTIME__=" + json.dumps(hardware), flush=True)


def main() -> int:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    user_tar = Path("/content/_job/workspace.tar.gz")
    if user_tar.exists():
        with tarfile.open(user_tar, "r:gz") as tar:
            tar.extractall(WORK_DIR)

    drive_mode = "none"
    remote_target = ""
    exit_code = 0
    try:
        probe_runtime()

        os.chdir(WORK_DIR)
        drive_mode, remote_target = setup_personal_drive(spec)

        # Prepare environment variables (available to both dataset hooks and training script)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["COLAB_OUTPUT_DIR"] = str(OUTPUT_DIR)
        env["COLAB_DRIVE_DIR"] = str(MYDRIVE_DIR)
        pp_parts = [str(WORK_DIR)]
        if drive_mode != "none":
            env["COLAB_POOL_DRIVE_READY"] = "1"
            pp_parts.insert(0, str(SITECUSTOMIZE_DIR))
        prev_pp = env.get("PYTHONPATH", "")
        if prev_pp:
            pp_parts.append(prev_pp)
        env["PYTHONPATH"] = os.pathsep.join(pp_parts)

        for k, v in spec.get("env_vars", {}).items():
            env[str(k)] = str(v)

        # Install Python dependencies
        req_file = WORK_DIR / "requirements.txt"
        pip_pkgs = spec.get("pip_packages", [])
        use_uv = shutil.which("uv") is not None
        if req_file.exists():
            log("Installing dependencies from requirements.txt...")
            cmd = ["uv", "pip", "install", "--system", "-r", str(req_file)] if use_uv else [sys.executable, "-m", "pip", "install", "-q", "-r", str(req_file)]
            run_cmd(cmd, check=True)
        if pip_pkgs:
            log(f"Installing pip packages: {pip_pkgs}")
            cmd = (["uv", "pip", "install", "--system"] + pip_pkgs) if use_uv else ([sys.executable, "-m", "pip", "install", "-q"] + pip_pkgs)
            run_cmd(cmd, check=True)

        # Run public dataset download hooks inside WORK_DIR on Colab gigabit network
        for ds_cmd in spec.get("dataset_commands", []):
            log(f"Running dataset hook: {ds_cmd}")
            run_cmd(ds_cmd, shell=True, env=env, check=True)

        entrypoint = WORK_DIR / spec["entrypoint"]
        if not entrypoint.exists():
            raise FileNotFoundError(f"Entrypoint {entrypoint} not found in workspace.")

        script_args = [str(a) for a in spec.get("script_args", [])]
        if entrypoint.suffix.lower() == ".ipynb":
            log(f"Executing notebook: {entrypoint.name}")
            cmd = [
                "jupyter", "nbconvert",
                "--to", "notebook",
                "--execute",
                "--inplace",
                "--ExecutePreprocessor.timeout=-1",
                str(entrypoint),
            ]
        else:
            log(f"Executing training script: {entrypoint.name} {' '.join(script_args)}")
            cmd = [sys.executable, "-u", str(entrypoint)] + script_args

        proc = subprocess.run(cmd, env=env)
        exit_code = proc.returncode
        log(f"Workload finished with exit_code={exit_code}")
    except Exception as exc:
        log(f"ERROR: {exc}")
        exit_code = 1
    finally:
        finalize_personal_drive(drive_mode, remote_target, spec)
        archive_outputs(WORK_DIR / spec["entrypoint"])

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
'''


KERNEL_BOOTSTRAP_TEMPLATE = r'''
import os
import subprocess
import sys
import tarfile
import threading
import time

bundle = "/content/_job_bundle.tar.gz"
os.makedirs("/content/_job", exist_ok=True)
with tarfile.open(bundle, "r:gz") as tf:
    tf.extractall("/content/_job")

proc = subprocess.Popen(
    [sys.executable, "-u", "/content/_job/_pool_runner.py"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1,
)

last_output = [time.monotonic()]
stop_hb = threading.Event()

def _heartbeat():
    while not stop_hb.wait(15.0):
        if time.monotonic() - last_output[0] >= 45.0:
            print(f"[colab-pool-heartbeat] {time.strftime('%Y-%m-%d %H:%M:%S')} training active...", flush=True)
            last_output[0] = time.monotonic()

hb_thread = threading.Thread(target=_heartbeat, daemon=True)
hb_thread.start()

assert proc.stdout is not None
for line in proc.stdout:
    last_output[0] = time.monotonic()
    sys.stdout.write(line)
    sys.stdout.flush()

rc = proc.wait()
stop_hb.set()
print(f"__COLAB_POOL_EXIT_CODE__={rc}", flush=True)
if rc != 0:
    raise SystemExit(rc)
'''


def build_job_bundle(
    job_workspace: Path,
    job_record: dict[str, Any],
    drives_dir: Path,
    output_bundle_path: Path,
) -> Path:
    """Create `/content/_job_bundle.tar.gz` containing user files, spec, and runner."""
    import tarfile
    import tempfile

    rclone_conf_b64 = ""
    rclone_remote_name = ""
    drive_profile = job_record.get("drive_profile", "").strip()
    if drive_profile:
        conf_file = drives_dir / f"{drive_profile}.conf"
        if not conf_file.exists():
            raise FileNotFoundError(
                f"Drive profile '{drive_profile}' not found at {conf_file}. "
                f"Run scripts/init_drive_profile.sh {drive_profile} first."
            )
        conf_text = conf_file.read_text(encoding="utf-8")
        rclone_remote_name = parse_first_rclone_remote(conf_text)
        rclone_conf_b64 = base64.b64encode(conf_text.encode("utf-8")).decode("ascii")

    spec = {
        "job_id": job_record["job_id"],
        "entrypoint": job_record["entrypoint"],
        "script_args": job_record.get("script_args", []),
        "pip_packages": job_record.get("pip_packages", []),
        "dataset_commands": job_record.get("dataset_commands", []),
        "env_vars": job_record.get("env_vars", {}),
        "drive_profile": drive_profile,
        "drive_subpath": job_record.get("drive_subpath", ""),
        "drive_sync_outputs_to": job_record.get("drive_sync_outputs_to", ""),
        "rclone_conf_b64": rclone_conf_b64,
        "rclone_remote_name": rclone_remote_name,
    }

    with tempfile.TemporaryDirectory(prefix="colab_bundle_") as tmp:
        tmp_path = Path(tmp)
        ws_tar = tmp_path / "workspace.tar.gz"
        with tarfile.open(ws_tar, "w:gz") as tf:
            for item in job_workspace.iterdir():
                tf.add(item, arcname=item.name)

        spec_file = tmp_path / "job_spec.json"
        spec_file.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

        runner_file = tmp_path / "_pool_runner.py"
        runner_file.write_text(REMOTE_POOL_RUNNER_PY, encoding="utf-8")

        output_bundle_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output_bundle_path, "w:gz") as bundle_tf:
            bundle_tf.add(ws_tar, arcname="workspace.tar.gz")
            bundle_tf.add(spec_file, arcname="job_spec.json")
            bundle_tf.add(runner_file, arcname="_pool_runner.py")

    return output_bundle_path

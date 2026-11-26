"""End-to-end tests for Colab Pro Pool (cross-platform Windows/POSIX, unittest & pytest)."""

from __future__ import annotations

import asyncio
import base64
import inspect
import io
import json
import os
import stat
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import colab_pool.mcp_server as mcp_mod
from colab_pool.api import create_app
from colab_pool.config import PoolSettings
from colab_pool.mcp_server import handle_jsonrpc_message
from colab_pool.utils import read_log_from_offset, read_log_tail


MOCK_COLAB_CLI_SCRIPT = r'''#!/usr/bin/env python3
"""Mock `google-colab-cli` binary simulating multi-account HOME isolation & VM execution."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

home = Path(os.environ["HOME"])
acct_name = home.name
state_root = Path(os.environ["MOCK_COLAB_CLOUD_ROOT"]) / acct_name
state_root.mkdir(parents=True, exist_ok=True)

args = sys.argv[1:]
if not args:
    sys.exit(1)

cmd = args[0]

def get_opt(flag: str, default: str = "") -> str:
    if flag in args:
        idx = args.index(flag)
        if idx + 1 < len(args):
            return args[idx + 1]
    return default

if cmd == "sessions":
    active = [p.name for p in state_root.iterdir() if p.is_dir()]
    print("\n".join(active))
    sys.exit(0)

if cmd == "stop":
    sess = get_opt("-s", "")
    if sess:
        shutil.rmtree(state_root / sess, ignore_errors=True)
        print(f"[mock-colab] stopped session {sess} on {acct_name}")
    else:
        for p in state_root.iterdir():
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        print(f"[mock-colab] stopped all sessions on {acct_name}")
    sys.exit(0)

if cmd == "new":
    sess = get_opt("-s", "default")
    if (home / "SIMULATE_AUTH_ERROR").exists():
        print("ERROR: UNAUTHENTICATED: OAuth invalid_grant token expired.", file=sys.stderr)
        sys.exit(1)
    if (home / "SIMULATE_EXHAUSTED").exists():
        print("ERROR: FAILED_PRECONDITION: Compute unit quota exhausted for this account.", file=sys.stderr)
        sys.exit(1)
    vm_root = state_root / sess / "fs"
    (vm_root / "content").mkdir(parents=True, exist_ok=True)
    (vm_root / "tmp").mkdir(parents=True, exist_ok=True)
    print(f"[mock-colab] allocated runtime {sess} on {acct_name}")
    sys.exit(0)

if cmd == "upload":
    sess = get_opt("-s", "default")
    positional = [a for i, a in enumerate(args[1:], start=1) if a != "-s" and args[i - 1] != "-s"]
    local_src, remote_dst = Path(positional[0]), positional[1].lstrip("/")
    vm_root = state_root / sess / "fs"
    dst = vm_root / remote_dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local_src, dst)
    print(f"[mock-colab] uploaded {local_src.name} -> /{remote_dst}")
    sys.exit(0)

if cmd == "download":
    sess = get_opt("-s", "default")
    positional = [a for i, a in enumerate(args[1:], start=1) if a != "-s" and args[i - 1] != "-s"]
    remote_src, local_dst = positional[0].lstrip("/"), Path(positional[1])
    vm_root = state_root / sess / "fs"
    src = vm_root / remote_src
    if not src.exists():
        print(f"Not found: {src}", file=sys.stderr)
        sys.exit(1)
    local_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, local_dst)
    print(f"[mock-colab] downloaded /{remote_src} -> {local_dst}")
    sys.exit(0)

if cmd == "exec":
    sess = get_opt("-s", "default")
    vm_root = state_root / sess / "fs"
    vm_root_posix = vm_root.as_posix()

    patched_entry = state_root / sess / "_patched_entry.py"
    wrapper = f"""
import subprocess, sys, tarfile
from pathlib import Path
vm_root = Path({vm_root_posix!r})
bundle = vm_root / "content" / "_job_bundle.tar.gz"
job_dir = vm_root / "content" / "_job"
job_dir.mkdir(parents=True, exist_ok=True)
with tarfile.open(bundle, "r:gz") as tf:
    tf.extractall(job_dir)

runner_py = job_dir / "_pool_runner.py"
txt = runner_py.read_text(encoding="utf-8")
txt = txt.replace('"/content', f'"{{vm_root.as_posix()}}/content').replace('"/tmp', f'"{{vm_root.as_posix()}}/tmp')
runner_py.write_text(txt, encoding="utf-8")

p = subprocess.run([sys.executable, "-u", str(runner_py)])
print(f"__COLAB_POOL_EXIT_CODE__={{p.returncode}}", flush=True)
sys.exit(p.returncode)
"""
    patched_entry.write_text(wrapper, encoding="utf-8")
    proc = subprocess.run([sys.executable, "-u", str(patched_entry)])
    sys.exit(proc.returncode)

sys.exit(1)
'''


def _install_mock_tool(tmp_path: Path, name: str, script_body: str) -> str:
    """Create a cross-platform runnable CLI mock in `tmp_path`."""
    if os.name == "nt":
        py_path = tmp_path / f"{name}.py"
        py_path.write_text(script_body, encoding="utf-8")
        cmd_path = tmp_path / f"{name}.cmd"
        cmd_path.write_text(
            f'@echo off\r\n"{sys.executable}" "{py_path}" %*\r\n',
            encoding="utf-8",
        )
        return str(cmd_path)

    bin_path = tmp_path / name
    bin_path.write_text(script_body, encoding="utf-8")
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC)
    return str(bin_path)


def _make_async_client(app) -> httpx.AsyncClient:
    client_kwargs: dict = {"base_url": "http://test"}
    if "app" in inspect.signature(httpx.AsyncClient.__init__).parameters:
        client_kwargs["app"] = app
    else:
        client_kwargs["transport"] = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(**client_kwargs)


class ColabProPoolEndToEndTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix=".tmp_colab_pool_test_", dir=str(Path(__file__).resolve().parent)
        )
        tmp_path = Path(self._tmp.name)

        self.cloud_root = tmp_path / "mock_cloud"
        self.cloud_root.mkdir()
        self._orig_cloud_root = os.environ.get("MOCK_COLAB_CLOUD_ROOT")
        os.environ["MOCK_COLAB_CLOUD_ROOT"] = str(self.cloud_root)

        mock_bin = _install_mock_tool(tmp_path, "mock_colab", MOCK_COLAB_CLI_SCRIPT)
        _install_mock_tool(
            tmp_path,
            "rclone",
            "#!/usr/bin/env python3\nimport sys\nprint('[mock-rclone]', ' '.join(sys.argv[1:]))\nsys.exit(0)\n",
        )
        _install_mock_tool(
            tmp_path,
            "nvidia-smi",
            "#!/usr/bin/env python3\nimport sys\nprint('NVIDIA A100-SXM4-40GB, 40960 MiB')\nsys.exit(0)\n",
        )

        self._orig_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{tmp_path}{os.pathsep}{self._orig_path}"

        data_dir = tmp_path / "data"
        self.settings = PoolSettings(
            data_dir=data_dir,
            accounts_dir=data_dir / "accounts",
            drives_dir=data_dir / "drives",
            jobs_dir=data_dir / "jobs",
            db_path=data_dir / "pool.sqlite3",
            colab_bin=mock_bin,
            api_key="test-secret-key",
            default_cooldown_seconds=600,
            poll_interval_seconds=0.12,
            cleanup_orphans_on_startup=True,
            host="127.0.0.1",
            port=8080,
        )
        self.settings.ensure_dirs()

        # Seed a dangling orphan session on `pro_02` to verify startup reaper cleans it
        (self.cloud_root / "pro_02" / "dangling-orphan-vm").mkdir(parents=True)

        for name, tier, exhausted in [
            ("pro_01", "pro", True),
            ("pro_02", "pro", False),
            ("pro_plus_01", "pro+", False),
        ]:
            acct_dir = self.settings.accounts_dir / name
            (acct_dir / ".config" / "colab-cli").mkdir(parents=True)
            if name == "pro_02":
                (acct_dir / ".config" / "colab-cli" / "token.json").write_text(
                    '{"access_token": "ok"}\n', encoding="utf-8"
                )
            (acct_dir / "tier.txt").write_text(tier + "\n", encoding="utf-8")
            if exhausted:
                (acct_dir / "SIMULATE_EXHAUSTED").write_text("1", encoding="utf-8")

        (self.settings.drives_dir / "my_gdrive.conf").write_text(
            '[personal_drive]\ntype = drive\nscope = drive\ntoken = {"access_token":"dummy"}\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        os.environ["PATH"] = self._orig_path
        if self._orig_cloud_root is None:
            os.environ.pop("MOCK_COLAB_CLOUD_ROOT", None)
        else:
            os.environ["MOCK_COLAB_CLOUD_ROOT"] = self._orig_cloud_root
        self._tmp.cleanup()

    async def test_startup_orphan_reaper_failover_drive_shim_and_h100_routing(self) -> None:
        app = create_app(self.settings)
        headers = {"Authorization": "Bearer test-secret-key"}

        await app.state.scheduler.start()
        try:
            self.assertEqual(list((self.cloud_root / "pro_02").iterdir()), [])

            async with _make_async_client(app) as client:
                unauth = await client.get("/api/v1/accounts")
                self.assertEqual(unauth.status_code, 401)

                pool_resp = await client.get("/api/v1/accounts", headers=headers)
                self.assertEqual(pool_resp.status_code, 200)
                pdata = pool_resp.json()
                self.assertEqual(len(pdata["accounts"]), 3)
                acct_map = {a["name"]: a for a in pdata["accounts"]}
                self.assertTrue(acct_map["pro_02"]["auth_configured"])
                self.assertFalse(acct_map["pro_01"]["auth_configured"])
                self.assertEqual(pdata["drive_profiles"][0]["profile"], "my_gdrive")
                self.assertEqual(pdata["drive_profiles"][0]["remote_name"], "personal_drive")

                training_script = """
import json, os, sys, types
from pathlib import Path

gc_pkg = types.ModuleType("google")
colab_pkg = types.ModuleType("google.colab")
drive_mod = types.ModuleType("google.colab.drive")
def _would_hang_in_headless(*args, **kwargs):
    raise RuntimeError("Should not invoke interactive browser OAuth mount!")
drive_mod.mount = _would_hang_in_headless
colab_pkg.drive = drive_mod
gc_pkg.colab = colab_pkg
sys.modules["google"] = gc_pkg
sys.modules["google.colab"] = colab_pkg
sys.modules["google.colab.drive"] = drive_mod

import importlib, sitecustomize
importlib.reload(sitecustomize)
from google.colab import drive
drive.mount("/content/drive")

assert os.environ.get("COLAB_POOL_DRIVE_READY") == "1", "Drive shim env not set!"
ds_file = Path(__file__).parent / "downloaded_public_data.txt"
assert ds_file.read_text().strip() == "PUBLIC_DATASET_OK"

out_dir = Path(os.environ["COLAB_OUTPUT_DIR"])
(out_dir / "metrics.json").write_text(json.dumps({"acc": 0.965, "loss": 0.08}), encoding="utf-8")
print("Training epoch 1/1 complete! acc=0.965")
"""
                sub_resp = await client.post(
                    "/api/v1/jobs",
                    headers=headers,
                    json={
                        "name": "eeg-ablation-01",
                        "gpu": "A100",
                        "entrypoint": "train.py",
                        "script_content": training_script,
                        "dataset_commands": ["echo PUBLIC_DATASET_OK > downloaded_public_data.txt"],
                        "drive_profile": "my_gdrive",
                        "drive_subpath": "Datasets/EEG",
                        "drive_sync_outputs_to": "Results/eeg-ablation-01",
                    },
                )
                self.assertEqual(sub_resp.status_code, 200)
                job_id = sub_resp.json()["job_id"]

                final_job = None
                for _ in range(100):
                    jr = await client.get(f"/api/v1/jobs/{job_id}", headers=headers)
                    jdata = jr.json()
                    if jdata["status"] in {"succeeded", "failed", "cancelled"}:
                        final_job = jdata
                        break
                    await asyncio.sleep(0.12)

                self.assertIsNotNone(final_job)
                assert final_job is not None
                logs_resp = await client.get(f"/api/v1/jobs/{job_id}/logs", headers=headers)
                self.assertEqual(
                    final_job["status"],
                    "succeeded",
                    f"Job failed: {final_job}\nLogs:\n{logs_resp.json().get('logs')}",
                )
                self.assertIn("pro_01", final_job["attempted_accounts"])
                self.assertIn(final_job["assigned_account"], {"pro_02", "pro_plus_01"})
                self.assertIn("metrics.json", final_job["output_files"])
                self.assertTrue(
                    any(d["path"] == "metrics.json" and d["size"] > 0 for d in final_job["output_file_details"])
                )
                self.assertTrue(bool(final_job["gpu_detected"]))
                self.assertEqual(final_job["runtime_info"]["gpu_devices"][0]["name"], "NVIDIA A100-SXM4-40GB")

                art_resp = await client.get(f"/api/v1/jobs/{job_id}/artifacts", headers=headers)
                self.assertEqual(art_resp.status_code, 200)
                self.assertGreater(len(art_resp.content), 30)

                accts = {
                    a["name"]: a
                    for a in (await client.get("/api/v1/accounts", headers=headers)).json()["accounts"]
                }
                self.assertEqual(accts["pro_01"]["status"], "cooldown")
                self.assertGreater(accts["pro_01"]["cooldown_remaining_seconds"], 0)
                self.assertEqual(accts[final_job["assigned_account"]]["status"], "idle")

                winning_cloud_dir = self.cloud_root / final_job["assigned_account"]
                self.assertEqual(list(winning_cloud_dir.iterdir()), [])

                # Verify H100 job routes strictly to `pro_plus_01`
                h100_resp = await client.post(
                    "/api/v1/jobs",
                    headers=headers,
                    json={
                        "name": "h100-only-job",
                        "gpu": "H100",
                        "entrypoint": "run.py",
                        "script_content": "print('H100 ok')",
                    },
                )
                h100_id = h100_resp.json()["job_id"]
                for _ in range(80):
                    jr = (await client.get(f"/api/v1/jobs/{h100_id}", headers=headers)).json()
                    if jr["status"] == "succeeded":
                        self.assertEqual(jr["assigned_account"], "pro_plus_01")
                        break
                    await asyncio.sleep(0.12)
                else:
                    self.fail("H100 job did not finish in time")
        finally:
            await app.state.scheduler.stop()

    async def test_account_persistence_patch_delete_and_pool_cleanup(self) -> None:
        app = create_app(self.settings)
        headers = {"Authorization": "Bearer test-secret-key"}
        await app.state.scheduler.start()
        try:
            async with _make_async_client(app) as client:
                # Disable pro_02 via reset endpoint and verify it persists across sync_accounts_from_disk()
                r = await client.post(
                    "/api/v1/accounts/pro_02/reset",
                    headers=headers,
                    json={"enabled": False},
                )
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["enabled"], 0)
                self.assertTrue((self.settings.accounts_dir / "pro_02" / ".disabled").exists())

                sync_resp = await client.post("/api/v1/pool/sync", headers=headers)
                self.assertEqual(sync_resp.status_code, 200)
                acct_map = {a["name"]: a for a in sync_resp.json()["accounts"]}
                self.assertEqual(acct_map["pro_02"]["enabled"], 0)

                # Update tier and re-enable via PATCH
                patch_resp = await client.patch(
                    "/api/v1/accounts/pro_02",
                    headers=headers,
                    json={"tier": "pro+", "enabled": True, "clear_cooldown": True},
                )
                self.assertEqual(patch_resp.status_code, 200)
                self.assertEqual(patch_resp.json()["tier"], "pro+")
                self.assertEqual(patch_resp.json()["enabled"], 1)
                self.assertFalse((self.settings.accounts_dir / "pro_02" / ".disabled").exists())
                self.assertEqual(
                    (self.settings.accounts_dir / "pro_02" / "tier.txt").read_text(encoding="utf-8").strip(),
                    "pro+",
                )

                # Seed a dangling VM and mark pro_02 busy in DB; cleanup must skip pro_02 and clean pro_plus_01
                (self.cloud_root / "pro_02" / "active-vm").mkdir(parents=True)
                (self.cloud_root / "pro_plus_01" / "orphan-vm").mkdir(parents=True)
                app.state.db.acquire_idle_account(job_id="fake-job-1", exclude_accounts=["pro_01", "pro_plus_01"])

                # Deleting busy account must fail with 409
                del_busy = await client.delete("/api/v1/accounts/pro_02", headers=headers)
                self.assertEqual(del_busy.status_code, 409)

                cleanup_resp = await client.post("/api/v1/pool/cleanup", headers=headers)
                self.assertEqual(cleanup_resp.status_code, 200)
                c_res = cleanup_resp.json()["results"]
                self.assertEqual(c_res["pro_02"], "skipped (busy)")
                self.assertTrue((self.cloud_root / "pro_02" / "active-vm").exists())
                self.assertEqual(list((self.cloud_root / "pro_plus_01").iterdir()), [])

                # Release fake-job-1 and delete pro_01 with remove_disk=true
                app.state.db.release_account("pro_02", increment_run=False)
                del_ok = await client.delete(
                    "/api/v1/accounts/pro_01?remove_disk=true", headers=headers
                )
                self.assertEqual(del_ok.status_code, 200)
                self.assertFalse((self.settings.accounts_dir / "pro_01").exists())
        finally:
            await app.state.scheduler.stop()

    async def test_security_validation_and_auth_check(self) -> None:
        app = create_app(self.settings)
        headers = {"Authorization": "Bearer test-secret-key"}
        # Do not start scheduler loop needed for submission validation
        async with _make_async_client(app) as client:
            # Auth check endpoint
            c_no = await client.get("/api/v1/auth/check")
            self.assertEqual(c_no.status_code, 200)
            self.assertEqual(c_no.json(), {"auth_required": True, "authenticated": False})

            c_ok = await client.get("/api/v1/auth/check", headers=headers)
            self.assertEqual(c_ok.status_code, 200)
            self.assertEqual(c_ok.json(), {"auth_required": True, "authenticated": True})

            # Unsafe drive_profile
            r_drv = await client.post(
                "/api/v1/jobs",
                headers=headers,
                json={"entrypoint": "train.py", "script_content": "print(1)", "drive_profile": "../evil"},
            )
            self.assertEqual(r_drv.status_code, 400)

            # Unsafe extra_files path
            r_extra = await client.post(
                "/api/v1/jobs",
                headers=headers,
                json={
                    "entrypoint": "train.py",
                    "script_content": "print(1)",
                    "extra_files": [{"path": "../escape.py", "content": "x=1"}],
                },
            )
            self.assertEqual(r_extra.status_code, 400)

            # Unsafe entrypoint
            r_ep = await client.post(
                "/api/v1/jobs",
                headers=headers,
                json={"entrypoint": "../../evil.py", "script_content": "print(1)"},
            )
            self.assertEqual(r_ep.status_code, 400)

            # Symlink inside workspace tar archive
            tar_buf = io.BytesIO()
            with tarfile.open(fileobj=tar_buf, mode="w:gz") as tf:
                sym = tarfile.TarInfo(name="bad_link")
                sym.type = tarfile.SYMTYPE
                sym.linkname = "/etc/passwd"
                tf.addfile(sym)
            r_sym = await client.post(
                "/api/v1/jobs",
                headers=headers,
                json={
                    "entrypoint": "train.py",
                    "script_content": "print(1)",
                    "workspace_tar_gz_b64": base64.b64encode(tar_buf.getvalue()).decode("ascii"),
                },
            )
            self.assertEqual(r_sym.status_code, 400)

            # Malformed base64
            r_b64 = await client.post(
                "/api/v1/jobs",
                headers=headers,
                json={"entrypoint": "train.py", "workspace_tar_gz_b64": "!!!not-base64!!!"},
            )
            self.assertEqual(r_b64.status_code, 400)

            # Multipart invalid spec_json
            r_mp = await client.post(
                "/api/v1/jobs/upload",
                headers=headers,
                files={"files": ("train.py", b"print(1)", "text/plain")},
                data={"spec_json": "{not-valid-json"},
            )
            self.assertEqual(r_mp.status_code, 400)

            # Ensure failed submissions left zero orphan directories in jobs_dir
            self.assertEqual(list(self.settings.jobs_dir.iterdir()), [])

    async def test_jobs_priority_search_files_workspace_sse_retry_delete_and_stats(self) -> None:
        # Test priority queue ordering directly before starting scheduler
        app = create_app(self.settings)
        headers = {"Authorization": "Bearer test-secret-key"}
        async with _make_async_client(app) as client:
            j_low = await client.post(
                "/api/v1/jobs",
                headers=headers,
                json={"name": "low-pri", "priority": -5, "entrypoint": "a.py", "script_content": "print('low')"},
            )
            j_high = await client.post(
                "/api/v1/jobs",
                headers=headers,
                json={"name": "high-pri", "priority": 20, "entrypoint": "a.py", "script_content": "print('high')"},
            )
            self.assertEqual(j_low.status_code, 200)
            self.assertEqual(j_high.status_code, 200)

            queued = app.state.db.list_queued_jobs()
            self.assertEqual([q["name"] for q in queued], ["high-pri", "low-pri"])

            # Delete low-pri while queued so only main test runs
            del_q = await client.delete(f"/api/v1/jobs/{j_low.json()['job_id']}", headers=headers)
            self.assertEqual(del_q.status_code, 200)
            del_h = await client.delete(f"/api/v1/jobs/{j_high.json()['job_id']}", headers=headers)
            self.assertEqual(del_h.status_code, 200)

        await app.state.scheduler.start()
        try:
            async with _make_async_client(app) as client:
                script = """
import json, os
from pathlib import Path

for i in range(6):
    print(f"日志行 {i}: 中文UTF-8输出测试", flush=True)

out = Path(os.environ["COLAB_OUTPUT_DIR"])
(out / "sub").mkdir(parents=True, exist_ok=True)
(out / "summary.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
(out / "sub" / "notes.txt").write_text("hello output", encoding="utf-8")
"""
                sub = await client.post(
                    "/api/v1/jobs",
                    headers=headers,
                    json={
                        "name": "alpha-search-exp",
                        "gpu": "T4",
                        "priority": 5,
                        "entrypoint": "train.py",
                        "script_content": script,
                        "extra_files": [{"path": "cfg/params.yaml", "content": "lr: 0.001\n"}],
                    },
                )
                self.assertEqual(sub.status_code, 200)
                job_id = sub.json()["job_id"]

                for _ in range(100):
                    jr = (await client.get(f"/api/v1/jobs/{job_id}", headers=headers)).json()
                    if jr["status"] == "succeeded":
                        break
                    await asyncio.sleep(0.12)
                else:
                    self.fail("Job did not succeed in time")

                # List search + pagination
                list_r = await client.get(
                    "/api/v1/jobs?search=alpha-search&gpu=T4&status=succeeded&limit=10&offset=0",
                    headers=headers,
                )
                self.assertEqual(list_r.status_code, 200)
                ldata = list_r.json()
                self.assertEqual(ldata["total"], 1)
                self.assertEqual(ldata["jobs"][0]["job_id"], job_id)

                # Logs offset & tail
                tail_r = await client.get(f"/api/v1/jobs/{job_id}/logs?tail_lines=3", headers=headers)
                self.assertEqual(tail_r.status_code, 200)
                self.assertLessEqual(tail_r.json()["total_lines"], 3)
                off_r = await client.get(f"/api/v1/jobs/{job_id}/logs?offset=0", headers=headers)
                self.assertEqual(off_r.status_code, 200)
                self.assertIn("中文UTF-8输出测试", off_r.json()["logs"])

                # SSE stream endpoint
                sse_r = await client.get(f"/api/v1/jobs/{job_id}/logs/stream?offset=0", headers=headers)
                self.assertEqual(sse_r.status_code, 200)
                self.assertIn("text/event-stream", sse_r.headers["content-type"])
                sse_body = sse_r.text
                self.assertIn("event: log", sse_body)
                self.assertIn("event: status", sse_body)
                self.assertIn("event: done", sse_body)

                # Single output file preview/download
                f_r = await client.get(f"/api/v1/jobs/{job_id}/files/sub/notes.txt", headers=headers)
                self.assertEqual(f_r.status_code, 200)
                self.assertEqual(f_r.text, "hello output")

                # Workspace inspect
                ws_r = await client.get(
                    f"/api/v1/jobs/{job_id}/workspace?file=cfg/params.yaml", headers=headers
                )
                self.assertEqual(ws_r.status_code, 200)
                ws_data = ws_r.json()
                self.assertEqual(ws_data["content"], "lr: 0.001\n")
                self.assertFalse(ws_data["truncated"])
                self.assertIn("train.py", {f["path"] for f in ws_data["files"]})

                # Stats endpoint
                stats_r = await client.get("/api/v1/stats", headers=headers)
                self.assertEqual(stats_r.status_code, 200)
                sdata = stats_r.json()
                self.assertEqual(sdata["jobs"]["succeeded"], 1)
                self.assertEqual(sdata["success_rate"], 1.0)
                self.assertGreater(sdata["jobs_disk_bytes"], 0)

                # Retry endpoint
                retry_r = await client.post(
                    f"/api/v1/jobs/{job_id}/retry",
                    headers=headers,
                    json={"gpu": "L4"},
                )
                self.assertEqual(retry_r.status_code, 200)
                retry_id = retry_r.json()["job_id"]
                self.assertNotEqual(retry_id, job_id)
                self.assertEqual(retry_r.json()["gpu"], "L4")

                for _ in range(100):
                    rr = (await client.get(f"/api/v1/jobs/{retry_id}", headers=headers)).json()
                    if rr["status"] == "succeeded":
                        break
                    await asyncio.sleep(0.12)
                else:
                    self.fail("Retried job did not finish")

                # Delete original job
                del_r = await client.delete(f"/api/v1/jobs/{job_id}", headers=headers)
                self.assertEqual(del_r.status_code, 200)
                self.assertFalse((self.settings.jobs_dir / job_id).exists())
                missing_r = await client.get(f"/api/v1/jobs/{job_id}", headers=headers)
                self.assertEqual(missing_r.status_code, 404)
        finally:
            await app.state.scheduler.stop()

    async def test_cancel_running_job_skips_download_and_releases_account(self) -> None:
        app = create_app(self.settings)
        headers = {"Authorization": "Bearer test-secret-key"}
        await app.state.scheduler.start()
        try:
            async with _make_async_client(app) as client:
                slow_script = """
import os, time
from pathlib import Path
print("SLOW_STARTED", flush=True)
out = Path(os.environ["COLAB_OUTPUT_DIR"])
(out / "should_not_download.txt").write_text("partial", encoding="utf-8")
time.sleep(20)
"""
                sub = await client.post(
                    "/api/v1/jobs",
                    headers=headers,
                    json={"name": "cancel-me", "gpu": "T4", "entrypoint": "slow.py", "script_content": slow_script},
                )
                self.assertEqual(sub.status_code, 200)
                job_id = sub.json()["job_id"]

                # Wait until job is running and SLOW_STARTED appears in log
                for _ in range(100):
                    jr = (await client.get(f"/api/v1/jobs/{job_id}", headers=headers)).json()
                    lr = (await client.get(f"/api/v1/jobs/{job_id}/logs", headers=headers)).json()
                    if jr["status"] == "running" and "SLOW_STARTED" in lr["logs"]:
                        break
                    await asyncio.sleep(0.12)
                else:
                    self.fail("Slow job never started")

                # Active job cannot be deleted (409)
                del_active = await client.delete(f"/api/v1/jobs/{job_id}", headers=headers)
                self.assertEqual(del_active.status_code, 409)

                # Cancel running job
                cancel_r = await client.post(f"/api/v1/jobs/{job_id}/cancel", headers=headers)
                self.assertEqual(cancel_r.status_code, 200)

                final_job = None
                for _ in range(100):
                    jr = (await client.get(f"/api/v1/jobs/{job_id}", headers=headers)).json()
                    if jr["status"] in {"cancelled", "failed", "succeeded"}:
                        final_job = jr
                        break
                    await asyncio.sleep(0.12)

                self.assertIsNotNone(final_job)
                assert final_job is not None
                self.assertEqual(final_job["status"], "cancelled")
                self.assertEqual(final_job["artifacts_archive"], "")

                # Assigned account must be released back to idle once teardown finishes
                acct_released = False
                for _ in range(30):
                    accts = {
                        a["name"]: a
                        for a in (await client.get("/api/v1/accounts", headers=headers)).json()["accounts"]
                    }
                    if accts[final_job["assigned_account"]]["status"] == "idle":
                        acct_released = True
                        break
                    await asyncio.sleep(0.1)
                self.assertTrue(acct_released, "Assigned account was not released back to idle after cancellation")
        finally:
            await app.state.scheduler.stop()

    async def test_auth_error_classification_and_spa_serving(self) -> None:
        # Mark pro_02 with SIMULATE_AUTH_ERROR; keep pro_plus_01 healthy
        (self.settings.accounts_dir / "pro_02" / "SIMULATE_AUTH_ERROR").write_text("1", encoding="utf-8")
        app = create_app(self.settings)
        headers = {"Authorization": "Bearer test-secret-key"}
        await app.state.scheduler.start()
        try:
            async with _make_async_client(app) as client:
                sub = await client.post(
                    "/api/v1/jobs",
                    headers=headers,
                    json={"name": "auth-failover", "gpu": "T4", "entrypoint": "run.py", "script_content": "print('ok')"},
                )
                self.assertEqual(sub.status_code, 200)
                job_id = sub.json()["job_id"]

                for _ in range(100):
                    jr = (await client.get(f"/api/v1/jobs/{job_id}", headers=headers)).json()
                    if jr["status"] == "succeeded":
                        break
                    await asyncio.sleep(0.12)
                else:
                    self.fail("Failover job did not finish")

                accts = {
                    a["name"]: a
                    for a in (await client.get("/api/v1/accounts", headers=headers)).json()["accounts"]
                }
                self.assertEqual(accts["pro_02"]["status"], "cooldown")
                self.assertTrue(accts["pro_02"]["last_error"].startswith("[AUTH_REQUIRED]"))
                self.assertGreater(accts["pro_02"]["cooldown_remaining_seconds"], 3000)

                # Built SPA routes
                root_r = await client.get("/")
                self.assertEqual(root_r.status_code, 200)
                self.assertIn("text/html", root_r.headers["content-type"])
                self.assertIn("Colab Pro Pool", root_r.text)

                spa_r = await client.get(f"/jobs/{job_id}")
                self.assertEqual(spa_r.status_code, 200)
                self.assertIn("text/html", spa_r.headers["content-type"])

                api_404 = await client.get("/api/v1/nonexistent", headers=headers)
                self.assertEqual(api_404.status_code, 404)
        finally:
            await app.state.scheduler.stop()

    def test_log_utf8_offset_boundary_and_tail(self) -> None:
        log_file = Path(self._tmp.name) / "utf8.log"
        # Repeat 3-byte UTF-8 Chinese text so a 10-byte chunk boundary splits a character
        content = ("中文日志line\n" * 20).encode("utf-8")
        log_file.write_bytes(content)

        # Incremental reads with max_bytes=10 must reconstruct exact UTF-8 without U+FFFD
        cursor = 0
        pieces: list[str] = []
        while cursor < len(content):
            chunk, next_off, total = read_log_from_offset(log_file, offset=cursor, max_bytes=10)
            self.assertEqual(total, len(content))
            self.assertNotIn("\ufffd", chunk)
            self.assertGreater(next_off, cursor)
            pieces.append(chunk)
            cursor = next_off
        self.assertEqual("".join(pieces), content.decode("utf-8"))

        tail_txt, size, cnt = read_log_tail(log_file, tail_lines=4)
        self.assertEqual(size, len(content))
        self.assertEqual(cnt, 4)
        self.assertEqual(len(tail_txt.splitlines()), 4)

    def test_mcp_jsonrpc_protocol_and_new_tools(self) -> None:
        init_reply = handle_jsonrpc_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert init_reply is not None
        self.assertEqual(init_reply["result"]["serverInfo"]["name"], "colab-pro-pool")

        tools_reply = handle_jsonrpc_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert tools_reply is not None
        tool_names = {t["name"] for t in tools_reply["result"]["tools"]}
        self.assertTrue(
            {
                "colab_pool_status",
                "submit_colab_job",
                "list_colab_jobs",
                "get_colab_job",
                "retry_colab_job",
                "download_colab_artifacts",
                "cancel_colab_job",
            }.issubset(tool_names)
        )

        # Verify list_colab_jobs and retry_colab_job tool dispatch via MockTransport
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/api/v1/jobs":
                return httpx.Response(200, json={"jobs": [{"job_id": "abc123"}], "total": 1})
            if request.method == "POST" and request.url.path == "/api/v1/jobs/abc123/retry":
                body = json.loads(request.content.decode("utf-8"))
                return httpx.Response(200, json={"job_id": "def456", "gpu": body.get("gpu", "T4")})
            return httpx.Response(404)

        mock_client = httpx.Client(base_url="http://mock", transport=httpx.MockTransport(handler))
        with patch.object(mcp_mod, "_client", return_value=mock_client):
            list_res = handle_jsonrpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "list_colab_jobs", "arguments": {"status": "succeeded", "limit": 5}},
                }
            )
            assert list_res is not None
            self.assertFalse(list_res["result"]["isError"])
            self.assertIn("abc123", list_res["result"]["content"][0]["text"])

            mock_client2 = httpx.Client(base_url="http://mock", transport=httpx.MockTransport(handler))
            with patch.object(mcp_mod, "_client", return_value=mock_client2):
                retry_res = handle_jsonrpc_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {"name": "retry_colab_job", "arguments": {"job_id": "abc123", "gpu": "A100"}},
                    }
                )
                assert retry_res is not None
                self.assertFalse(retry_res["result"]["isError"])
                parsed = json.loads(retry_res["result"]["content"][0]["text"])
                self.assertEqual(parsed["job_id"], "def456")
                self.assertEqual(parsed["gpu"], "A100")


if __name__ == "__main__":
    unittest.main()

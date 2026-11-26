"""Tests for `cpool` / `colab-pool` CLI, persistent profiles, `.cpoolignore`, and SSE streaming."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import socket
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn

import colab_pool.cli as cli_mod
from colab_pool.api import create_app
from colab_pool.cli_config import (
    delete_profile,
    load_config,
    resolve_profile,
    save_profile,
    set_current_profile,
)
from colab_pool.config import PoolSettings
from colab_pool.utils import pack_local_dir_b64


def _make_tar_gz_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class CliConfigAndPackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix=".tmp_cli_cfg_", dir=str(Path(__file__).resolve().parent)
        )
        self.tmp_path = Path(self._tmp.name)
        self.cfg_path = self.tmp_path / "config.json"
        self._orig_env = {
            k: os.environ.get(k)
            for k in (
                "COLAB_POOL_CONFIG_PATH",
                "COLAB_POOL_PROFILE",
                "COLAB_POOL_SERVER_URL",
                "COLAB_POOL_API_KEY",
            )
        }
        os.environ["COLAB_POOL_CONFIG_PATH"] = str(self.cfg_path)
        for k in ("COLAB_POOL_PROFILE", "COLAB_POOL_SERVER_URL", "COLAB_POOL_API_KEY"):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def test_profile_lifecycle_and_precedence(self) -> None:
        # Missing config defaults
        name, prof = resolve_profile(path=self.cfg_path)
        self.assertEqual(name, "default")
        self.assertEqual(prof["server"], "http://127.0.0.1:8080")
        self.assertEqual(prof["api_key"], "")

        save_profile(
            "lab",
            server="http://10.0.0.9:8080/",
            api_key="lab-secret-1234",
            default_gpu="A100",
            default_timeout=7200,
            path=self.cfg_path,
        )
        save_profile(
            "backup",
            server="http://10.0.0.10:8080",
            api_key="backup-secret",
            set_current=False,
            path=self.cfg_path,
        )

        loaded = load_config(self.cfg_path)
        self.assertEqual(loaded["current_profile"], "lab")
        name, prof = resolve_profile(path=self.cfg_path)
        self.assertEqual(name, "lab")
        self.assertEqual(prof["server"], "http://10.0.0.9:8080")
        self.assertEqual(prof["api_key"], "lab-secret-1234")
        self.assertEqual(prof["default_gpu"], "A100")
        self.assertEqual(prof["default_timeout"], 7200)

        # Env overrides saved profile
        os.environ["COLAB_POOL_SERVER_URL"] = "http://env.local:9000/"
        os.environ["COLAB_POOL_API_KEY"] = "env-key"
        _, env_prof = resolve_profile(path=self.cfg_path)
        self.assertEqual(env_prof["server"], "http://env.local:9000")
        self.assertEqual(env_prof["api_key"], "env-key")

        # Explicit CLI flag overrides env
        _, cli_prof = resolve_profile(
            server_override="http://cli.local:8888/",
            api_key_override="cli-key",
            path=self.cfg_path,
        )
        self.assertEqual(cli_prof["server"], "http://cli.local:8888")
        self.assertEqual(cli_prof["api_key"], "cli-key")

        os.environ.pop("COLAB_POOL_SERVER_URL", None)
        os.environ.pop("COLAB_POOL_API_KEY", None)
        self.assertTrue(set_current_profile("backup", path=self.cfg_path))
        self.assertFalse(set_current_profile("nonexistent", path=self.cfg_path))
        self.assertEqual(resolve_profile(path=self.cfg_path)[0], "backup")

        self.assertTrue(delete_profile("backup", path=self.cfg_path))
        self.assertEqual(resolve_profile(path=self.cfg_path)[0], "lab")

    def test_pack_local_dir_honors_default_and_cpoolignore(self) -> None:
        proj = self.tmp_path / "proj"
        (proj / ".git").mkdir(parents=True)
        (proj / "__pycache__").mkdir()
        (proj / "node_modules").mkdir()
        (proj / "checkpoints").mkdir()
        (proj / "data" / "raw").mkdir(parents=True)
        (proj / "src").mkdir()

        (proj / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        (proj / "__pycache__" / "a.pyc").write_bytes(b"123")
        (proj / "node_modules" / "pkg.js").write_text("x", encoding="utf-8")
        (proj / "checkpoints" / "epoch1.pt").write_bytes(b"weights")
        (proj / "data" / "raw" / "big.npy").write_bytes(b"raw")
        (proj / "data" / "meta.json").write_text("{}", encoding="utf-8")
        (proj / "src" / "model.py").write_text("print('model')", encoding="utf-8")
        (proj / "train.py").write_text("print('train')", encoding="utf-8")
        (proj / ".cpoolignore").write_text(
            "# ignore heavy artifacts\ncheckpoints/\n*.npy\n",
            encoding="utf-8",
        )

        b64 = pack_local_dir_b64(proj)
        raw = base64.b64decode(b64)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            names = set(tf.getnames())

        self.assertIn("train.py", names)
        self.assertIn("src/model.py", names)
        self.assertIn("data/meta.json", names)
        self.assertNotIn(".git/HEAD", names)
        self.assertNotIn("__pycache__/a.pyc", names)
        self.assertNotIn("node_modules/pkg.js", names)
        self.assertNotIn("checkpoints/epoch1.pt", names)
        self.assertNotIn("data/raw/big.npy", names)


class CpoolCliCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix=".tmp_cli_cmds_", dir=str(Path(__file__).resolve().parent)
        )
        self.tmp_path = Path(self._tmp.name)
        self.cfg_path = self.tmp_path / "config.json"
        self._orig_cfg = os.environ.get("COLAB_POOL_CONFIG_PATH")
        os.environ["COLAB_POOL_CONFIG_PATH"] = str(self.cfg_path)
        save_profile(
            "default",
            server="http://127.0.0.1:8080",
            api_key="test-key-1234",
            path=self.cfg_path,
        )

    def tearDown(self) -> None:
        if self._orig_cfg is None:
            os.environ.pop("COLAB_POOL_CONFIG_PATH", None)
        else:
            os.environ["COLAB_POOL_CONFIG_PATH"] = self._orig_cfg
        self._tmp.cleanup()

    def _run_cli(
        self,
        argv: list[str],
        handler=None,
    ) -> tuple[int, str, str]:
        out_buf = io.StringIO()
        err_buf = io.StringIO()

        def factory(server_url: str, api_key: str, *, streaming: bool = False) -> httpx.Client:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            return httpx.Client(
                base_url=server_url.rstrip("/"),
                headers=headers,
                transport=httpx.MockTransport(handler),
            )

        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            if handler is not None:
                with patch.object(cli_mod, "_make_client", side_effect=factory):
                    rc = cli_mod.main(argv)
            else:
                rc = cli_mod.main(argv)
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def test_login_config_and_tunnel_print_only(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/auth/check":
                auth = request.headers.get("authorization", "")
                if auth == "Bearer good-secret":
                    return httpx.Response(200, json={"auth_required": True, "authenticated": True})
                return httpx.Response(200, json={"auth_required": True, "authenticated": False})
            return httpx.Response(404)

        rc, out, _ = self._run_cli(
            ["login", "http://10.0.0.8:8080", "--api-key", "good-secret", "--gpu", "A100", "--json"],
            handler,
        )
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["server"], "http://10.0.0.8:8080")
        self.assertEqual(data["default_gpu"], "A100")

        # Invalid key fails cleanly without traceback
        rc_bad, _, err_bad = self._run_cli(
            ["login", "http://10.0.0.8:8080", "--api-key", "wrong-secret"],
            handler,
        )
        self.assertEqual(rc_bad, 2)
        self.assertIn("Authentication failed", err_bad)

        # Config commands
        rc_cfg, out_cfg, _ = self._run_cli(["config", "show"])
        self.assertEqual(rc_cfg, 0)
        self.assertIn("good****cret", out_cfg)
        self.assertNotIn("good-secret", out_cfg)

        rc_gpu, out_gpu, _ = self._run_cli(["config", "set-gpu", "H100", "--json"])
        self.assertEqual(rc_gpu, 0)
        self.assertEqual(json.loads(out_gpu)["default_gpu"], "H100")

        # Tunnel --print-only
        rc_tun, out_tun, _ = self._run_cli(
            ["tunnel", "alice@gpu-box", "--local-port", "18080", "--print-only", "--json"]
        )
        self.assertEqual(rc_tun, 0)
        tun_data = json.loads(out_tun)
        self.assertIn("18080:127.0.0.1:8080", tun_data["shell"])
        self.assertIn("alice@gpu-box", tun_data["shell"])

    def test_status_json_flag_position_and_accounts_commands(self) -> None:
        calls: list[tuple[str, str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body_txt = request.content.decode("utf-8") if request.content else ""
            calls.append((request.method, request.url.path, body_txt))
            if request.url.path == "/api/v1/stats":
                return httpx.Response(
                    200,
                    json={
                        "accounts": {"total": 2, "idle": 1, "busy": 1, "cooldown": 0, "disabled": 0},
                        "jobs": {"queued": 0, "provisioning": 0, "running": 1, "succeeded": 4, "failed": 1, "cancelled": 0, "total": 6},
                        "gpu_counts": {"A100": 5},
                        "success_rate": 0.8,
                        "avg_duration_seconds": 125.0,
                        "jobs_disk_bytes": 4096,
                        "drive_profiles_count": 0,
                    },
                )
            if request.url.path == "/api/v1/accounts" and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "accounts": [
                            {
                                "name": "pro_01",
                                "tier": "pro",
                                "status": "idle",
                                "enabled": 1,
                                "auth_configured": True,
                                "cooldown_remaining_seconds": 0,
                                "total_runs": 4,
                                "total_failures": 0,
                                "current_job_id": None,
                                "last_error": "",
                            },
                            {
                                "name": "pro_plus_01",
                                "tier": "pro+",
                                "status": "busy",
                                "enabled": 1,
                                "auth_configured": True,
                                "cooldown_remaining_seconds": 0,
                                "total_runs": 1,
                                "total_failures": 0,
                                "current_job_id": "job999",
                                "last_error": "",
                            },
                        ],
                        "drive_profiles": [],
                    },
                )
            if request.url.path == "/api/v1/accounts" and request.method == "POST":
                return httpx.Response(200, json={"name": "pro_03", "tier": "pro+"})
            if request.url.path == "/api/v1/accounts/pro_01" and request.method == "PATCH":
                payload = json.loads(body_txt)
                return httpx.Response(
                    200,
                    json={"name": "pro_01", "tier": payload.get("tier", "pro"), "enabled": 1, "status": "idle"},
                )
            if request.url.path == "/api/v1/accounts/pro_01" and request.method == "DELETE":
                return httpx.Response(200, json={"deleted": "pro_01"})
            if request.url.path == "/api/v1/pool/sync":
                return httpx.Response(200, json={"accounts": [{"name": "pro_01"}]})
            if request.url.path == "/api/v1/pool/cleanup":
                return httpx.Response(200, json={"results": {"pro_01": "ok", "pro_plus_01": "skipped (busy)"}})
            return httpx.Response(404)

        # Human table
        rc_h, out_h, _ = self._run_cli(["status"], handler)
        self.assertEqual(rc_h, 0)
        self.assertIn("pro_01", out_h)
        self.assertIn("job:job999", out_h)

        # --json BEFORE subcommand and AFTER subcommand must match
        rc1, out1, _ = self._run_cli(["--json", "status"], handler)
        rc2, out2, _ = self._run_cli(["status", "--json"], handler)
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        self.assertEqual(json.loads(out1)["stats"]["jobs"]["total"], 6)
        self.assertEqual(json.loads(out1), json.loads(out2))

        # Accounts operations
        self.assertEqual(self._run_cli(["accounts", "list"], handler)[0], 0)
        self.assertEqual(self._run_cli(["accounts", "add", "pro_03", "--tier", "pro+"], handler)[0], 0)
        self.assertEqual(self._run_cli(["accounts", "disable", "pro_01"], handler)[0], 0)
        self.assertEqual(self._run_cli(["accounts", "enable", "pro_01"], handler)[0], 0)
        self.assertEqual(self._run_cli(["accounts", "reset", "pro_01"], handler)[0], 0)
        self.assertEqual(self._run_cli(["accounts", "tier", "pro_01", "pro+"], handler)[0], 0)
        self.assertEqual(self._run_cli(["accounts", "rm", "pro_01", "--remove-disk"], handler)[0], 0)
        self.assertEqual(self._run_cli(["accounts", "sync"], handler)[0], 0)
        rc_cln, out_cln, _ = self._run_cli(["accounts", "cleanup"], handler)
        self.assertEqual(rc_cln, 0)
        self.assertIn("skipped (busy)", out_cln)

    def test_run_exec_sse_logs_wait_files_pull_cancel_retry_rm(self) -> None:
        archive_bytes = _make_tar_gz_bytes(
            {
                "metrics.json": b'{"acc": 0.99}',
                "sub/chart.txt": b"plot-data",
            }
        )
        sse_stream_body = (
            'event: status\ndata: {"status": "running", "assigned_account": "pro_02", "gpu_detected": "NVIDIA A100"}\n\n'
            'event: log\ndata: {"chunk": "epoch 1 acc=0.99\\n", "offset": 18}\n\n'
            'event: done\ndata: {"status": "succeeded", "offset": 18}\n\n'
        ).encode("utf-8")

        captured_submit: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/api/v1/jobs" and request.method == "POST":
                body = json.loads(request.content.decode("utf-8"))
                captured_submit.append(body)
                return httpx.Response(
                    200,
                    json={
                        "job_id": "job123",
                        "name": body["name"],
                        "gpu": body["gpu"],
                        "priority": body.get("priority", 0),
                        "entrypoint": body["entrypoint"],
                        "status": "queued",
                    },
                )
            if path == "/api/v1/jobs" and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "jobs": [
                            {
                                "job_id": "job123",
                                "name": "eeg-exp",
                                "gpu": "A100",
                                "tpu": "",
                                "status": "succeeded",
                                "assigned_account": "pro_02",
                                "started_at": 100.0,
                                "finished_at": 135.0,
                                "created_at": 95.0,
                            }
                        ],
                        "total": 1,
                    },
                )
            if path == "/api/v1/jobs/job123" and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "job_id": "job123",
                        "name": "eeg-exp",
                        "gpu": "A100",
                        "tpu": "",
                        "high_mem": True,
                        "priority": 10,
                        "gpu_detected": "NVIDIA A100",
                        "entrypoint": "train.py",
                        "status": "succeeded",
                        "assigned_account": "pro_02",
                        "attempted_accounts": ["pro_01", "pro_02"],
                        "exit_code": 0,
                        "artifacts_archive": "/tmp/outputs.tar.gz",
                        "output_files": ["metrics.json", "sub/chart.txt"],
                        "output_file_details": [
                            {"path": "metrics.json", "size": 13},
                            {"path": "sub/chart.txt", "size": 9},
                        ],
                        "created_at": 95.0,
                        "started_at": 100.0,
                        "finished_at": 135.0,
                    },
                )
            if path == "/api/v1/jobs/job123/logs":
                return httpx.Response(
                    200,
                    json={
                        "job_id": "job123",
                        "status": "running",
                        "logs": "starting...\n",
                        "next_offset": 12,
                        "file_size": 12,
                    },
                )
            if path == "/api/v1/jobs/job123/logs/stream":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=sse_stream_body,
                )
            if path == "/api/v1/jobs/job123/workspace":
                return httpx.Response(
                    200,
                    json={"files": [{"path": "train.py", "size": 42}]},
                )
            if path == "/api/v1/jobs/job123/artifacts":
                return httpx.Response(200, content=archive_bytes)
            if path == "/api/v1/jobs/job123/files/metrics.json":
                return httpx.Response(200, content=b'{"acc": 0.99}')
            if path == "/api/v1/jobs/job123/cancel":
                return httpx.Response(200, json={"job_id": "job123", "status": "cancelled"})
            if path == "/api/v1/jobs/job123/retry":
                return httpx.Response(200, json={"job_id": "job456", "gpu": "H100", "status": "queued"})
            if path == "/api/v1/jobs/job123" and request.method == "DELETE":
                return httpx.Response(200, json={"deleted": "job123"})
            if path == "/api/v1/jobs/missing":
                return httpx.Response(404, json={"detail": "Job 'missing' not found."})
            return httpx.Response(404)

        script = self.tmp_path / "train.py"
        script.write_text("print('hi')", encoding="utf-8")
        out_dir = self.tmp_path / "pulled_all"

        # 1. `run` with --env, --priority, --out-dir (auto-waits, streams SSE, extracts archive)
        rc_run, out_run, _ = self._run_cli(
            [
                "run",
                str(script),
                "--gpu",
                "A100",
                "--priority",
                "10",
                "--env",
                "SEED=42",
                "--pip",
                "einops",
                "--out-dir",
                str(out_dir),
            ],
            handler,
        )
        self.assertEqual(rc_run, 0)
        self.assertIn("epoch 1 acc=0.99", out_run)
        self.assertEqual(captured_submit[0]["env_vars"], {"SEED": "42"})
        self.assertEqual(captured_submit[0]["priority"], 10)
        self.assertEqual((out_dir / "metrics.json").read_text(encoding="utf-8"), '{"acc": 0.99}')

        # 2. `exec -c`
        rc_exec, out_exec, _ = self._run_cli(
            ["exec", "-c", "print(123)", "--gpu", "T4"],
            handler,
        )
        self.assertEqual(rc_exec, 0)
        self.assertIn("epoch 1 acc=0.99", out_exec)
        self.assertEqual(captured_submit[-1]["entrypoint"], "inline_exec.py")

        # 3. `jobs`, `show`, `logs`, `logs -f`, `wait`, `files`
        rc_jobs, out_jobs, _ = self._run_cli(["jobs", "--status", "succeeded"], handler)
        self.assertEqual(rc_jobs, 0)
        self.assertIn("job123", out_jobs)

        rc_show, out_show, _ = self._run_cli(["show", "job123"], handler)
        self.assertEqual(rc_show, 0)
        self.assertIn("pro_01 -> pro_02", out_show)

        rc_logs, out_logs, _ = self._run_cli(["logs", "job123"], handler)
        self.assertEqual(rc_logs, 0)
        self.assertIn("starting...", out_logs)

        rc_logsf, out_logsf, _ = self._run_cli(["logs", "job123", "-f"], handler)
        self.assertEqual(rc_logsf, 0)
        self.assertIn("epoch 1 acc=0.99", out_logsf)

        rc_wait, out_wait, _ = self._run_cli(["wait", "job123", "--quiet", "--json"], handler)
        self.assertEqual(rc_wait, 0)
        self.assertEqual(json.loads(out_wait)["status"], "succeeded")

        rc_files, out_files, _ = self._run_cli(["files", "job123"], handler)
        self.assertEqual(rc_files, 0)
        self.assertIn("metrics.json", out_files)

        rc_ws, out_ws, _ = self._run_cli(["files", "job123", "--workspace"], handler)
        self.assertEqual(rc_ws, 0)
        self.assertIn("train.py", out_ws)

        # 4. `pull --file` single file + reject traversal
        single_dir = self.tmp_path / "single_dl"
        rc_single, _, _ = self._run_cli(
            ["pull", "job123", str(single_dir), "--file", "metrics.json"],
            handler,
        )
        self.assertEqual(rc_single, 0)
        self.assertEqual((single_dir / "metrics.json").read_text(encoding="utf-8"), '{"acc": 0.99}')

        rc_trav, _, err_trav = self._run_cli(
            ["pull", "job123", str(single_dir), "--file", "../evil.txt"],
            handler,
        )
        self.assertEqual(rc_trav, 2)
        self.assertIn("Unsafe", err_trav)

        # 5. `cancel`, `retry`, `rm`, and friendly 404 error
        self.assertEqual(self._run_cli(["cancel", "job123"], handler)[0], 0)
        rc_ret, out_ret, _ = self._run_cli(["retry", "job123", "--gpu", "H100", "--json"], handler)
        self.assertEqual(rc_ret, 0)
        self.assertEqual(json.loads(out_ret)["job_id"], "job456")
        self.assertEqual(self._run_cli(["rm", "job123"], handler)[0], 0)

        rc_404, out_404, _ = self._run_cli(["show", "missing", "--json"], handler)
        self.assertEqual(rc_404, 1)
        self.assertIn("not found", json.loads(out_404)["error"])

    def test_live_http_server_login_and_status(self) -> None:
        data_dir = self.tmp_path / "srv_data"
        settings = PoolSettings(
            data_dir=data_dir,
            accounts_dir=data_dir / "accounts",
            drives_dir=data_dir / "drives",
            jobs_dir=data_dir / "jobs",
            db_path=data_dir / "pool.sqlite3",
            colab_bin="colab",
            api_key="live-smoke-key",
            default_cooldown_seconds=600,
            poll_interval_seconds=1.0,
            cleanup_orphans_on_startup=False,
            host="127.0.0.1",
            port=0,
        )
        app = create_app(settings)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{port}"
            for _ in range(50):
                try:
                    r = httpx.get(f"{url}/healthz", timeout=1.0)
                    if r.status_code == 200:
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                self.fail("Uvicorn smoke server did not become ready")

            rc_login, _, _ = self._run_cli(
                ["login", url, "--api-key", "live-smoke-key", "--profile", "live"]
            )
            self.assertEqual(rc_login, 0)

            rc_st, out_st, _ = self._run_cli(["status", "--profile", "live", "--json"])
            self.assertEqual(rc_st, 0)
            parsed = json.loads(out_st)
            self.assertEqual(parsed["stats"]["accounts"]["total"], 0)
        finally:
            server.should_exit = True
            thread.join(timeout=5.0)


if __name__ == "__main__":
    unittest.main()

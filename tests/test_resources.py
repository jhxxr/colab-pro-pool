"""Hardware capture and honest per-VM pricing attribution."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from colab_pool.api import create_app
from colab_pool.config import PoolSettings
from colab_pool.db import PoolDatabase
from colab_pool.remote_bootstrap import REMOTE_POOL_RUNNER_PY
from colab_pool.resources import build_resource_catalog, record_usage_observation, sanitize_runtime_info


def hardware(model="NVIDIA A100-SXM4-40GB", *, memory=40 * 1024, cpu_count=12):
    return sanitize_runtime_info(json.dumps({
        "cpu_count": cpu_count, "memory_total_bytes": 83 * 1024**3,
        "disk_total_bytes": 200 * 1024**3,
        "gpu_devices": [{"name": model, "memory_total_mib": memory}],
    }))


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="colab-resources-test-")
        self.addCleanup(self.tmp.cleanup)
        data = Path(self.tmp.name)
        self.settings = PoolSettings(
            data_dir=data, accounts_dir=data / "accounts", drives_dir=data / "drives",
            jobs_dir=data / "jobs", db_path=data / "pool.sqlite3", colab_bin="not-invoked",
            api_key="resources-key", default_cooldown_seconds=600, poll_interval_seconds=0.1,
            cleanup_orphans_on_startup=False, host="127.0.0.1", port=8080,
        )
        self.app = create_app(self.settings)
        self.addAsyncCleanup(self.app.state.account_usage.stop)
        self.addAsyncCleanup(self.app.state.account_auth.stop)
        self.pool, self.db = self.app.state.pool, self.app.state.db

    def client(self, authenticated=True):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
            headers={"Authorization": "Bearer resources-key"} if authenticated else {},
        )

    async def running_job(self, *, gpu="A100", actual="NVIDIA A100-SXM4-40GB"):
        self.pool.register_account_dir("research", "pro+")
        async with self.client() as client:
            job = (await client.post("/api/v1/jobs", json={"gpu": gpu, "name": "Experiment",
                                                         "script_content": "print(1)"})).json()
        self.db.acquire_idle_account(job["job_id"])
        self.db.update_job(
            job["job_id"], status="running", assigned_account="research",
            started_at=time.time() - 10, gpu_detected=actual,
            runtime_info_json=json.dumps(hardware(actual)),
        )
        return self.db.get_job(job["job_id"])

    def usage(self, *, count=1, checked_at=None, error=""):
        account = self.db.get_account("research")
        self.db.save_account_usage(
            "research", account["home_dir"], account["cu_generation"],
            checked_at=checked_at if checked_at is not None else time.time() + 1,
            balance=80, consumption_rate=13.75, active_assignments=count, error=error,
        )
        record_usage_observation(self.db, "research", account["cu_generation"])

    async def test_account_reports_detected_model_and_hardware_separately_from_request(self):
        job = await self.running_job(actual="NVIDIA L4")
        async with self.client() as client:
            response = await client.get("/api/v1/accounts")
        runtime = response.json()["accounts"][0]["runtime"]
        self.assertEqual(runtime["job_id"], job["job_id"])
        self.assertEqual(runtime["gpu"], "A100")
        self.assertEqual(runtime["gpu_detected"], "NVIDIA L4")
        self.assertEqual(runtime["hardware"]["cpu_count"], 12)
        self.assertEqual(runtime["hardware"]["gpu_devices"][0]["name"], "NVIDIA L4")
        self.db.update_job(job["job_id"], status="succeeded")
        self.assertIsNone(self.pool.list_accounts_enriched()[0]["runtime"])

    async def test_actual_model_price_is_saved_and_persists_after_job_finishes(self):
        job = await self.running_job(actual="NVIDIA L4")
        self.usage()
        catalog = build_resource_catalog(self.db)["resources"]
        self.assertEqual(next(item for item in catalog if item["id"] == "A100")["profiles"], [])
        profile = next(item for item in catalog if item["id"] == "L4")["profiles"][0]
        self.assertEqual(profile["consumption_rate_hourly"], 13.75)
        self.assertEqual(profile["runtime_info"]["cpu_count"], 12)
        self.db.update_job(job["job_id"], status="succeeded")
        self.db.release_account("research")
        self.db.delete_job(job["job_id"])
        persisted = build_resource_catalog(PoolDatabase(self.settings.db_path))
        self.assertEqual(next(item for item in persisted["resources"] if item["id"] == "L4")["profiles"][0]["consumption_rate_hourly"], 13.75)

    async def test_multi_vm_stale_failed_and_unconfirmed_rates_are_not_single_vm_prices(self):
        job = await self.running_job()
        detected_at = job["runtime_info"]["detected_at"]
        self.usage(count=2)
        self.usage(count=1, checked_at=detected_at - 1)
        self.usage(error="network unavailable")
        self.db.update_job(job["job_id"], status="provisioning")
        self.usage()
        self.assertEqual(self.db.list_resource_observations(), [])

    async def test_catalog_requires_api_key_and_contains_every_cli_resource_without_fake_prices(self):
        async with self.client(False) as client:
            self.assertEqual((await client.get("/api/v1/resources")).status_code, 401)
        async with self.client() as client:
            response = await client.get("/api/v1/resources")
        self.assertEqual(response.status_code, 200)
        items = response.json()["resources"]
        self.assertEqual({item["id"] for item in items}, {"CPU", "T4", "L4", "A100", "H100", "G4", "v5e1", "v6e1"})
        self.assertTrue(all(item["profiles"] == [] for item in items))
        self.assertEqual(next(item for item in items if item["id"] == "v6e1")["tpu"], "v6e1")
        self.assertEqual(self.db.count_jobs(), 0)

    async def test_profile_dedup_preserves_different_memory_shapes(self):
        for index, memory in enumerate((40 * 1024**3, 80 * 1024**3, 80 * 1024**3)):
            info = hardware()
            info["memory_total_bytes"] = memory
            self.db.save_resource_observation({
                "job_id": str(index), "resource_id": "A100", "high_mem": int(index > 0),
                "actual_model": "NVIDIA A100-SXM4-40GB", "runtime_info_json": json.dumps(info),
                "consumption_rate_hourly": index + 10, "observed_at": time.time() + index,
            })
        profiles = next(item for item in build_resource_catalog(self.db)["resources"] if item["id"] == "A100")["profiles"]
        self.assertEqual(len(profiles), 2)
        self.assertEqual(profiles[0]["consumption_rate_hourly"], 12)
        self.assertTrue(profiles[0]["high_mem"])
        self.assertFalse(profiles[1]["high_mem"])


class RuntimeProbeTests(unittest.TestCase):
    def test_remote_probe_reports_actual_gpu_and_cgroup_limited_memory(self):
        namespace = {"__name__": "test_remote_runner"}
        exec(compile(REMOTE_POOL_RUNNER_PY, "remote_runner.py", "exec"), namespace)
        def read(path, *args, **kwargs):
            if path.as_posix() == "/proc/meminfo":
                return f"MemTotal: {128 * 1024**2} kB\n"
            if str(path).endswith("memory.max"):
                return str(32 * 1024**3)
            raise OSError("not available")
        output = io.StringIO()
        with patch.object(Path, "read_text", read), patch.object(namespace["os"], "cpu_count", return_value=8), \
             patch.object(namespace["shutil"], "disk_usage", return_value=SimpleNamespace(total=200 * 1024**3)), \
             patch.object(namespace["shutil"], "which", return_value="mock-nvidia-smi"), \
             patch.object(namespace["subprocess"], "run", return_value=SimpleNamespace(returncode=0, stdout="NVIDIA A100-SXM4-40GB, 40960\n")), \
             contextlib.redirect_stdout(output):
            namespace["probe_runtime"]()
        lines = output.getvalue().splitlines()
        self.assertTrue(any("__COLAB_POOL_GPU__=NVIDIA A100-SXM4-40GB, 40960 MiB" == line for line in lines))
        raw = next(line.split("=", 1)[1] for line in lines if line.startswith("__COLAB_POOL_RUNTIME__="))
        runtime = sanitize_runtime_info(raw)
        self.assertEqual(runtime["memory_total_bytes"], 32 * 1024**3)
        self.assertEqual(runtime["disk_total_bytes"], 200 * 1024**3)
        self.assertEqual(runtime["gpu_devices"][0]["memory_total_mib"], 40960)

    def test_malformed_remote_metadata_cannot_add_secrets_or_unbounded_values(self):
        for raw in ("garbage", "[]", "null", "x" * 20000):
            self.assertEqual(sanitize_runtime_info(raw), {})
        result = sanitize_runtime_info(json.dumps({
            "cpu_count": -4, "memory_total_bytes": True, "disk_total_bytes": 2**90,
            "gpu_devices": [{"name": "NVIDIA T4", "memory_total_mib": "invalid"}],
            "access_token": "PRIVATE", "env": {"key": "PRIVATE"},
        }))
        self.assertNotIn("cpu_count", result)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(result["gpu_devices"], [{"name": "NVIDIA T4"}])

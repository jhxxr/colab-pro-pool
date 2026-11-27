"""MCP identity isolation, quota ledger, and runtime enforcement regressions."""

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from colab_pool.api import create_app
from colab_pool.db import PoolDatabase
from colab_pool.config import PoolSettings
from colab_pool.mcp_server import handle_jsonrpc_message


class McpKeyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.settings = PoolSettings(
            data_dir=root, accounts_dir=root / "accounts", drives_dir=root / "drives",
            jobs_dir=root / "jobs", db_path=root / "pool.sqlite3", colab_bin="colab",
            api_key="admin-test-secret", default_cooldown_seconds=60,
            poll_interval_seconds=0.1, cleanup_orphans_on_startup=False,
            host="127.0.0.1", port=8080,
        )
        self.app = create_app(self.settings)
        self.db = self.app.state.db
        self.keys = self.app.state.mcp_keys
        self.scheduler = self.app.state.scheduler
        self.addAsyncCleanup(self.scheduler.stop)
        self.addAsyncCleanup(self.app.state.account_usage.stop)
        self.addAsyncCleanup(self.app.state.account_auth.stop)
        self.key = self.keys.create("member", 10, ["T4", "CPU", "v5e1"])

    def client(self, token=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
            headers={"Authorization": f"Bearer {token if token is not None else self.settings.api_key}"},
        )

    async def submit(self, client, **kwargs):
        response = await client.post("/api/v1/jobs", json={"script_content": "print(1)", **kwargs})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_create_update_and_hash_only_storage(self):
        async with self.client() as client:
            response = await client.post("/api/v1/mcp-keys", json={
                "name": " student ", "cu_limit": 50.5, "allowed_models": ["t4", "t4", "V6E1"],
            })
            self.assertEqual(response.status_code, 201, response.text)
            key = response.json()
            self.assertEqual(key["name"], "student")
            self.assertEqual(key["allowed_models"], ["T4", "v6e1"])
            self.assertGreaterEqual(len(key["token"]), 40)
            row = self.keys.get(key["key_id"])
            self.assertEqual(row["token_hash"], hashlib.sha256(key["token"].encode()).hexdigest())
            listed = await client.get("/api/v1/mcp-keys")
            self.assertNotIn(key["token"], listed.text)
            self.assertNotIn("token_hash", listed.text)
            self.assertNotIn(key["token"].encode(), self.settings.db_path.read_bytes())
            updated = await client.patch(f'/api/v1/mcp-keys/{key["key_id"]}', json={
                "cu_limit": 100, "allowed_models": ["A100"], "enabled": False,
            })
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["cu_limit"], 100)
            self.assertNotIn("token_hash", updated.text)
            self.assertNotIn('"token":', updated.text)

    async def test_invalid_key_configuration_is_rejected(self):
        async with self.client() as client:
            for changes in ({"cu_limit": -1}, {"allowed_models": []}, {"allowed_models": ["any"]}, {"name": "  "}):
                response = await client.post("/api/v1/mcp-keys", json={
                    "name": "test", "cu_limit": 10, "allowed_models": ["T4"], **changes,
                })
                self.assertIn(response.status_code, {400, 422}, response.text)
            for field in ("cu_limit", "name", "allowed_models", "enabled"):
                response = await client.patch(f'/api/v1/mcp-keys/{self.key["key_id"]}', json={field: None})
                self.assertEqual(response.status_code, 400)
            for value in ("NaN", "Infinity", "-Infinity"):
                response = await client.post("/api/v1/mcp-keys", content='{"name":"bad","cu_limit":' + value + ',"allowed_models":["T4"]}', headers={"Content-Type": "application/json"})
                self.assertIn(response.status_code, {400, 422}, response.text)

    async def test_member_cannot_access_admin_endpoints(self):
        async with self.client(self.key["token"]) as client:
            for method, url, payload in [
                ("GET", "/api/v1/accounts", None), ("GET", "/api/v1/mcp-keys", None),
                ("GET", "/api/v1/proxies", None), ("GET", "/api/v1/stats", None),
                ("GET", "/api/v1/resources", None),
                ("POST", "/api/v1/pool/cleanup", None), ("POST", "/api/v1/pool/sync", None),
                ("POST", "/api/v1/accounts", {"name": "hack"}),
                ("POST", "/api/v1/account-auth", {"name": "hack"}),
                ("POST", "/api/v1/mcp-keys", {"name": "hack", "cu_limit": 10, "allowed_models": ["H100"]}),
                ("PATCH", f'/api/v1/mcp-keys/{self.key["key_id"]}', {"cu_limit": 100000}),
            ]:
                with self.subTest(url=url):
                    response = await client.request(method, url, **({"json": payload} if payload else {}))
                    self.assertEqual(response.status_code, 403, response.text)
            # The control panel login remains administrator-only.
            self.assertFalse((await client.get("/api/v1/auth/check")).json()["authenticated"])

    async def test_status_is_safe_and_works_after_quota_exhaustion(self):
        self.keys.update(self.key["key_id"], {"cu_limit": 0})
        self.app.state.pool.register_account_dir("private-account")
        async with self.client(self.key["token"]) as client:
            response = await client.get("/api/v1/mcp/status")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["key"]["cu_remaining"], 0)
            self.assertEqual(response.json()["allowed_models"], ["T4", "CPU", "v5e1"])
            self.assertNotIn("private-account", response.text)
            self.assertNotIn("token_hash", response.text)
            self.assertNotIn("home_dir", response.text)

    async def test_submit_retry_and_upload_enforce_models_and_quota(self):
        async with self.client(self.key["token"]) as client:
            job = await self.submit(client, gpu=" t4 ")
            self.assertEqual(job["gpu"], "T4")
            self.assertEqual(job["mcp_key_id"], self.key["key_id"])
            self.assertNotIn("cu_baseline_balance", job)
            self.assertNotIn("cu_meter_at", job)
            for spec in ({"gpu": "H100"}, {"gpu": "CPU", "tpu": "v6e1"}, {"drive_profile": "admin-drive"}, {"gpu": "v5e1"}, {"tpu": "T4"}):
                response = await client.post("/api/v1/jobs", json={"script_content": "pass", **spec})
                self.assertIn(response.status_code, {400, 403}, response.text)
            tpu = await self.submit(client, gpu="H100", tpu="V5E1")
            self.assertEqual((tpu["gpu"], tpu["tpu"]), ("CPU", "v5e1"))
            response = await client.post(f'/api/v1/jobs/{job["job_id"]}/retry', json={"gpu": "A100"})
            self.assertEqual(response.status_code, 403)
            response = await client.post("/api/v1/jobs/upload", files={"files": ("train.py", b"pass")}, data={"spec_json": json.dumps({"gpu": "H100"})})
            self.assertEqual(response.status_code, 403)
            response = await client.post("/api/v1/jobs/upload", files={"files": ("train.py", b"pass")}, data={"spec_json": json.dumps({"gpu": "CPU"})})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["mcp_key_id"], self.key["key_id"])
            self.keys.update(self.key["key_id"], {"cu_limit": 0})
            response = await client.post("/api/v1/jobs", json={"script_content": "pass"})
            self.assertEqual(response.status_code, 429)
            response = await client.post(f'/api/v1/jobs/{job["job_id"]}/retry', json={})
            self.assertEqual(response.status_code, 429)
            # Exhaustion does not prevent reading/cancelling one's own jobs.
            self.assertEqual((await client.get(f'/api/v1/jobs/{job["job_id"]}')).status_code, 200)
            self.assertEqual((await client.post(f'/api/v1/jobs/{job["job_id"]}/cancel')).status_code, 200)

    async def test_jobs_are_isolated_on_every_entrypoint(self):
        async with self.client() as admin, self.client(self.key["token"]) as member:
            foreign = await self.submit(admin)
            own = await self.submit(member)
            for method, suffix in [("GET", ""), ("GET", "/logs"), ("GET", "/logs/stream"),
                                   ("GET", "/workspace"), ("GET", "/artifacts"), ("GET", "/files/x"),
                                   ("POST", "/retry"), ("POST", "/cancel"), ("DELETE", "")]:
                response = await member.request(method, f'/api/v1/jobs/{foreign["job_id"]}{suffix}')
                self.assertEqual(response.status_code, 404, response.text)
            listed = (await member.get("/api/v1/jobs?limit=1")).json()
            self.assertEqual(listed["total"], 1)
            self.assertEqual(listed["jobs"][0]["job_id"], own["job_id"])
            second_key = self.keys.create("other", 10, ["T4"])
            async with self.client(second_key["token"]) as other:
                self.assertEqual((await other.get("/api/v1/jobs")).json()["total"], 0)
                self.assertEqual((await other.get(f'/api/v1/jobs/{own["job_id"]}')).status_code, 404)
            retry = await admin.post(f'/api/v1/jobs/{own["job_id"]}/retry', json={})
            self.assertEqual(retry.json()["mcp_key_id"], self.key["key_id"])
            self.keys.update(self.key["key_id"], {"cu_limit": 0})
            self.assertEqual((await admin.post(f'/api/v1/jobs/{own["job_id"]}/retry', json={})).status_code, 429)

    async def test_disabled_and_unknown_tokens_rejected_and_can_be_reenabled(self):
        self.keys.update(self.key["key_id"], {"enabled": False})
        for token in (self.key["token"], "cpool_unknown", "invalid", ""):
            async with self.client(token) as client:
                self.assertEqual((await client.get("/api/v1/jobs")).status_code, 401)
        self.keys.update(self.key["key_id"], {"enabled": True})
        async with self.client(self.key["token"]) as client:
            self.assertEqual((await client.get("/api/v1/jobs")).status_code, 200)
        async with self.client("") as client:
            self.assertEqual((await client.get("/api/v1/jobs", headers={"Authorization": "", "X-API-Key": self.key["token"]})).status_code, 200)

    async def test_removing_admin_env_never_makes_existing_keys_public(self):
        unconfigured = create_app(replace(self.settings, api_key=""))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=unconfigured), base_url="http://test") as client:
            self.assertEqual((await client.get("/api/v1/accounts")).status_code, 401)
            self.assertEqual((await client.get("/api/v1/jobs")).status_code, 401)
            self.assertEqual((await client.get("/api/v1/auth/check")).json(), {"auth_required": True, "authenticated": False})
            response = await client.get("/api/v1/accounts", headers={"Authorization": f'Bearer {self.key["token"]}'})
            self.assertEqual(response.status_code, 403)

    async def test_create_requires_configured_admin_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = create_app(replace(self.settings, api_key="", data_dir=root, accounts_dir=root / "accounts",
                                     drives_dir=root / "drives", jobs_dir=root / "jobs", db_path=root / "db"))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/api/v1/mcp-keys", json={"name": "bad", "cu_limit": 10, "allowed_models": ["T4"]})
                self.assertEqual(response.status_code, 409)

    async def test_ledger_is_idempotent_persistent_and_survives_job_deletion(self):
        async with self.client(self.key["token"]) as client:
            job = await self.submit(client)
        jid = job["job_id"]
        self.keys.begin_meter(jid, 100, now=1000)
        self.keys.meter(jid, now=1001, rate=360)
        self.keys.meter(jid, now=1002)
        self.keys.meter(jid, now=1002)
        self.assertAlmostEqual(self.keys.get(self.key["key_id"])["cu_used"], 0.2)
        self.keys.meter(jid, now=1002, balance=99.7, rate=360)
        self.keys.meter(jid, now=1002, balance=99.7)
        self.assertAlmostEqual(self.db.get_job(jid)["cu_used"], 0.3)
        self.keys.meter(jid, now=1003, finish=True)
        self.keys.meter(jid, now=5000)
        self.assertAlmostEqual(self.db.get_job(jid)["cu_used"], 0.4)
        self.keys.meter(jid, now=5000, balance=99.5)
        self.assertAlmostEqual(self.db.get_job(jid)["cu_used"], 0.5)
        self.assertIsNone(self.db.get_job(jid)["cu_meter_at"])
        self.keys.meter(jid, now=5001, balance=110)
        self.assertAlmostEqual(self.keys.get(self.key["key_id"])["cu_used"], 0.5)
        self.db.delete_job(jid)
        restarted = create_app(self.settings)
        self.assertAlmostEqual(restarted.state.mcp_keys.get(self.key["key_id"])["cu_used"], 0.5)
        self.assertIsNotNone(restarted.state.mcp_keys.authenticate(self.key["token"]))

    async def test_concurrent_jobs_charge_shared_key_and_distinct_keys_independently(self):
        async with self.client(self.key["token"]) as client:
            jobs = [await self.submit(client) for _ in range(2)]
        for job in jobs:
            self.keys.begin_meter(job["job_id"], 100, now=1000)
        for job in jobs:
            self.keys.meter(job["job_id"], now=1001, rate=360)
        self.assertAlmostEqual(self.keys.get(self.key["key_id"])["cu_used"], 0.2)
        other = self.keys.create("other", 20, ["CPU"])
        self.assertEqual(other["cu_used"], 0)

    async def test_watchdog_ticks_do_not_erase_time_before_first_rate(self):
        async with self.client(self.key["token"]) as client:
            job = await self.submit(client)
        jid = job["job_id"]
        self.keys.begin_meter(jid, 100, now=1000)
        self.keys.meter(jid, now=1001)
        self.keys.meter(jid, now=1002)
        self.keys.meter(jid, now=1003, rate=360)
        self.assertAlmostEqual(self.db.get_job(jid)["cu_used"], 0.3)

    async def test_restart_settles_active_meter_and_fails_stale_job(self):
        async with self.client(self.key["token"]) as client:
            job = await self.submit(client)
        jid = job["job_id"]
        self.db.update_job(jid, status="running")
        self.keys.begin_meter(jid, 100, now=1000)
        self.keys.meter(jid, now=1001, rate=360)
        with patch("colab_pool.db.time.time", return_value=1002):
            self.db.reset_stale_busy_accounts_on_boot()
        self.assertAlmostEqual(self.keys.get(self.key["key_id"])["cu_used"], 0.2)
        self.assertEqual(self.db.get_job(jid)["status"], "failed")
        self.assertIsNone(self.db.get_job(jid)["cu_meter_at"])
        self.keys.begin_meter(jid, 100, now=1002)
        self.db.reset_stale_busy_accounts_on_boot()
        self.assertFalse(self.keys.get(self.key["key_id"])["enabled"])

    async def test_database_rechecks_policy_at_insert(self):
        async with self.client(self.key["token"]) as client:
            original = self.db.create_job

            def revoke_then_insert(record):
                self.keys.update(self.key["key_id"], {"enabled": False})
                return original(record)

            with patch.object(self.db, "create_job", side_effect=revoke_then_insert):
                response = await client.post("/api/v1/jobs", json={"script_content": "pass"})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(list(self.settings.jobs_dir.iterdir()), [])

    async def test_queued_job_rechecks_policy_before_provisioning(self):
        async with self.client(self.key["token"]) as client:
            job = await self.submit(client)
        self.app.state.pool.register_account_dir("pro01")
        self.keys.update(self.key["key_id"], {"allowed_models": ["CPU"]})
        self.scheduler._try_schedule_queued_jobs()
        self.assertEqual(self.db.get_job(job["job_id"])["status"], "failed")
        self.assertEqual(self.scheduler._active_tasks, {})
        self.assertEqual(self.db.get_account("pro01")["status"], "idle")

    async def test_runtime_meters_official_usage_and_tears_down(self):
        self.app.state.pool.register_account_dir("pro01")
        async with self.client(self.key["token"]) as client:
            job = await self.submit(client)
        calls = []

        async def cli(home, args, *a, **kw):
            calls.append(args)
            if args[0] == "exec":
                return 0, "__COLAB_POOL_EXIT_CODE__=0\n"
            return (1, "no archive") if args[0] == "download" else (0, "ok")

        with patch.object(self.scheduler, "_read_key_usage", side_effect=[(100, 0, 0), (99.99, 1, 1), (99.98, 0, 0)]), patch.object(self.scheduler, "_run_cli_logged", side_effect=cli):
            self.scheduler._try_schedule_queued_jobs()
            await self.scheduler._active_tasks[job["job_id"]]
        finished = self.db.get_job(job["job_id"])
        self.assertEqual(finished["status"], "succeeded", finished["error_message"])
        self.assertAlmostEqual(finished["cu_used"], 0.02, places=4)
        self.assertIsNone(finished["cu_meter_at"])
        self.assertTrue(any(args[:2] == ["stop", "-s"] for args in calls))

    async def test_unconfirmed_or_shared_usage_stops_without_running_workload(self):
        self.app.state.pool.register_account_dir("pro01")
        for initial, allocated in [((100, 1, 1), (100, 1, 1)), ((100, 0, 0), (100, 10, 2)), ((100, 0, 0), (100, 0, 0))]:
            async with self.client(self.key["token"]) as client:
                job = await self.submit(client)
            calls = []

            async def cli(home, args, *a, **kw):
                calls.append(args[0])
                return 0, "ok"

            with patch.object(self.scheduler, "_read_key_usage", side_effect=[initial, allocated, (100, 0, 0)]), patch.object(self.scheduler, "_run_cli_logged", side_effect=cli):
                self.scheduler._try_schedule_queued_jobs()
                await self.scheduler._active_tasks[job["job_id"]]
            self.assertEqual(self.db.get_job(job["job_id"])["status"], "failed")
            self.assertNotIn("exec", calls)
            self.assertEqual(self.db.get_account("pro01")["status"], "idle")

    async def test_guard_stops_silent_work_on_revocation_model_change_and_exhaustion(self):
        self.app.state.pool.register_account_dir("pro01")
        self.scheduler.key_watch_interval = 0.005
        self.scheduler.key_usage_interval = 100
        for patch_key in ({"enabled": False}, {"cu_limit": 0}, {"allowed_models": ["CPU"]}):
            self.keys.update(self.key["key_id"], {"enabled": True, "cu_limit": 100, "allowed_models": ["T4"]})
            async with self.client(self.key["token"]) as client:
                job = await self.submit(client)
            running = asyncio.Event()
            stops = []

            async def cli(home, args, *a, **kw):
                if args[0] == "exec":
                    running.set()
                    await asyncio.Event().wait()
                if args[:2] == ["stop", "-s"]:
                    stops.append(args)
                return 0, "ok"

            async def usage(home):
                current = self.db.get_job(job["job_id"])
                if current["cu_meter_at"] is None or stops:
                    return 100, 0, 0
                return 100, 1, 1

            with patch.object(self.scheduler, "_read_key_usage", side_effect=usage), patch.object(self.scheduler, "_run_cli_logged", side_effect=cli):
                self.scheduler._try_schedule_queued_jobs()
                task = self.scheduler._active_tasks[job["job_id"]]
                await asyncio.wait_for(running.wait(), timeout=2)
                self.keys.update(self.key["key_id"], patch_key)
                await asyncio.wait_for(task, timeout=2)
            result = self.db.get_job(job["job_id"])
            self.assertEqual(result["status"], "failed", result)
            self.assertTrue(stops)
            self.assertEqual(self.db.get_account("pro01")["status"], "idle")

    async def test_runtime_quota_runs_out_without_cli_output(self):
        self.app.state.pool.register_account_dir("pro01")
        self.keys.update(self.key["key_id"], {"cu_limit": 0.02})
        self.scheduler.key_watch_interval = 0.005
        async with self.client(self.key["token"]) as client:
            job = await self.submit(client)

        async def cli(home, args, *a, **kw):
            if args[0] == "exec":
                await asyncio.Event().wait()
            return 0, "ok"

        async def usage(home):
            current = self.db.get_job(job["job_id"])
            return (100, 3600, 1) if current["cu_meter_at"] is not None else (100, 0, 0)

        with patch.object(self.scheduler, "_read_key_usage", side_effect=usage), patch.object(self.scheduler, "_run_cli_logged", side_effect=cli):
            self.scheduler._try_schedule_queued_jobs()
            await asyncio.wait_for(self.scheduler._active_tasks[job["job_id"]], timeout=2)
        finished = self.db.get_job(job["job_id"])
        self.assertEqual(finished["status"], "failed")
        self.assertIn("额度已耗尽", finished["error_message"])
        self.assertGreaterEqual(finished["cu_used"], 0.02)

    async def test_cli_usage_query_failure_prevents_allocation(self):
        self.app.state.pool.register_account_dir("pro01")
        async with self.client(self.key["token"]) as client:
            job = await self.submit(client)
        calls = []

        async def cli(home, args, *a, **kw):
            calls.append(args[0])
            return 0, "ok"

        with patch.object(self.scheduler, "_read_key_usage", side_effect=ValueError("usage unavailable")), patch.object(self.scheduler, "_run_cli_logged", side_effect=cli):
            self.scheduler._try_schedule_queued_jobs()
            await self.scheduler._active_tasks[job["job_id"]]
        self.assertNotIn("new", calls)
        self.assertEqual(self.db.get_job(job["job_id"])["status"], "failed")

    async def test_failed_stop_quarantines_key_and_account(self):
        self.app.state.pool.register_account_dir("pro01")
        async with self.client(self.key["token"]) as client:
            job = await self.submit(client)

        async def cli(home, args, *a, **kw):
            if args[0] == "exec":
                return 0, "__COLAB_POOL_EXIT_CODE__=0\n"
            if args[:2] == ["stop", "-s"]:
                return 1, "network failed"
            return (1, "no archive") if args[0] == "download" else (0, "ok")

        with patch.object(self.scheduler, "_read_key_usage", side_effect=[(100, 0, 0), (100, 1, 1), (100, 1, 1)]), patch.object(self.scheduler, "_run_cli_logged", side_effect=cli):
            self.scheduler._try_schedule_queued_jobs()
            await self.scheduler._active_tasks[job["job_id"]]
        self.assertEqual(self.db.get_job(job["job_id"])["status"], "failed")
        self.assertIn("RUNTIME_STOP_FAILED", self.db.get_job(job["job_id"])["error_message"])
        self.assertFalse(self.keys.get(self.key["key_id"])["enabled"])
        self.assertFalse(self.db.get_account("pro01")["enabled"])
        self.app.state.pool.sync_accounts_from_disk()
        self.assertFalse(self.db.get_account("pro01")["enabled"])

    async def test_guard_query_failure_cancels_running_work(self):
        self.app.state.pool.register_account_dir("pro01")
        self.scheduler.key_watch_interval = 0.005
        self.scheduler.key_usage_interval = 0
        async with self.client(self.key["token"]) as client:
            job = await self.submit(client)
        count = 0

        async def usage(home):
            nonlocal count
            count += 1
            if count == 1:
                return 100, 0, 0
            if count == 2:
                return 100, 1, 1
            raise ValueError("CU connection lost")

        async def cli(home, args, *a, **kw):
            if args[0] == "exec":
                await asyncio.Event().wait()
            return 0, "ok"

        with patch.object(self.scheduler, "_read_key_usage", side_effect=usage), patch.object(self.scheduler, "_run_cli_logged", side_effect=cli):
            self.scheduler._try_schedule_queued_jobs()
            await asyncio.wait_for(self.scheduler._active_tasks[job["job_id"]], timeout=2)
        result = self.db.get_job(job["job_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("CU connection lost", result["error_message"])
        self.assertEqual(self.db.get_account("pro01")["status"], "idle")

    def test_mcp_errors_surface_quota_detail(self):
        request = httpx.Request("POST", "http://test/api/v1/jobs")
        response = httpx.Response(429, json={"detail": "MCP 密钥的 CU 额度已耗尽。"}, request=request)
        error = httpx.HTTPStatusError("HTTP 429", request=request, response=response)
        with patch("colab_pool.mcp_server._handle_tool_call", side_effect=error):
            result = handle_jsonrpc_message({"id": 1, "method": "tools/call", "params": {"name": "submit_colab_job", "arguments": {}}})
        self.assertTrue(result["result"]["isError"])
        self.assertIn("CU 额度已耗尽", result["result"]["content"][0]["text"])

    def test_older_database_is_migrated_without_losing_jobs(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.sqlite3"
            conn = sqlite3.connect(path)
            try:
                # A copy of today's schema minus the new key/meter columns.
                sql = self.settings.db_path
                source = sqlite3.connect(str(sql))
                create = source.execute("SELECT sql FROM sqlite_master WHERE name = 'jobs'").fetchone()[0]
                source.close()
                for column, definition in [("mcp_key_id", "TEXT"), ("cu_used", "REAL NOT NULL DEFAULT 0"),
                                           ("cu_meter_at", "REAL"), ("cu_rate", "REAL"),
                                           ("cu_baseline_balance", "REAL"), ("cu_session_used", "REAL NOT NULL DEFAULT 0")]:
                    create = create.replace(f", {column} {definition}", "")
                conn.execute(create)
                columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
                self.assertNotIn("mcp_key_id", columns)
                conn.execute("INSERT INTO jobs (job_id, name, status, gpu, entrypoint, workspace_dir, log_path, created_at) VALUES ('old-job', 'preserved', 'queued', 'T4', 'train.py', 'workspace', 'job.log', 1000)")
                conn.commit()
            finally:
                conn.close()
            migrated = PoolDatabase(path)
            with migrated._conn() as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            self.assertIn("mcp_key_id", columns)
            self.assertIn("cu_used", columns)
            self.assertEqual(migrated.get_job("old-job")["name"], "preserved")
            self.assertIsNone(migrated.get_job("old-job")["mcp_key_id"])

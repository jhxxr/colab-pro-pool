"""CU queries exercise a real isolated child CLI, cache, and credential lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from colab_pool.account_usage import AccountUsageManager, UsageQueryError, parse_usage_output
from colab_pool.api import create_app
from colab_pool.config import PoolSettings
from colab_pool.db import PoolDatabase
from colab_pool.oauth_store import write_oauth_token


MOCK_USAGE_CLI = r'''#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

assert sys.argv[1:] == ["--auth", "oauth2", "usage"]
assert sys.stdin.read() == ""  # No interactive OAuth prompt can wait for input.
home = Path(os.environ["COLAB_POOL_ACCOUNT_HOME"])
assert os.environ["HOME"] == os.environ["USERPROFILE"] == str(home)
assert os.environ["XDG_CONFIG_HOME"] == str(home / ".config")
assert os.environ["PYTHON_KEYRING_BACKEND"] == "colab_pool.keyring_backend.AccountKeyring"
with (home / "queries.log").open("a") as stream:
    stream.write("usage\n")
mode_path = home / "usage-mode.txt"
mode = mode_path.read_text().strip() if mode_path.exists() else "ok"
if mode == "slow":
    (home / "query.pid").write_text(str(os.getpid()))
    time.sleep(30)
if mode == "error":
    print("Warning: invalid_grant access_token=DO_NOT_LEAK", file=sys.stderr)
    sys.exit(1)
if mode == "unsupported":
    print("No such command 'usage'.", file=sys.stderr)
    sys.exit(2)
if mode == "malformed":
    print("Warning: access_token=DO_NOT_LEAK")
    sys.exit(0)
token_path = home / ".config" / "colab-cli" / "pool-oauth.json"
if not token_path.exists():
    token_path = token_path.with_name("token.json")
token = json.loads(token_path.read_text())
print("[colab] A new version of Colab CLI is available: 99.0")
print(f"Current balance: {token['test_balance']:.2f} compute units")
print(f"Usage rate: {token['test_rate']:.2f}/hr")
print(f"Active assignments: {token['test_assignments']}")
'''


def install_usage_cli(root: Path) -> str:
    script = root / "usage_cli.py"
    script.write_text(MOCK_USAGE_CLI, encoding="utf-8")
    if os.name == "nt":
        launcher = root / "usage_cli.cmd"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return str(launcher)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


class AccountUsageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="colab-usage-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        data = self.root / "data"
        self.settings = PoolSettings(
            data_dir=data, accounts_dir=data / "accounts", drives_dir=data / "drives",
            jobs_dir=data / "jobs", db_path=data / "pool.sqlite3",
            colab_bin=install_usage_cli(self.root), api_key="usage-secret",
            default_cooldown_seconds=600, poll_interval_seconds=0.1,
            cleanup_orphans_on_startup=False, host="127.0.0.1", port=8080,
        )
        self.app = create_app(self.settings)
        self.pool = self.app.state.pool
        self.db = self.app.state.db
        self.usage = self.app.state.account_usage
        self.addAsyncCleanup(self.usage.stop)
        self.addAsyncCleanup(self.app.state.account_auth.stop)

    def account(self, name="research", *, balance=80.25, rate=4.5, count=1, legacy=False):
        account = self.pool.register_account_dir(name)
        token = json.dumps({
            "refresh_token": f"PRIVATE-{name}", "client_id": "mock-client",
            "client_secret": "PRIVATE-secret", "test_balance": balance,
            "test_rate": rate, "test_assignments": count,
        })
        home = Path(account["home_dir"])
        if legacy:
            (home / ".config" / "colab-cli" / "token.json").write_text(token)
        else:
            write_oauth_token(home, token)
        return home

    def client(self, authenticated=True):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
            headers={"Authorization": "Bearer usage-secret"} if authenticated else {},
        )

    async def settle(self):
        tasks = list(self.usage._tasks.values())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=6)
        await asyncio.sleep(0)

    def result(self, name="research"):
        return self.usage.enrich_account(self.db.get_account(name))["usage"]

    def age_cache(self, name="research"):
        with closing(sqlite3.connect(self.settings.db_path)) as conn, conn:
            conn.execute("UPDATE accounts SET cu_checked_at = 1 WHERE name = ?", (name,))

    async def test_api_returns_fast_and_keeps_busy_and_disabled_accounts_isolated(self):
        first = self.account(balance=82.15, rate=13.75, count=2)
        second = self.account("backup", balance=0, rate=0, count=0, legacy=True)
        self.db.acquire_idle_account("running-job", exclude_accounts=["backup"])
        self.pool.update_account("backup", enabled=False)
        async with self.client() as client:
            response = await client.get("/api/v1/accounts")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(all(a["usage"]["refreshing"] for a in response.json()["accounts"]))
            self.assertNotIn("PRIVATE", response.text)
            await self.settle()
            self.assertEqual(self.result()["remaining_cu"], 82.15)
            self.assertEqual(self.result()["consumption_rate_hourly"], 13.75)
            self.assertEqual(self.result()["active_assignments"], 2)
            self.assertFalse(self.result()["stale"])
            self.assertEqual(self.result("backup")["remaining_cu"], 0)
            self.assertEqual(self.result("backup")["consumption_rate_hourly"], 0)
            self.assertEqual(self.db.get_account("research")["status"], "busy")
            self.assertEqual(self.db.get_account("research")["current_job_id"], "running-job")
            self.assertFalse(self.db.get_account("backup")["enabled"])
            for _ in range(3):
                await client.get("/api/v1/accounts")
                await client.post("/api/v1/pool/usage/refresh")
            await self.settle()
            for home in (first, second):
                self.assertEqual((home / "queries.log").read_text().splitlines(), ["usage"])

    async def test_failures_keep_the_last_good_balance_rate_and_success_timestamp(self):
        home = self.account()
        self.usage.refresh("research")
        await self.settle()
        original = self.result()
        (home / "usage-mode.txt").write_text("error")
        self.age_cache()
        self.usage.refresh("research", force=True)
        await self.settle()
        current = self.result()
        for key in ("remaining_cu", "consumption_rate_hourly", "active_assignments", "updated_at"):
            self.assertEqual(current[key], original[key])
        self.assertTrue(current["stale"])
        self.assertIn("授权", current["error"])
        self.assertNotIn("DO_NOT_LEAK", json.dumps(current))
        self.assertGreaterEqual(current["checked_at"], original["updated_at"])
        self.assertEqual(self.db.get_account("research")["last_error"], "")
        (home / "usage-mode.txt").write_text("ok")
        self.age_cache()
        self.usage.refresh("research")
        await self.settle()
        self.assertFalse(self.result()["stale"])
        self.assertEqual(self.result()["error"], "")

    async def test_missing_invalid_and_maintenance_credentials_never_start_cli(self):
        home = Path(self.pool.register_account_dir("research")["home_dir"])
        self.usage.refresh("research")
        await self.settle()
        self.assertIsNone(self.result()["remaining_cu"])
        self.assertIsNone(self.result()["consumption_rate_hourly"])
        self.account()
        self.assertTrue(self.db.acquire_account_for_auth("research"))
        self.usage.refresh("research", force=True)
        self.assertFalse(self.result()["refreshing"])
        self.db.release_account_from_cleanup("research")
        write_oauth_token(home, '{"token":"PRIVATE-invalid"}')
        self.usage.refresh("research", force=True)
        await self.settle()
        self.assertIn("授权", self.result()["error"])
        self.assertFalse((home / "queries.log").exists())
        self.assertIsNone(self.result()["remaining_cu"])

    async def test_concurrent_queries_deduplicate_and_old_results_cannot_repopulate_reauthorized_account(self):
        self.account()
        started, finish = asyncio.Event(), asyncio.Event()

        async def query(home):
            started.set()
            await finish.wait()
            return "Current balance: 99.00 compute units\nUsage rate: 9.00/hr\nActive assignments: 1"

        with patch.object(self.usage, "_query_cli", side_effect=query) as mocked:
            self.usage.refresh("research")
            await started.wait()
            for _ in range(5):
                self.usage.refresh("research", force=True)
            self.db.invalidate_account_usage("research")
            finish.set()
            await self.settle()
            self.assertEqual(mocked.call_count, 1)
        self.assertIsNone(self.result()["remaining_cu"])
        self.assertIsNone(self.result()["consumption_rate_hourly"])

    async def test_recreated_slot_rejects_result_from_deleted_slot(self):
        self.account()
        old = self.db.get_account("research")
        self.assertTrue(self.pool.delete_account("research"))
        self.pool.register_account_dir("research")
        self.db.save_account_usage(
            "research", old["home_dir"], old["cu_generation"], checked_at=time.time(),
            balance=99, consumption_rate=8, active_assignments=1,
        )
        self.assertIsNone(self.result()["remaining_cu"])

    async def test_cache_survives_restart_and_ages_into_stale(self):
        self.account()
        self.usage.refresh("research")
        await self.settle()
        new_app = create_app(self.settings)
        self.addAsyncCleanup(new_app.state.account_usage.stop)
        self.addAsyncCleanup(new_app.state.account_auth.stop)
        cached = new_app.state.account_usage.list_accounts()[0]["usage"]
        self.assertEqual(cached["remaining_cu"], 80.25)
        self.assertFalse(cached["stale"])
        new_app.state.account_usage.refresh_all()
        self.assertEqual(new_app.state.account_usage._tasks, {})
        with patch("colab_pool.pool_manager.time.time", return_value=cached["updated_at"] + 61):
            self.assertTrue(new_app.state.account_usage.list_accounts()[0]["usage"]["stale"])

    async def test_api_key_is_required_for_refresh_and_unknown_slot_is_404(self):
        self.account()
        async with self.client(False) as client:
            for endpoint in ("/api/v1/accounts/research/usage/refresh", "/api/v1/pool/usage/refresh"):
                self.assertEqual((await client.post(endpoint)).status_code, 401)
        async with self.client() as client:
            self.assertEqual((await client.post("/api/v1/accounts/missing/usage/refresh")).status_code, 404)
            response = await client.post("/api/v1/accounts/research/usage/refresh")
            self.assertEqual(response.status_code, 202)
            self.assertTrue(response.json()["usage"]["refreshing"])
            self.assertNotIn("cu_generation", response.json())
            await self.settle()

    async def test_authorization_cancels_query_before_credentials_can_change(self):
        self.account()
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def query(home):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        def auth_start(name, tier):
            self.assertTrue(cancelled.is_set())
            self.assertNotIn(name, self.usage._tasks)
            return {"status": "starting"}

        with patch.object(self.usage, "_query_cli", side_effect=query), patch.object(
            self.app.state.account_auth, "start", side_effect=auth_start
        ):
            self.usage.refresh("research")
            await started.wait()
            async with self.client() as client:
                response = await client.post("/api/v1/account-auth", json={"name": "research"})
                self.assertEqual(response.status_code, 202)
        self.assertIsNone(self.result()["remaining_cu"])

    async def test_timeout_and_shutdown_reap_slow_cli(self):
        home = self.account()
        (home / "usage-mode.txt").write_text("slow")
        short = AccountUsageManager(self.pool)
        short.settings = replace(self.settings, usage_timeout_seconds=0.5)
        self.addAsyncCleanup(short.stop)
        start = time.monotonic()
        short.refresh("research")
        await asyncio.wait_for(asyncio.gather(*list(short._tasks.values())), timeout=4)
        self.assertLess(time.monotonic() - start, 4)
        self.assertIn("超时", self.result()["error"])
        self.assertIsNone(self.result()["remaining_cu"])
        self.age_cache()
        self.usage.refresh("research")
        deadline = time.monotonic() + 3
        while not (home / "queries.log").exists() or len((home / "queries.log").read_text().splitlines()) < 2:
            self.assertLess(time.monotonic(), deadline)
            await asyncio.sleep(0.02)
        await asyncio.wait_for(self.usage.stop(), timeout=3)
        self.assertEqual(self.usage._tasks, {})
        self.assertEqual(self.db.get_account("research")["status"], "idle")

    async def test_unsupported_and_malformed_output_stays_unknown(self):
        home = self.account()
        for mode, message in (("unsupported", "升级"), ("malformed", "识别")):
            with self.subTest(mode=mode):
                (home / "usage-mode.txt").write_text(mode)
                self.age_cache()
                self.usage.refresh("research")
                await self.settle()
                result = self.result()
                self.assertIsNone(result["remaining_cu"])
                self.assertIsNone(result["consumption_rate_hourly"])
                self.assertIn(message, result["error"])
                self.assertNotIn("DO_NOT_LEAK", json.dumps(result))

    async def test_new_machine_refreshes_recent_idle_rate_and_collects_its_price(self):
        home = self.account(rate=0, count=0)
        await self.usage.start()
        await asyncio.sleep(0)
        await self.settle()
        self.assertEqual(self.result()["consumption_rate_hourly"], 0)
        async with self.client() as client:
            job = (await client.post("/api/v1/jobs", json={"gpu": "A100", "script_content": "print(1)"})).json()
        self.db.acquire_idle_account(job["job_id"])
        runtime = {"cpu_count": 12, "memory_total_bytes": 83 * 1024**3,
                   "gpu_devices": [{"name": "NVIDIA A100-SXM4-40GB", "memory_total_mib": 40960}],
                   "detected_at": time.time()}
        self.db.update_job(job["job_id"], status="running", assigned_account="research",
                           runtime_info_json=json.dumps(runtime))
        token_path = home / ".config" / "colab-cli" / "pool-oauth.json"
        token = json.loads(token_path.read_text())
        token.update(test_rate=13.75, test_assignments=1)
        write_oauth_token(home, json.dumps(token))
        # A probe newer than the idle sample bypasses the one-minute cache.
        self.usage.refresh_all()
        await self.settle()
        self.assertEqual(self.result()["consumption_rate_hourly"], 13.75)
        profile = self.db.list_resource_observations()[0]
        self.assertEqual(profile["resource_id"], "A100")
        self.assertEqual(profile["consumption_rate_hourly"], 13.75)


class UsageFormatAndMigrationTests(unittest.TestCase):
    def test_official_format_accepts_zero_negative_balance_and_upgrade_banner(self):
        self.assertEqual(parse_usage_output(
            "[colab] update available\r\n\x1b[32mCurrent balance: 0.00 compute units\x1b[0m\r\n"
            "Usage rate: 0.00/hr\r\nActive assignments: 0\r\n"
        ), (0, 0, 0))
        self.assertEqual(parse_usage_output(
            "Current balance: -0.12 compute units\nUsage rate: 13.75/hr\nActive assignments: 2"
        ), (-0.12, 13.75, 2))

    def test_incomplete_duplicate_and_non_finite_values_are_rejected(self):
        valid = "Current balance: 8.00 compute units\nUsage rate: 1.00/hr\nActive assignments: 1"
        for output in ("", valid.splitlines()[0], valid + "\n" + valid,
                       valid.replace("8.00", "NaN"), valid.replace("1.00/hr", "-1.00/hr")):
            with self.subTest(output=output), self.assertRaises(UsageQueryError):
                parse_usage_output(output)

    def test_existing_sqlite_database_gains_nullable_usage_columns_without_losing_accounts(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "old.sqlite3"
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("CREATE TABLE accounts (name TEXT PRIMARY KEY, home_dir TEXT, tier TEXT, "
                             "status TEXT, current_job_id TEXT, cooldown_until REAL DEFAULT 0, "
                             "last_used_at REAL DEFAULT 0, total_runs INTEGER DEFAULT 0, "
                             "total_failures INTEGER DEFAULT 0, last_error TEXT DEFAULT '', enabled INTEGER)")
                conn.execute("INSERT INTO accounts (name, home_dir, tier, status, enabled) "
                             "VALUES ('old', '/accounts/old', 'pro', 'idle', 1)")
            migrated = PoolDatabase(path).get_account("old")
            self.assertEqual(migrated["tier"], "pro")
            self.assertIsNone(migrated["cu_balance"])
            self.assertIsNone(migrated["cu_consumption_rate"])
            self.assertEqual(migrated["cu_error"], "")

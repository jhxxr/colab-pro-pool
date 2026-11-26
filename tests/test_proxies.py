from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

import httpx

from colab_pool.api import create_app
from colab_pool.config import PoolSettings
from colab_pool.db import PoolDatabase
from colab_pool.proxy_config import redact_proxy_credentials


class ProxyManagementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="colab-proxy-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        data = Path(self.tmp.name) / "data"
        self.auth_cli = self._install_auth_cli()
        self.settings = PoolSettings(
            data_dir=data, accounts_dir=data / "accounts", drives_dir=data / "drives",
            jobs_dir=data / "jobs", db_path=data / "pool.sqlite3",
            colab_bin=self.auth_cli, api_key="proxy-test-key", cleanup_orphans_on_startup=False,
            default_cooldown_seconds=600, poll_interval_seconds=0.1,
            host="127.0.0.1", port=8080,
        )
        self.app = create_app(self.settings)
        self.addAsyncCleanup(self.app.state.account_usage.stop)
        self.addAsyncCleanup(self.app.state.account_auth.stop)
        self.addAsyncCleanup(self.app.state.scheduler.stop)
        self.client_headers = {"Authorization": "Bearer proxy-test-key"}

    def _install_auth_cli(self) -> str:
        script = self.root / "mock_auth_cli.py"
        capture = json.dumps(str(self.root / "captured-proxy.json"))
        script.write_text(
            "import json, os, sys\n"
            "from urllib.parse import urlencode\n"
            f"open({capture}, 'w', encoding='utf-8').write(json.dumps({{k: os.environ.get(k, '') for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')}}))\n"
            "print('https://accounts.google.com/o/oauth2/auth?' + urlencode({'redirect_uri': 'https://sdk.cloud.google.com/applicationdefaultauthcode.html', 'state': 'mock-state'}), flush=True)\n"
            "sys.stdin.readline()\n",
            encoding="utf-8",
        )
        if os.name == "nt":
            launcher = self.root / "mock_auth_cli.cmd"
            launcher.write_text(
                f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
            )
            return str(launcher)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return str(script)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
            headers=self.client_headers,
        )

    async def test_proxy_secrets_are_redacted_and_account_environment_is_scoped(self):
        async with self.client() as client:
            created = await client.post("/api/v1/proxies", json={
                "name": "Tokyo",
                "url": "http://proxy-user:s3cret-pass@proxy.example:3128",
            })
            self.assertEqual(created.status_code, 201, created.text)
            proxy = created.json()
            self.assertEqual(proxy["endpoint"], "http://proxy.example:3128")
            self.assertTrue(proxy["has_auth"])
            self.assertNotIn("proxy-user", created.text)
            self.assertNotIn("s3cret-pass", created.text)

            listed = await client.get("/api/v1/proxies")
            self.assertEqual(listed.status_code, 200)
            self.assertNotIn("s3cret-pass", listed.text)

            account = self.app.state.pool.register_account_dir("research")
            assigned = await client.patch(
                "/api/v1/accounts/research", json={"proxy_id": proxy["proxy_id"]}
            )
            self.assertEqual(assigned.status_code, 200, assigned.text)
            self.assertEqual(assigned.json()["proxy_id"], proxy["proxy_id"])
            self.assertEqual(assigned.json()["proxy_name"], "Tokyo")
            self.assertNotIn("s3cret-pass", assigned.text)

            env = self.app.state.pool.build_cli_env(account["home_dir"])
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                self.assertEqual(env[key], "http://proxy-user:s3cret-pass@proxy.example:3128")

            deleting_assigned = await client.delete(f"/api/v1/proxies/{proxy['proxy_id']}")
            self.assertEqual(deleting_assigned.status_code, 409)
            cleared = await client.patch("/api/v1/accounts/research", json={"proxy_id": None})
            self.assertEqual(cleared.status_code, 200, cleared.text)
            self.assertIsNone(cleared.json()["proxy_id"])
            deleted = await client.delete(f"/api/v1/proxies/{proxy['proxy_id']}")
            self.assertEqual(deleted.status_code, 200, deleted.text)

    async def test_proxy_validation_and_assignment_conflicts(self):
        async with self.client() as client:
            for url in (
                "socks5://proxy.example:1080",
                "http://proxy.example",
                "http://proxy.example:99999",
                "http://proxy.example:8080/path",
            ):
                response = await client.post("/api/v1/proxies", json={"name": "bad", "url": url})
                self.assertEqual(response.status_code, 400, (url, response.text))

            created = await client.post("/api/v1/proxies", json={
                "name": "Relay", "url": "https://relay.example:8443",
            })
            self.assertEqual(created.status_code, 201, created.text)
            proxy_id = created.json()["proxy_id"]
            self.app.state.pool.register_account_dir("active")
            self.app.state.db.acquire_idle_account("job-1")
            response = await client.patch(
                "/api/v1/accounts/active", json={"proxy_id": proxy_id}
            )
            self.assertEqual(response.status_code, 409, response.text)

    async def test_new_account_authorization_uses_selected_proxy(self):
        capture = self.root / "captured-proxy.json"
        async with self.client() as client:
            created = await client.post("/api/v1/proxies", json={
                "name": "Auth route", "url": "http://auth-user:auth-pass@proxy.example:8080",
            })
            self.assertEqual(created.status_code, 201, created.text)
            proxy_id = created.json()["proxy_id"]
            started = await client.post("/api/v1/account-auth", json={
                "name": "new_login", "tier": "pro", "proxy_id": proxy_id,
            })
            self.assertEqual(started.status_code, 202, started.text)
            session_id = started.json()["session_id"]
            deadline = asyncio.get_running_loop().time() + 3
            while not capture.exists() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
            self.assertTrue(capture.exists(), "mock auth CLI did not capture its environment")
            env = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(env["HTTP_PROXY"], "http://auth-user:auth-pass@proxy.example:8080")
            self.assertEqual(env["HTTPS_PROXY"], env["HTTP_PROXY"])
            self.assertEqual(env["NO_PROXY"], "")
            blocked_edit = await client.patch(f"/api/v1/proxies/{proxy_id}", json={
                "url": "http://new-user:new-pass@proxy.example:8080",
            })
            self.assertEqual(blocked_edit.status_code, 409)
            blocked_delete = await client.delete(f"/api/v1/proxies/{proxy_id}")
            self.assertEqual(blocked_delete.status_code, 409)
            cancelled = await client.delete(f"/api/v1/account-auth/{session_id}")
            self.assertEqual(cancelled.status_code, 200, cancelled.text)

    def test_existing_database_migrates_proxy_assignment_column(self):
        db_path = Path(self.tmp.name) / "legacy.sqlite3"
        import sqlite3
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE accounts (name TEXT PRIMARY KEY, home_dir TEXT NOT NULL, "
                "tier TEXT NOT NULL DEFAULT 'pro', status TEXT NOT NULL DEFAULT 'idle', "
                "current_job_id TEXT, cooldown_until REAL NOT NULL DEFAULT 0, "
                "last_used_at REAL NOT NULL DEFAULT 0, total_runs INTEGER NOT NULL DEFAULT 0, "
                "total_failures INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', "
                "enabled INTEGER NOT NULL DEFAULT 1)"
            )
            conn.execute("INSERT INTO accounts (name, home_dir) VALUES ('legacy', '/accounts/legacy')")
            conn.commit()
        finally:
            conn.close()
        db = PoolDatabase(db_path)
        account = db.get_account("legacy")
        self.assertIsNotNone(account)
        self.assertIsNone(account["proxy_id"])
        self.assertEqual(db.list_proxies(), [])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(db_path.stat().st_mode), 0o600)

    def test_colab_cli_output_redacts_proxy_url_userinfo(self):
        self.assertEqual(
            redact_proxy_credentials("proxy=http://user:p%40ss@host:8080 failed"),
            "proxy=http://***@host:8080 failed",
        )

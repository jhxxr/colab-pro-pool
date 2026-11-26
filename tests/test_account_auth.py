"""Exercise web OAuth against a real interactive child process, without Google."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from colab_pool.api import create_app
from colab_pool.config import PoolSettings
from colab_pool.oauth_store import oauth_token_path, write_oauth_token


MOCK_AUTH_CLI = r'''#!/usr/bin/env python3
import json
import os
import sys
import time
import uuid
from urllib.parse import urlencode
from colab_pool.oauth_store import write_oauth_token

if sys.argv[1:] != ["--auth", "oauth2", "sessions"]:
    sys.exit(3)
assert os.environ["HOME"] == os.environ["USERPROFILE"] == os.environ["COLAB_POOL_ACCOUNT_HOME"]
assert os.environ["PYTHON_KEYRING_BACKEND"] == "colab_pool.keyring_backend.AccountKeyring"

scenario = os.environ.get("MOCK_AUTH_SCENARIO", "normal")
if scenario == "exit":
    print("CLI failed; access_token=DO_NOT_LEAK", flush=True)
    sys.exit(1)
redirect = "https://sdk.cloud.google.com/applicationdefaultauthcode.html"
if scenario == "unsafe_url":
    redirect = "https://attacker.invalid/steal"
url = "https://accounts.google.com/o/oauth2/auth?" + urlencode({
    "redirect_uri": redirect, "state": uuid.uuid4().hex, "client_id": "mock-client"
})
# Split a URL across pipe reads; the prompt deliberately has no newline.
print("Visit this URL:", file=sys.stderr, flush=True)
sys.stderr.write(url[:48])
sys.stderr.flush()
time.sleep(0.01)
sys.stderr.write(url[48:] + "\nEnter the authorization code: ")
sys.stderr.flush()
code = input().strip()
if code == "bad-code":
    print("invalid_grant access_token=DO_NOT_LEAK code=" + code, flush=True)
    sys.exit(1)
if code == "no-save":
    sys.exit(0)
if code == "slow-code":
    time.sleep(30)
token = {
    "token": "access-for-" + code, "refresh_token": "refresh-for-" + code,
    "client_id": "mock-client", "client_secret": "mock-secret",
    "token_uri": "https://oauth2.googleapis.com/token",
    "scopes": [
        "https://www.googleapis.com/auth/colaboratory",
        "https://www.googleapis.com/auth/cloud-platform",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/drive.file",
    ],
}
if code == "partial-consent":
    token["scopes"] = token["scopes"][:1]
if code == "no-refresh":
    token["refresh_token"] = ""
write_oauth_token(os.environ["HOME"], json.dumps(token))
print("No active sessions", flush=True)
'''


def install_auth_cli(root: Path) -> str:
    script = root / "auth_cli.py"
    script.write_text(MOCK_AUTH_CLI, encoding="utf-8")
    if os.name == "nt":
        launcher = root / "auth_cli.cmd"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return str(launcher)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


class WebAccountAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="colab-web-auth-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        data = self.root / "data"
        self.settings = PoolSettings(
            data_dir=data, accounts_dir=data / "accounts", drives_dir=data / "drives",
            jobs_dir=data / "jobs", db_path=data / "pool.sqlite3",
            colab_bin=install_auth_cli(self.root), api_key="web-auth-secret",
            default_cooldown_seconds=600, poll_interval_seconds=0.1,
            cleanup_orphans_on_startup=False, host="127.0.0.1", port=8080,
            auth_timeout_seconds=10, auth_startup_timeout_seconds=3,
        )
        self.app = create_app(self.settings)
        self.addAsyncCleanup(self.app.state.account_auth.stop)
        self.headers = {"Authorization": "Bearer web-auth-secret"}

    def client(self, authenticated: bool = True) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
            headers=self.headers if authenticated else {},
        )

    async def start(self, client, name="pro_web", tier="pro"):
        response = await client.post("/api/v1/account-auth", json={"name": name, "tier": tier})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["session_id"]

    async def wait_status(self, client, session_id, *statuses):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            response = await client.get(f"/api/v1/account-auth/{session_id}")
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            if data["status"] in statuses:
                return data
            await asyncio.sleep(0.02)
        self.fail(f"Authorization did not reach {statuses}: {data}")

    async def test_new_account_is_registered_only_after_success_and_tokens_stay_private(self):
        async with self.client() as client:
            session_id = await self.start(client, tier="pro+")
            pending = await self.wait_status(client, session_id, "awaiting_code")
            self.assertTrue(pending["authorization_url"].startswith("https://accounts.google.com/"))
            accounts = (await client.get("/api/v1/accounts")).json()["accounts"]
            self.assertEqual(accounts, [])
            self.assertIsNone(self.app.state.db.acquire_idle_account("queued-job"))
            response = await client.post(f"/api/v1/account-auth/{session_id}/code", json={"code": "one-time-code"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("one-time-code", response.text)
            done = await self.wait_status(client, session_id, "succeeded", "failed")
            self.assertEqual(done["status"], "succeeded", done)
            self.assertIsNone(done["authorization_url"])
            self.assertTrue(done["account"]["auth_configured"])
            self.assertEqual(done["account"]["tier"], "pro+")
            self.assertEqual(done["account"]["status"], "idle")
            self.assertNotIn("refresh-for-", json.dumps(done))
            saved = oauth_token_path(self.settings.accounts_dir / "pro_web")
            self.assertEqual(json.loads(saved.read_text())["refresh_token"], "refresh-for-one-time-code")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
            self.assertEqual(list((self.settings.data_dir / ".auth").iterdir()), [])
            duplicate = await client.post(f"/api/v1/account-auth/{session_id}/code", json={"code": "one-time-code"})
            self.assertEqual(duplicate.status_code, 409)

    async def test_existing_slot_is_reserved_and_failed_reauth_preserves_credentials(self):
        pool = self.app.state.pool
        acct = pool.register_account_dir("existing")
        write_oauth_token(acct["home_dir"], '{"token":"old-credentials"}')
        self.app.state.db.release_account("existing", cooldown_seconds=600, error_message="old-error")
        pool.update_account("existing", enabled=False)
        async with self.client() as client:
            session_id = await self.start(client, "existing")
            await self.wait_status(client, session_id, "awaiting_code")
            self.assertEqual(self.app.state.db.get_account("existing")["status"], "maintenance")
            for method, path, payload in [
                ("post", "/api/v1/accounts", {"name": "existing"}),
                ("patch", "/api/v1/accounts/existing", {"enabled": True}),
                ("post", "/api/v1/accounts/existing/reset", {}),
                ("delete", "/api/v1/accounts/existing", None),
            ]:
                response = await client.request(method, path, json=payload)
                self.assertEqual(response.status_code, 409, response.text)
            await client.post("/api/v1/pool/sync")
            self.assertEqual(self.app.state.db.get_account("existing")["status"], "maintenance")
            self.assertIsNone(self.app.state.db.acquire_idle_account("another-job"))
            await client.post(f"/api/v1/account-auth/{session_id}/code", json={"code": "bad-code"})
            done = await self.wait_status(client, session_id, "failed")
            self.assertIn("授权码", done["error"])
            self.assertNotIn("DO_NOT_LEAK", json.dumps(done))
            self.assertNotIn("bad-code", json.dumps(done))
            self.assertEqual(oauth_token_path(acct["home_dir"]).read_text(), '{"token":"old-credentials"}')
            updated = self.app.state.db.get_account("existing")
            self.assertEqual(updated["enabled"], 0)
            self.assertEqual(updated["status"], "cooldown")
            self.assertEqual(updated["last_error"], "old-error")

    async def test_concurrent_authorizations_are_isolated(self):
        async with self.client() as client:
            first = await self.start(client, "first")
            second = await self.start(client, "second")
            a = await self.wait_status(client, first, "awaiting_code")
            b = await self.wait_status(client, second, "awaiting_code")
            self.assertNotEqual(a["authorization_url"], b["authorization_url"])
            conflict = await client.post("/api/v1/account-auth", json={"name": "first"})
            self.assertEqual(conflict.status_code, 409)
            await client.post(f"/api/v1/account-auth/{first}/code", json={"code": "first-code"})
            await client.post(f"/api/v1/account-auth/{second}/code", json={"code": "second-code"})
            await self.wait_status(client, first, "succeeded")
            await self.wait_status(client, second, "succeeded")
            tokens = [json.loads(oauth_token_path(self.settings.accounts_dir / n).read_text())["token"] for n in ("first", "second")]
            self.assertEqual(tokens, ["access-for-first-code", "access-for-second-code"])

    async def test_successful_reauth_clears_cooldown_and_preserves_disabled_state(self):
        pool = self.app.state.pool
        pool.register_account_dir("existing")
        pool.update_account("existing", enabled=False)
        self.app.state.db.release_account("existing", cooldown_seconds=600, error_message="expired-token")
        async with self.client() as client:
            session_id = await self.start(client, "existing", "pro+")
            await self.wait_status(client, session_id, "awaiting_code")
            await client.post(f"/api/v1/account-auth/{session_id}/code", json={"code": "renewed-code"})
            done = await self.wait_status(client, session_id, "succeeded")
            acct = done["account"]
            self.assertEqual(acct["status"], "idle")
            self.assertEqual(acct["tier"], "pro+")
            self.assertEqual(acct["enabled"], 0)
            self.assertEqual(acct["last_error"], "")
            self.assertEqual(acct["cooldown_until"], 0)
            pool.sync_accounts_from_disk()
            self.assertEqual(self.app.state.db.get_account("existing")["enabled"], 0)

    async def test_busy_slot_and_invalid_names_are_rejected_without_mutation(self):
        self.app.state.pool.register_account_dir("running")
        self.app.state.db.acquire_idle_account("running-job")
        async with self.client() as client:
            for name in ("../escape", "bad\n", "x" * 65):
                response = await client.post("/api/v1/account-auth", json={"name": name})
                self.assertEqual(response.status_code, 400)
            response = await client.post("/api/v1/account-auth", json={"name": "running"})
            self.assertEqual(response.status_code, 409)
        self.assertEqual(self.app.state.db.get_account("running")["current_job_id"], "running-job")
        self.assertFalse((self.settings.data_dir / ".auth").exists())

    async def test_cancel_reaps_child_and_leaves_new_account_unregistered(self):
        async with self.client() as client:
            session_id = await self.start(client)
            await self.wait_status(client, session_id, "awaiting_code")
            proc = self.app.state.account_auth._sessions[session_id].process
            response = await client.delete(f"/api/v1/account-auth/{session_id}")
            self.assertEqual(response.json()["status"], "cancelled")
            self.assertIsNotNone(proc.returncode)
            self.assertIsNone(self.app.state.db.get_account("pro_web"))
            self.assertFalse((self.settings.accounts_dir / "pro_web").exists())
            self.assertEqual(list((self.settings.data_dir / ".auth").iterdir()), [])
            self.assertEqual((await client.delete(f"/api/v1/account-auth/{session_id}")).json()["status"], "cancelled")

    async def test_shutdown_before_task_start_releases_maintenance_reservation(self):
        self.app.state.pool.register_account_dir("existing")
        manager = self.app.state.account_auth
        session_id = manager.start("existing", "pro")["session_id"]
        await manager.stop()
        self.assertEqual(manager.get(session_id)["status"], "cancelled")
        self.assertFalse(manager.is_authorizing("existing"))
        self.assertEqual(self.app.state.db.get_account("existing")["status"], "idle")

    async def test_cancel_during_verification_preserves_old_token_and_reaps_child(self):
        acct = self.app.state.pool.register_account_dir("existing")
        write_oauth_token(acct["home_dir"], '{"token":"old-token"}')
        async with self.client() as client:
            session_id = await self.start(client, "existing")
            await self.wait_status(client, session_id, "awaiting_code")
            proc = self.app.state.account_auth._sessions[session_id].process
            await client.post(f"/api/v1/account-auth/{session_id}/code", json={"code": "slow-code"})
            await client.delete(f"/api/v1/account-auth/{session_id}")
            self.assertIsNotNone(proc.returncode)
            self.assertEqual(oauth_token_path(acct["home_dir"]).read_text(), '{"token":"old-token"}')
            self.assertEqual(self.app.state.db.get_account("existing")["status"], "idle")

    async def test_expiry_and_unsafe_redirect_release_account(self):
        self.app.state.pool.register_account_dir("existing")
        self.app.state.account_auth.settings = replace(self.settings, auth_timeout_seconds=0.6, auth_startup_timeout_seconds=2)
        async with self.client() as client:
            session_id = await self.start(client, "existing")
            await self.wait_status(client, session_id, "expired")
            self.assertEqual(self.app.state.db.get_account("existing")["status"], "idle")
            self.app.state.account_auth.settings = replace(self.settings, auth_timeout_seconds=3, auth_startup_timeout_seconds=0.5)
            with patch.dict(os.environ, {"MOCK_AUTH_SCENARIO": "unsafe_url"}):
                session_id = await self.start(client, "existing")
                done = await self.wait_status(client, session_id, "failed")
            self.assertIsNone(done["authorization_url"])
            self.assertEqual(self.app.state.db.get_account("existing")["status"], "idle")

    async def test_partial_consent_missing_refresh_and_unsaved_credentials_fail(self):
        async with self.client() as client:
            for code in ("partial-consent", "no-refresh", "no-save"):
                with self.subTest(code=code):
                    session_id = await self.start(client)
                    await self.wait_status(client, session_id, "awaiting_code")
                    response = await client.post(f"/api/v1/account-auth/{session_id}/code", json={"code": code})
                    self.assertEqual(response.status_code, 200)
                    await self.wait_status(client, session_id, "failed")
                    self.assertIsNone(self.app.state.db.get_account("pro_web"))

    async def test_code_validation_does_not_feed_extra_lines_to_cli(self):
        async with self.client() as client:
            session_id = await self.start(client)
            await self.wait_status(client, session_id, "awaiting_code")
            for code in ("", "code\nextra-command", "code\x00suffix", "x" * 4097):
                response = await client.post(f"/api/v1/account-auth/{session_id}/code", json={"code": code})
                self.assertEqual(response.status_code, 400)
                if code:
                    self.assertNotIn(code, response.text)
            self.assertEqual((await client.get(f"/api/v1/account-auth/{session_id}")).json()["status"], "awaiting_code")

    async def test_authentication_is_required_on_all_session_routes(self):
        async with self.client() as client:
            session_id = await self.start(client)
        async with self.client(authenticated=False) as client:
            for method, path, payload in [
                ("post", "/api/v1/account-auth", {"name": "another"}),
                ("get", f"/api/v1/account-auth/{session_id}", None),
                ("post", f"/api/v1/account-auth/{session_id}/code", {"code": "secret-code"}),
                ("delete", f"/api/v1/account-auth/{session_id}", None),
            ]:
                self.assertEqual((await client.request(method, path, json=payload)).status_code, 401)

    async def test_missing_cli_fails_cleanly_and_releases_existing_slot(self):
        self.app.state.pool.register_account_dir("existing")
        self.app.state.account_auth.settings = replace(self.settings, colab_bin=str(self.root / "missing-colab"))
        async with self.client() as client:
            session_id = await self.start(client, "existing")
            done = await self.wait_status(client, session_id, "failed")
            self.assertIn("Colab CLI", done["error"])
            self.assertEqual(self.app.state.db.get_account("existing")["status"], "idle")

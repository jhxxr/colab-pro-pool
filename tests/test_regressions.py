"""Deterministic lifecycle regression tests without a real Colab connection."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from colab_pool.api import create_app
from colab_pool.config import PoolSettings


class LifecycleRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.settings = PoolSettings(
            data_dir=root, accounts_dir=root / "accounts", drives_dir=root / "drives",
            jobs_dir=root / "jobs", db_path=root / "pool.sqlite3", colab_bin="colab",
            api_key="", default_cooldown_seconds=600, poll_interval_seconds=0.1,
            cleanup_orphans_on_startup=False, host="127.0.0.1", port=8080,
        )
        self.app = create_app(self.settings)
        self.pool = self.app.state.pool
        self.db = self.app.state.db
        self.scheduler = self.app.state.scheduler
        self.pool.register_account_dir("pro_01")

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )

    async def submit(self):
        async with self.client() as client:
            response = await client.post("/api/v1/jobs", json={"script_content": "print(1)"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["job_id"]

    async def run_job(self, job_id):
        self.scheduler._try_schedule_queued_jobs()
        await self.scheduler._active_tasks[job_id]
        return self.db.get_job(job_id)

    async def test_provisioning_errors_release_lease_and_attempt_teardown(self):
        for phase, error in [
            ("stop", asyncio.TimeoutError()),
            ("new", asyncio.TimeoutError()),
            ("stop", FileNotFoundError("missing colab")),
        ]:
            with self.subTest(phase=phase, error=type(error).__name__):
                job_id = await self.submit()
                calls = []

                async def fake_cli(home, args, *a, **kw):
                    calls.append(args)
                    if args[0] == phase and "-s" not in args[1:]:
                        raise error
                    if args[0] == phase == "new":
                        raise error
                    return 0, "ok"

                with patch.object(self.scheduler, "_run_cli_logged", side_effect=fake_cli):
                    job = await self.run_job(job_id)
                self.assertEqual(job["status"], "failed")
                self.assertIsNotNone(job["finished_at"])
                self.assertEqual(self.db.get_account("pro_01")["status"], "idle")
                self.assertTrue(any(args[:2] == ["stop", "-s"] for args in calls))

    async def test_failover_error_releases_current_not_initial_owner(self):
        self.pool.register_account_dir("pro_02")
        job_id = await self.submit()

        async def fake_cli(home, args, *a, **kw):
            if args[0] == "new":
                if Path(home).name == "pro_01":
                    return 1, "RESOURCE_EXHAUSTED"
                raise asyncio.TimeoutError()
            return 0, "ok"

        with patch.object(self.scheduler, "_run_cli_logged", side_effect=fake_cli):
            job = await self.run_job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.db.get_account("pro_01")["status"], "cooldown")
        self.assertEqual(self.db.get_account("pro_02")["status"], "idle")

    async def test_final_exit_marker_and_cli_failure_are_respected(self):
        for cli_rc, output, expected in [
            (1, "__COLAB_POOL_EXIT_CODE__=0\n__COLAB_POOL_EXIT_CODE__=1\n", "failed"),
            (1, "__COLAB_POOL_EXIT_CODE__=0\n", "failed"),
            (0, "execution ended without marker\n", "failed"),
            (0, "__COLAB_POOL_EXIT_CODE__=1\n__COLAB_POOL_EXIT_CODE__=0\n", "succeeded"),
        ]:
            with self.subTest(cli_rc=cli_rc, output=output):
                job_id = await self.submit()

                async def fake_cli(home, args, *a, **kw):
                    if args[0] == "exec":
                        return cli_rc, output
                    if args[0] == "download":
                        return 1, "no artifacts"
                    return 0, "ok"

                with patch.object(self.scheduler, "_run_cli_logged", side_effect=fake_cli):
                    job = await self.run_job(job_id)
                self.assertEqual(job["status"], expected)

    async def test_cleanup_uses_live_reservations_and_blocks_scheduler(self):
        self.pool.register_account_dir("pro_02")
        started = asyncio.Event()
        proceed = asyncio.Event()
        stopped = []
        db = self.db

        class Proc:
            returncode = 0

            async def communicate(proc):
                started.set()
                await proceed.wait()
                stopped.append("pro_01")
                return b"stopped", None

        with patch("colab_pool.pool_manager.asyncio.create_subprocess_exec", return_value=Proc()):
            cleanup = asyncio.create_task(self.pool.cleanup_orphan_runtimes())
            await started.wait()
            try:
                self.assertEqual(db.get_account("pro_01")["status"], "maintenance")
                # Clearing cooldown and rescanning must not erase the reservation.
                self.pool.update_account("pro_01", clear_cooldown=True)
                self.pool.sync_accounts_from_disk()
                leased = db.acquire_idle_account("job-during-cleanup")
                self.assertEqual(leased["name"], "pro_02")
                with self.assertRaises(ValueError):
                    self.pool.delete_account("pro_01")
            finally:
                proceed.set()
                results = await cleanup
        self.assertEqual(stopped, ["pro_01"])
        self.assertEqual(results["pro_02"], "skipped (busy)")
        self.assertEqual(db.get_account("pro_01")["status"], "idle")
        self.assertEqual(db.get_account("pro_02")["status"], "busy")

    async def test_cleanup_timeout_kills_process_and_preserves_cooldown(self):
        self.db.release_account("pro_01", cooldown_seconds=600)
        proc = AsyncMock()
        proc.returncode = None
        proc.communicate.side_effect = [asyncio.TimeoutError(), (b"", None)]
        from unittest.mock import Mock
        proc.kill = Mock()
        with patch("colab_pool.pool_manager.asyncio.create_subprocess_exec", return_value=proc):
            results = await self.pool.cleanup_orphan_runtimes()
        self.assertIn("error:", results["pro_01"])
        proc.kill.assert_called_once()
        self.assertEqual(self.db.get_account("pro_01")["status"], "cooldown")

    async def test_default_account_delete_survives_sync_and_restart(self):
        async with self.client() as client:
            response = await client.delete("/api/v1/accounts/pro_01")
            self.assertEqual(response.status_code, 200)
            response = await client.get("/api/v1/accounts")
            self.assertEqual(response.json()["accounts"], [])
        self.assertTrue((self.settings.accounts_dir / "pro_01").is_dir())
        restarted = create_app(self.settings)
        restarted.state.pool.sync_accounts_from_disk()
        self.assertIsNone(restarted.state.db.get_account("pro_01"))
        # Explicit registration restores the existing credentials directory.
        restarted.state.pool.register_account_dir("pro_01")
        restarted.state.pool.sync_accounts_from_disk()
        self.assertEqual(restarted.state.db.get_account("pro_01")["enabled"], 1)

    async def test_provisioning_cancellation_releases_lease(self):
        job_id = await self.submit()
        started = asyncio.Event()

        async def fake_cli(home, args, *a, **kw):
            if args[0] == "new":
                started.set()
                await asyncio.Event().wait()
            return 0, "ok"

        with patch.object(self.scheduler, "_run_cli_logged", side_effect=fake_cli):
            self.scheduler._try_schedule_queued_jobs()
            task = self.scheduler._active_tasks[job_id]
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.db.get_job(job_id)["status"], "cancelled")
        self.assertEqual(self.db.get_account("pro_01")["status"], "idle")

    async def test_failed_delete_rolls_back_removal_marker(self):
        self.db.acquire_idle_account("active-job")
        with self.assertRaises(ValueError):
            self.pool.delete_account("pro_01")
        self.assertFalse((self.settings.accounts_dir / "pro_01" / ".removed").exists())
        self.pool.sync_accounts_from_disk()
        self.assertEqual(self.db.get_account("pro_01")["status"], "busy")

    async def test_cleanup_cancellation_releases_reservation_and_kills_process(self):
        started = asyncio.Event()
        from unittest.mock import Mock
        proc = Mock()
        proc.returncode = None
        count = 0

        async def communicate():
            nonlocal count
            count += 1
            if count == 1:
                started.set()
                await asyncio.Event().wait()
            return b"", None

        proc.communicate = communicate
        with patch("colab_pool.pool_manager.asyncio.create_subprocess_exec", return_value=proc):
            task = asyncio.create_task(self.pool.cleanup_orphan_runtimes())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        proc.kill.assert_called_once()
        self.assertEqual(self.db.get_account("pro_01")["status"], "idle")

    async def test_boot_recovers_stale_maintenance_reservations(self):
        self.assertTrue(self.db.acquire_account_for_cleanup("pro_01"))
        self.db.reset_stale_busy_accounts_on_boot()
        self.assertEqual(self.db.get_account("pro_01")["status"], "idle")

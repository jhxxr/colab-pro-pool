"""Asynchronous Colab Pro job dispatcher and lifecycle runner."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import tarfile
import time
from pathlib import Path
from typing import Any

from .config import PoolSettings
from .access_keys import KeyPolicyError, McpKeyStore
from .account_usage import AccountUsageManager, parse_usage_output
from .db import PoolDatabase
from .pool_manager import AccountPoolManager
from .proxy_config import redact_proxy_credentials
from .remote_bootstrap import KERNEL_BOOTSTRAP_TEMPLATE, build_job_bundle
from .resources import sanitize_runtime_info
from .utils import safe_extract_tar

logger = logging.getLogger("colab_pool.runner")

QUOTA_OR_CAPACITY_PATTERNS = (
    "failed_precondition",
    "resource_exhausted",
    "quota",
    "compute unit",
    "usage limit",
    "no capacity",
    "unavailable",
    "too many active runtimes",
    "cannot connect to gpu",
)

AUTH_ERROR_PATTERNS = (
    "unauthenticated",
    "invalid_grant",
    "token expired",
    "unauthorized",
    "login required",
    "re-authenticate",
)

PRO_PLUS_GPUS = {"H100", "G4"}
EXIT_MARKER_RE = re.compile(r"^__COLAB_POOL_EXIT_CODE__=(-?\d+)\s*$", re.MULTILINE)
GPU_MARKER_RE = re.compile(r"__COLAB_POOL_GPU__=(.+)")
RUNTIME_MARKER_RE = re.compile(r"^__COLAB_POOL_RUNTIME__=(.+)$")


def is_provisioning_failover_error(output: str) -> bool:
    low = output.lower()
    return any(pat in low for pat in QUOTA_OR_CAPACITY_PATTERNS)


def is_auth_error(output: str) -> bool:
    low = output.lower()
    return any(pat in low for pat in AUTH_ERROR_PATTERNS)


class ColabJobScheduler:
    """Background worker loop that matches queued jobs to idle Colab Pro accounts."""

    def __init__(
        self,
        settings: PoolSettings,
        db: PoolDatabase,
        pool: AccountPoolManager,
    ) -> None:
        self.settings = settings
        self.db = db
        self.pool = pool
        self.keys = McpKeyStore(db)
        self._key_usage = AccountUsageManager(pool)
        self.key_watch_interval = 1.0
        self.key_usage_interval = 10.0
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._active_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_procs: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled_jobs: set[str] = set()
        self._kill_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self.pool.sync_accounts_from_disk()
        self.db.reset_stale_busy_accounts_on_boot()
        if self.settings.cleanup_orphans_on_startup:
            await self.pool.cleanup_orphan_runtimes(skip_busy=True)
        self._stop_event.clear()
        self._dispatcher_task = asyncio.create_task(self._dispatch_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._dispatcher_task is not None:
            await self._dispatcher_task
        for job_id in list(self._active_tasks.keys()):
            await self.cancel_job(job_id)
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks.values(), return_exceptions=True)
        for task in list(self._kill_tasks):
            task.cancel()
        if self._kill_tasks:
            await asyncio.gather(*self._kill_tasks, return_exceptions=True)

    def notify_new_job(self) -> None:
        self._wake_event.set()

    async def _terminate_proc(
        self, proc: asyncio.subprocess.Process, force: bool = False
    ) -> None:
        if proc.returncode is not None:
            return
        if os.name == "nt":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/F",
                    "/T",
                    "/PID",
                    str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            except Exception:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            return

        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        except Exception:
            try:
                proc.kill() if force else proc.terminate()
            except ProcessLookupError:
                pass

    async def _escalate_kill(
        self, proc: asyncio.subprocess.Process, delay: float = 5.0
    ) -> None:
        await asyncio.sleep(delay)
        if proc.returncode is None:
            await self._terminate_proc(proc, force=True)

    async def cancel_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.db.get_job(job_id)
        if job is None:
            return None
        if job["status"] == "queued":
            return self.db.update_job(
                job_id,
                status="cancelled",
                error_message="Cancelled while queued.",
                finished_at=time.time(),
            )
        if job["status"] in {"provisioning", "running"}:
            self._cancelled_jobs.add(job_id)
            proc = self._active_procs.get(job_id)
            if proc and proc.returncode is None:
                await self._terminate_proc(proc, force=False)
                if proc.returncode is None:
                    kt = asyncio.create_task(self._escalate_kill(proc, delay=5.0))
                    self._kill_tasks.add(kt)
                    kt.add_done_callback(self._kill_tasks.discard)
            return self.db.update_job(
                job_id,
                error_message="Cancellation requested; tearing down Colab runtime...",
            )
        return job

    async def _dispatch_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.pool.sync_accounts_from_disk()
                self._try_schedule_queued_jobs()
            except Exception as exc:
                logger.exception("Error in dispatch loop: %s", exc)

            self._wake_event.clear()
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=self.settings.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass

    def _try_schedule_queued_jobs(self) -> None:
        queued = self.db.list_queued_jobs()
        for job in queued:
            job_id = job["job_id"]
            if job_id in self._active_tasks:
                continue
            try:
                self.keys.check_job(job)
            except KeyPolicyError as exc:
                self.db.update_job(job_id, status="failed", error_message=str(exc), finished_at=time.time())
                continue
            require_pro_plus = job["gpu"].upper() in PRO_PLUS_GPUS
            acct = self.db.acquire_idle_account(
                job_id=job_id,
                exclude_accounts=job.get("attempted_accounts", []),
                require_pro_plus=require_pro_plus,
            )
            if acct is None:
                continue

            session_name = f"job-{job_id[:12]}".lower().replace("_", "-")
            attempted = list(job.get("attempted_accounts", [])) + [acct["name"]]
            self.db.update_job(
                job_id,
                status="provisioning",
                assigned_account=acct["name"],
                session_name=session_name,
                attempted_accounts_json=json.dumps(attempted),
                started_at=time.time(),
            )
            task = asyncio.create_task(self._execute_job_with_failover(job_id, acct))
            self._active_tasks[job_id] = task
            task.add_done_callback(lambda _, jid=job_id: self._active_tasks.pop(jid, None))

    async def _execute_job_with_failover(
        self, job_id: str, initial_account: dict[str, Any]
    ) -> None:
        """Guard the entire lease lifetime, including pre-clean and provisioning."""
        try:
            job = self.db.get_job(job_id)
            if job and job.get("mcp_key_id"):
                await self._execute_guarded_job(job_id, initial_account)
            else:
                await self._execute_job_attempts(job_id, initial_account)
        except (Exception, asyncio.CancelledError) as exc:
            logger.warning("Job %s interrupted: %s", job_id, exc)
            job = self.db.get_job(job_id)
            was_cancelled = (
                job_id in self._cancelled_jobs or isinstance(exc, asyncio.CancelledError)
            )
            try:
                # Failover may have changed the owner since the initial lease.
                for account in self.db.list_accounts():
                    if account.get("current_job_id") != job_id:
                        continue
                    try:
                        if job is not None:
                            stopped = await self._teardown_session(
                                account["home_dir"], job["session_name"], Path(job["log_path"])
                            )
                            if job.get("mcp_key_id") and job.get("cu_baseline_balance") is not None and not stopped:
                                self._quarantine_key_job(job, account["name"])
                    except Exception as cleanup_exc:
                        logger.warning("Recovery cleanup failed for %s: %s", job_id, cleanup_exc)
                        if job and job.get("mcp_key_id") and job.get("cu_baseline_balance") is not None:
                            self._quarantine_key_job(job, account["name"])
                    finally:
                        if job and job.get("mcp_key_id") and job.get("cu_baseline_balance") is not None:
                            await self._finish_key_meter(job, account["home_dir"])
                        else:
                            self.keys.meter(job_id, finish=True)
                        self.db.release_account(account["name"], increment_run=False)
            finally:
                self._cancelled_jobs.discard(job_id)
                latest = self.db.get_job(job_id)
                stop_error = latest and latest.get("error_message", "").startswith("[RUNTIME_STOP_FAILED]")
                self.db.update_job(
                    job_id,
                    status="cancelled" if was_cancelled and not stop_error else "failed",
                    exit_code=124 if isinstance(exc, asyncio.TimeoutError) else 1,
                    error_message=(
                        latest["error_message"] if stop_error else
                        "Job cancelled during provisioning." if was_cancelled else
                        "Colab CLI timed out during provisioning." if isinstance(exc, asyncio.TimeoutError) else
                        f"Runner error: {exc}"
                    ),
                    finished_at=time.time(),
                )
                self._wake_event.set()
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def _execute_guarded_job(self, job_id: str, account: dict[str, Any]) -> None:
        """The independent guard can stop a silent CLI or a slow CU query."""
        worker = asyncio.create_task(self._execute_job_attempts(job_id, account))
        guard = asyncio.create_task(self._watch_key_job(job_id))
        try:
            done, _ = await asyncio.wait((worker, guard), return_when=asyncio.FIRST_COMPLETED)
            if guard in done:
                guard.result()
            await worker
        finally:
            for task in (worker, guard):
                if not task.done():
                    task.cancel()
            await asyncio.gather(worker, guard, return_exceptions=True)

    async def _read_key_usage(self, home: str) -> tuple[float, float, int]:
        return parse_usage_output(await self._key_usage._query_cli(home))

    async def _watch_key_job(self, job_id: str) -> None:
        query: asyncio.Task[tuple[float, float, int]] | None = None
        queried_at = 0.0
        query_account = ""
        try:
            while True:
                if job_id in self._cancelled_jobs:
                    raise asyncio.CancelledError()
                self.keys.meter(job_id)
                job = self.db.get_job(job_id)
                if job is None:
                    return
                self.keys.check_job(job)
                # Initial rate is confirmed synchronously after allocation.
                # Query in a separate task so revocation/quota checks keep running.
                if query and query.done():
                    balance, rate, count = query.result()
                    query = None
                    if query_account == job["assigned_account"] and job["cu_meter_at"] is not None:
                        if count != 1 or (rate <= 0 and (job["tpu"] or job["gpu"] != "CPU")):
                            raise KeyPolicyError("无法独占确认此任务的 CU 用量，已停止运行。")
                        self.keys.meter(job_id, balance=balance, rate=rate)
                if job["cu_meter_at"] is not None and job["cu_rate"] is not None and query is None:
                    if time.monotonic() - queried_at >= self.key_usage_interval:
                        acct = self.db.get_account(job["assigned_account"])
                        if acct:
                            query_account = job["assigned_account"]
                            query = asyncio.create_task(self._read_key_usage(acct["home_dir"]))
                            queried_at = time.monotonic()
                await asyncio.sleep(self.key_watch_interval)
        finally:
            if query:
                query.cancel()
                await asyncio.gather(query, return_exceptions=True)

    async def _finish_key_meter(self, job: dict[str, Any], home: str) -> None:
        if not job.get("mcp_key_id"):
            return
        self.keys.meter(job["job_id"], finish=True)
        reconciled = False
        try:
            balance, _, count = await self._read_key_usage(home)
            if count == 0:
                self.keys.meter(job["job_id"], balance=balance)
                reconciled = True
        except Exception:
            # The last verified rate remains an estimate if final querying fails.
            logger.warning("Final CU query failed for job %s; keeping rate estimate", job["job_id"])
        finally:
            current = self.db.get_job(job["job_id"])
            if not reconciled and current and current["cu_rate"] is None:
                self.keys.update(job["mcp_key_id"], {"enabled": False})
                logger.warning("Suspended MCP key for job %s: CU consumption could not be confirmed", job["job_id"])

    def _quarantine_key_job(self, job: dict[str, Any], account_name: str) -> str:
        reason = "[RUNTIME_STOP_FAILED] 无法确认远端实例已停止；已停用 MCP 密钥和账号，请管理员清理实例后再恢复。"
        self.keys.update(job["mcp_key_id"], {"enabled": False})
        self.pool.update_account(account_name, enabled=False)
        self.db.update_job(job["job_id"], error_message=reason)
        return reason

    async def _execute_job_attempts(
        self, job_id: str, initial_account: dict[str, Any]
    ) -> None:
        acct = initial_account
        while True:
            job = self.db.get_job(job_id)
            if job is None or job_id in self._cancelled_jobs:
                self.db.release_account(acct["name"], increment_run=False)
                if job is not None:
                    self.db.update_job(
                        job_id,
                        status="cancelled",
                        finished_at=time.time(),
                    )
                self._cancelled_jobs.discard(job_id)
                self._wake_event.set()
                return

            log_file = Path(job["log_path"])
            log_file.parent.mkdir(parents=True, exist_ok=True)
            session_name = job["session_name"]
            acct_name = acct["name"]
            home_dir = acct["home_dir"]

            self._append_log(
                log_file,
                f"=== [{time.strftime('%Y-%m-%d %H:%M:%S')}] Provisioning Colab runtime on account '{acct_name}' (session={session_name}, gpu={job['gpu']}, tpu={job['tpu']}) ===",
            )

            # Best-effort pre-clean of any orphan session on this account
            await self._run_cli_logged(
                home_dir, ["stop"], log_file, job_id=job_id, timeout=20.0, quiet=True
            )

            if job.get("mcp_key_id"):
                self.keys.check_job(job)
                balance, _, count = await self._read_key_usage(home_dir)
                if count != 0:
                    raise KeyPolicyError("账号仍有活动实例，无法独占计量 MCP 任务。")
                self.keys.begin_meter(job_id, balance)

            # Build `colab new` arguments
            new_args = ["new", "-s", session_name]
            if job["tpu"]:
                new_args += ["--tpu", job["tpu"]]
            elif job["gpu"] and job["gpu"].upper() != "CPU":
                new_args += ["--gpu", job["gpu"].upper()]
            if job["high_mem"]:
                new_args.append("--high-mem")

            rc, alloc_out = await self._run_cli_logged(
                home_dir, new_args, log_file, job_id=job_id, timeout=180.0
            )

            if rc != 0:
                if job.get("mcp_key_id"):
                    stopped = await self._teardown_session(home_dir, session_name, log_file)
                    if not stopped:
                        raise KeyPolicyError(self._quarantine_key_job(job, acct_name))
                    await self._finish_key_meter(job, home_dir)
                if job_id in self._cancelled_jobs:
                    await self._teardown_session(home_dir, session_name, log_file)
                    self.db.release_account(acct_name, increment_run=False)
                    self.db.update_job(
                        job_id,
                        status="cancelled",
                        error_message="Cancelled during provisioning.",
                        finished_at=time.time(),
                    )
                    self._cancelled_jobs.discard(job_id)
                    self._wake_event.set()
                    return

                non_empty_lines = [ln.strip() for ln in alloc_out.splitlines() if ln.strip()]
                err_summary = (
                    non_empty_lines[-1] if non_empty_lines else f"colab new exit={rc}"
                )
                if is_auth_error(alloc_out):
                    cooldown = max(self.settings.default_cooldown_seconds, 3600)
                    err_summary = f"[AUTH_REQUIRED] {err_summary}"
                elif is_provisioning_failover_error(alloc_out):
                    cooldown = self.settings.default_cooldown_seconds
                else:
                    cooldown = 300

                self._append_log(
                    log_file,
                    f"[colab-pool] Account '{acct_name}' failed provisioning ({err_summary}); placing in cooldown ({cooldown}s) and checking next idle Pro account...",
                )
                self.db.release_account(
                    acct_name,
                    cooldown_seconds=cooldown,
                    error_message=err_summary,
                    increment_run=False,
                )

                fresh_job = self.db.get_job(job_id)
                assert fresh_job is not None
                require_pro_plus = fresh_job["gpu"].upper() in PRO_PLUS_GPUS
                next_acct = self.db.acquire_idle_account(
                    job_id=job_id,
                    exclude_accounts=fresh_job.get("attempted_accounts", []),
                    require_pro_plus=require_pro_plus,
                )
                if next_acct is not None:
                    attempted = list(fresh_job.get("attempted_accounts", [])) + [next_acct["name"]]
                    self.db.update_job(
                        job_id,
                        assigned_account=next_acct["name"],
                        attempted_accounts_json=json.dumps(attempted),
                    )
                    acct = next_acct
                    continue

                # If other accounts exist and are merely busy (not tried yet), requeue; else fail
                all_eligible = [
                    a["name"]
                    for a in self.db.list_accounts()
                    if a["enabled"] and (not require_pro_plus or a["tier"].lower() == "pro+")
                ]
                untried = set(all_eligible) - set(fresh_job.get("attempted_accounts", []))
                if untried:
                    self._append_log(
                        log_file,
                        f"[colab-pool] Remaining untried accounts {sorted(untried)} are currently busy; returning job to queue.",
                    )
                    self.db.update_job(
                        job_id, status="queued", assigned_account=""
                    )
                else:
                    self.db.update_job(
                        job_id,
                        status="failed",
                        exit_code=rc,
                        error_message=f"All eligible Pro accounts failed provisioning. Last error ({acct_name}): {err_summary}",
                        finished_at=time.time(),
                    )
                self._wake_event.set()
                return

            # Runtime allocated! Guarantee `colab stop`, account release, and final status update in `finally`
            was_cancelled = False
            pending_update: dict[str, Any] | None = None
            try:
                if job.get("mcp_key_id"):
                    balance, rate, count = await self._read_key_usage(home_dir)
                    if count != 1 or (rate <= 0 and (job["tpu"] or job["gpu"] != "CPU")):
                        raise KeyPolicyError("未能确认实例的 CU 消耗速率，已停止任务。")
                    self.keys.meter(job_id, balance=balance, rate=rate)
                    self.keys.check_job(self.db.get_job(job_id))
                self.db.update_job(job_id, status="running")
                job_dir = Path(job["workspace_dir"]).parent
                bundle_path = job_dir / "_job_bundle.tar.gz"
                entry_script_path = job_dir / "_kernel_entry.py"

                build_job_bundle(
                    job_workspace=Path(job["workspace_dir"]),
                    job_record=job,
                    drives_dir=self.settings.drives_dir,
                    output_bundle_path=bundle_path,
                )
                entry_script_path.write_text(KERNEL_BOOTSTRAP_TEMPLATE.strip() + "\n", encoding="utf-8")

                self._append_log(log_file, "[colab-pool] Uploading job bundle to Colab VM...")
                up_rc, up_out = await self._run_cli_logged(
                    home_dir,
                    ["upload", "-s", session_name, str(bundle_path), "/content/_job_bundle.tar.gz"],
                    log_file,
                    job_id=job_id,
                    timeout=300.0,
                )
                if job_id in self._cancelled_jobs:
                    was_cancelled = True
                    self._append_log(log_file, "[colab-pool] Job cancelled before execution.")
                    pending_update = {
                        "status": "cancelled",
                        "error_message": "Job cancelled by user/agent.",
                        "finished_at": time.time(),
                    }
                    return
                if up_rc != 0:
                    raise RuntimeError(f"Failed uploading job bundle (exit={up_rc}): {up_out[-300:]}")

                self._append_log(log_file, "[colab-pool] Starting remote training execution...")
                exec_rc, exec_out = await self._run_cli_logged(
                    home_dir,
                    ["exec", "-s", session_name, "-f", str(entry_script_path)],
                    log_file,
                    job_id=job_id,
                    timeout=float(job["timeout_seconds"]),
                )

                # Workload stdout may contain marker-like text; the bootstrap's
                # final marker is authoritative, but cannot override a CLI failure.
                markers = EXIT_MARKER_RE.findall(exec_out)
                workload_rc = int(markers[-1]) if markers else (exec_rc or 1)
                if exec_rc != 0 and workload_rc == 0:
                    workload_rc = exec_rc

                if job_id in self._cancelled_jobs:
                    was_cancelled = True
                    self._append_log(
                        log_file,
                        "[colab-pool] Job cancelled; skipping artifact download.",
                    )
                    pending_update = {
                        "status": "cancelled",
                        "exit_code": workload_rc,
                        "error_message": "Job cancelled by user/agent.",
                        "finished_at": time.time(),
                    }
                    return

                # Pull `/tmp/_job_artifacts.tar.gz` back to server before VM teardown
                local_artifacts_tar = job_dir / "outputs.tar.gz"
                local_outputs_dir = job_dir / "outputs"
                dl_rc, _ = await self._run_cli_logged(
                    home_dir,
                    ["download", "-s", session_name, "/tmp/_job_artifacts.tar.gz", str(local_artifacts_tar)],
                    log_file,
                    job_id=job_id,
                    timeout=600.0,
                    quiet=True,
                )
                artifacts_archive_str = ""
                if dl_rc == 0 and local_artifacts_tar.exists() and local_artifacts_tar.stat().st_size > 0:
                    local_outputs_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        with tarfile.open(local_artifacts_tar, "r:gz") as tf:
                            safe_extract_tar(tf, local_outputs_dir)
                        artifacts_archive_str = str(local_artifacts_tar)
                        self._append_log(
                            log_file,
                            f"[colab-pool] Downloaded artifacts to {local_outputs_dir}",
                        )
                    except Exception as tar_exc:
                        self._append_log(log_file, f"[colab-pool] Warning extracting outputs: {tar_exc}")

                if job_id in self._cancelled_jobs:
                    was_cancelled = True
                    pending_update = {
                        "status": "cancelled",
                        "exit_code": workload_rc,
                        "artifacts_archive": artifacts_archive_str,
                        "error_message": "Job cancelled by user/agent.",
                        "finished_at": time.time(),
                    }
                elif workload_rc == 0:
                    pending_update = {
                        "status": "succeeded",
                        "exit_code": 0,
                        "artifacts_archive": artifacts_archive_str,
                        "finished_at": time.time(),
                    }
                else:
                    pending_update = {
                        "status": "failed",
                        "exit_code": workload_rc,
                        "artifacts_archive": artifacts_archive_str,
                        "error_message": f"Training exited with code {workload_rc}. Inspect job logs for traceback.",
                        "finished_at": time.time(),
                    }
            except asyncio.TimeoutError:
                self._append_log(log_file, f"[colab-pool] Job timed out after {job['timeout_seconds']}s.")
                pending_update = {
                    "status": "failed",
                    "exit_code": 124,
                    "error_message": f"Timed out after {job['timeout_seconds']} seconds.",
                    "finished_at": time.time(),
                }
            except Exception as exc:
                self._append_log(log_file, f"[colab-pool] Runner error: {exc}")
                pending_update = {
                    "status": "failed",
                    "exit_code": 1,
                    "error_message": str(exc),
                    "finished_at": time.time(),
                }
            finally:
                if job_id in self._cancelled_jobs:
                    was_cancelled = True
                self._cancelled_jobs.discard(job_id)
                stopped = False
                try:
                    stopped = await self._teardown_session(home_dir, session_name, log_file)
                except Exception as td_exc:
                    logger.warning("Teardown failed for %s (%s): %s", session_name, acct_name, td_exc)
                if job.get("mcp_key_id") and not stopped:
                    reason = self._quarantine_key_job(job, acct_name)
                    pending_update = {"status": "failed", "error_message": reason, "finished_at": time.time()}
                await self._finish_key_meter(job, home_dir)
                self.db.release_account(acct_name, increment_run=not was_cancelled)
                if pending_update is not None:
                    self.db.update_job(job_id, **pending_update)
                else:
                    self.db.update_job(
                        job_id,
                        status="cancelled" if was_cancelled else "failed",
                        error_message="Job interrupted during execution.",
                        finished_at=time.time(),
                    )
                self._wake_event.set()
            return

    async def _teardown_session(self, home_dir: str, session_name: str, log_file: Path) -> bool:
        self._append_log(log_file, f"[colab-pool] Tearing down Colab runtime '{session_name}'...")
        rc, _ = await self._run_cli_logged(
            home_dir, ["stop", "-s", session_name], log_file, timeout=35.0, quiet=False
        )
        return rc == 0

    async def _run_cli_logged(
        self,
        home_dir: str,
        args: list[str],
        log_file: Path,
        *,
        job_id: str | None = None,
        timeout: float = 300.0,
        quiet: bool = False,
    ) -> tuple[int, str]:
        exec_kwargs: dict[str, Any] = {
            "env": self.pool.build_cli_env(home_dir),
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
        }
        if os.name != "nt":
            exec_kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(
            self.settings.colab_bin,
            *args,
            **exec_kwargs,
        )
        if job_id:
            self._active_procs[job_id] = proc

        collected: list[str] = []
        try:
            async def _read_stream() -> None:
                assert proc.stdout is not None
                with log_file.open("a", encoding="utf-8") as lf:
                    while True:
                        line_bytes = await proc.stdout.readline()
                        if not line_bytes:
                            break
                        line = redact_proxy_credentials(
                            line_bytes.decode("utf-8", errors="replace")
                        )
                        collected.append(line)
                        if job_id:
                            runtime_match = RUNTIME_MARKER_RE.match(line.strip())
                            if runtime_match:
                                runtime = sanitize_runtime_info(runtime_match.group(1))
                                if runtime:
                                    self.db.update_job(job_id, runtime_info_json=json.dumps(runtime))
                            gpu_match = GPU_MARKER_RE.search(line)
                            if gpu_match:
                                self.db.update_job(
                                    job_id,
                                    gpu_detected=gpu_match.group(1).strip()[:120],
                                )
                        if not quiet:
                            lf.write(line)
                            lf.flush()

            await asyncio.wait_for(_read_stream(), timeout=timeout)
            rc = await asyncio.wait_for(proc.wait(), timeout=15.0)
            return rc, "".join(collected)
        except asyncio.TimeoutError:
            await self._terminate_proc(proc, force=True)
            raise
        finally:
            try:
                # Stream decoding/read errors and cancellation must not leave a
                # CLI process alive after its account lease is released.
                if proc.returncode is None:
                    await self._terminate_proc(proc, force=True)
                    await proc.communicate()
            finally:
                if job_id:
                    self._active_procs.pop(job_id, None)

    @staticmethod
    def _append_log(log_file: Path, message: str) -> None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(message.rstrip("\n") + "\n")

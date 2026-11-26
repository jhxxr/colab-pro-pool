"""Cached, non-interactive account CU queries through the official Colab CLI."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
import time
from pathlib import Path
from typing import Any

from .oauth_store import oauth_token_path
from .pool_manager import AccountPoolManager
from .resources import record_usage_observation


class UsageQueryError(ValueError):
    """A safe, user-facing error, without raw CLI output or credentials."""


def parse_usage_output(output: str) -> tuple[float, float, int]:
    """Parse `colab usage`'s labeled lines, ignoring upgrade banners and ANSI."""
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    patterns = (
        r"^Current balance:\s*([-+]?\d+(?:\.\d+)?)\s+compute units\s*$",
        r"^Usage rate:\s*(\d+(?:\.\d+)?)/hr\s*$",
        r"^Active assignments:\s*(\d+)\s*$",
    )
    values = [re.findall(pattern, clean, flags=re.MULTILINE) for pattern in patterns]
    if any(len(matches) != 1 for matches in values):
        raise UsageQueryError("未能识别 CU 查询结果，请检查或升级 google-colab-cli。")
    balance, rate = float(values[0][0]), float(values[1][0])
    if not math.isfinite(balance) or not math.isfinite(rate):
        raise UsageQueryError("Colab 返回了无效的 CU 数据，请稍后刷新。")
    return balance, rate, int(values[2][0])


class AccountUsageManager:
    """Dedupe per-slot requests, bound concurrency, and keep GET responses fast."""

    def __init__(self, pool: AccountPoolManager) -> None:
        self.pool = pool
        self.settings = pool.settings
        self.db = pool.db
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancelling: set[str] = set()
        self._semaphore = asyncio.Semaphore(2)
        self._stopped = False
        self._stop_event = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._stopped = False
            self._stop_event.clear()
            self._loop_task = asyncio.create_task(self._refresh_loop())

    async def _refresh_loop(self) -> None:
        while not self._stopped:
            self.refresh_all()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    def enrich_account(self, account: dict[str, Any]) -> dict[str, Any]:
        result = self.pool.enrich_account(account)
        task = self._tasks.get(account["name"])
        result["usage"]["refreshing"] = task is not None and not task.done()
        return result

    def list_accounts(self) -> list[dict[str, Any]]:
        return [self.enrich_account(account) for account in self.db.list_accounts()]

    def refresh_all(self, *, force: bool = False) -> None:
        for account in self.db.list_accounts():
            self.refresh(account["name"], force=force)

    def refresh(self, name: str, *, force: bool = False) -> None:
        if self._stopped or name in self._tasks or name in self._cancelling:
            return
        account = self.db.get_account(name)
        if account is None or account["status"] == "maintenance":
            return
        if not self.pool.detect_auth_configured(account["home_dir"]):
            return
        # Manual requests can bypass the normal cache, but not hammer Google.
        interval = 5.0 if force else max(5.0, self.settings.usage_cache_seconds)
        checked = account["cu_checked_at"]
        newly_started = False
        if account["status"] == "busy" and account["current_job_id"]:
            job = self.db.get_job(account["current_job_id"])
            detected_at = job.get("runtime_info", {}).get("detected_at", 0) if job else 0
            newly_started = detected_at > (checked or 0)
        if not newly_started and checked is not None and time.time() - checked < interval:
            return
        task = asyncio.create_task(self._refresh(account))
        self._tasks[name] = task
        task.add_done_callback(lambda done: self._finished(name, done))

    def _finished(self, name: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(name) is task:
            self._tasks.pop(name, None)
        if not task.cancelled():
            task.exception()  # Retrieve exceptions even if the HTTP caller left.

    @staticmethod
    def _check_credentials(home: str) -> None:
        path = oauth_token_path(home)
        if not path.is_file():
            path = Path(home) / ".config" / "colab-cli" / "token.json"
        try:
            token = json.loads(path.read_text(encoding="utf-8"))
            valid = isinstance(token, dict) and all(
                token.get(key) for key in ("refresh_token", "client_id", "client_secret")
            )
        except (OSError, ValueError):
            valid = False
        if not valid:
            raise UsageQueryError("未找到可用的账号凭据，请先完成网页授权。")

    async def _refresh(self, account: dict[str, Any]) -> None:
        async with self._semaphore:
            # Queued queries must not race newly started authorization/deletion.
            current = self.db.get_account(account["name"])
            if (current is None or current["status"] == "maintenance"
                    or current["cu_generation"] != account["cu_generation"]
                    or current["home_dir"] != account["home_dir"]):
                return
            try:
                self._check_credentials(account["home_dir"])
                queried_at = time.time()
                balance, rate, count = parse_usage_output(
                    await self._query_cli(account["home_dir"])
                )
            except UsageQueryError as exc:
                self.db.save_account_usage(
                    account["name"], account["home_dir"], account["cu_generation"],
                    checked_at=time.time(), error=str(exc),
                )
            else:
                self.db.save_account_usage(
                    account["name"], account["home_dir"], account["cu_generation"],
                    checked_at=time.time(), balance=balance,
                    consumption_rate=rate, active_assignments=count,
                )
                record_usage_observation(
                    self.db, account["name"], account["cu_generation"], queried_at=queried_at
                )

    async def _query_cli(self, home: str) -> str:
        env = self.pool.build_cli_env(home)
        env["NO_COLOR"] = "1"
        env["COLAB_CLI_REQUEST_TIMEOUT"] = str(min(15.0, self.settings.usage_timeout_seconds))
        launch = asyncio.create_task(asyncio.create_subprocess_exec(
            self.settings.colab_bin, "--auth", "oauth2", "usage",
            env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name != "nt",
        ))
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.shield(launch)
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.settings.usage_timeout_seconds
            )
            output = stdout.decode("utf-8", "replace")
            if proc.returncode:
                low = (output + stderr.decode("utf-8", "replace")).lower()
                if "no such command" in low:
                    message = "当前 Colab CLI 不支持 CU 查询，请升级 google-colab-cli。"
                elif any(s in low for s in ("invalid_grant", "unauthenticated", "authorization code", "authorize colab-cli")):
                    message = "账号授权已失效，请重新进行网页授权。"
                elif any(s in low for s in ("forbidden", "permission", "scope", "403")):
                    message = "无权读取 CU，请重新授权并勾选全部请求的权限。"
                else:
                    message = "暂时无法查询 CU，请检查服务端网络后重试。"
                raise UsageQueryError(message)
            return output
        except FileNotFoundError as exc:
            raise UsageQueryError("未找到 Colab CLI，请先安装 google-colab-cli。") from exc
        except asyncio.TimeoutError as exc:
            raise UsageQueryError("CU 查询超时，请稍后刷新。") from exc
        except OSError as exc:
            raise UsageQueryError("无法运行 Colab CLI，请检查服务端安装配置。") from exc
        finally:
            # A cancellation during process creation still needs to reap its child.
            if proc is None:
                try:
                    proc = await launch
                except (OSError, asyncio.CancelledError):
                    pass
            if proc is not None and proc.returncode is None:
                await self._terminate(proc)
                await proc.communicate()

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> None:
        if os.name == "nt":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            except OSError:
                pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def cancel(self, name: str) -> None:
        # Keep polling clients from launching a replacement during teardown.
        self._cancelling.add(name)
        try:
            task = self._tasks.get(name)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if self._tasks.get(name) is task:
                    self._tasks.pop(name, None)
        finally:
            self._cancelling.discard(name)

    async def stop(self) -> None:
        self._stopped = True
        self._stop_event.set()
        if self._loop_task is not None:
            await self._loop_task
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

"""Bounded web sessions bridging the official CLI's remote OAuth prompt."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import signal
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .config import PoolSettings
from .oauth_store import oauth_token_path, write_oauth_token
from .pool_manager import AccountPoolManager, VALID_TIERS
from .utils import SAFE_NAME_RE


ACTIVE_STATUSES = {"starting", "awaiting_code", "verifying"}
REMOTE_REDIRECT_URI = "https://sdk.cloud.google.com/applicationdefaultauthcode.html"
GOOGLE_URL_RE = re.compile(r'https://accounts\.google\.com/[^\s<>"\x27]+')
REQUIRED_SCOPES = {
    "https://www.googleapis.com/auth/colaboratory",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.file",
}


class AuthSessionError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class AuthSession:
    session_id: str
    name: str
    tier: str
    created_at: float
    expires_at: float
    reserved: bool
    status: str = "starting"
    authorization_url: str | None = None
    error: str = ""
    account: dict[str, Any] | None = None
    task: asyncio.Task[None] | None = None
    process: asyncio.subprocess.Process | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    output_tail: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "tier": self.tier,
            "status": self.status,
            "authorization_url": self.authorization_url,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "error": self.error,
            "account": self.account,
        }


class AccountAuthManager:
    def __init__(
        self,
        settings: PoolSettings,
        pool: AccountPoolManager,
        on_change: Callable[[], None],
    ) -> None:
        self.settings = settings
        self.pool = pool
        self.on_change = on_change
        self._sessions: dict[str, AuthSession] = {}
        self._by_name: dict[str, str] = {}

    def is_authorizing(self, name: str) -> bool:
        return name in self._by_name

    def start(self, name: str, tier: str) -> dict[str, Any]:
        if not SAFE_NAME_RE.fullmatch(name) or len(name) > 64:
            raise AuthSessionError(400, "账号名称限 1–64 位字母、数字、下划线或连字符。")
        tier = tier.strip().lower()
        if tier not in VALID_TIERS:
            raise AuthSessionError(400, "订阅等级必须为 free、pro 或 pro+。")
        target = (self.settings.accounts_dir / name).resolve()
        if target.parent != self.settings.accounts_dir.resolve():
            raise AuthSessionError(400, "账号目录不能指向账号池目录之外。")
        if self.is_authorizing(name):
            raise AuthSessionError(409, "该账号已有进行中的授权，请完成或取消后重试。")
        if len(self._by_name) >= 8:
            raise AuthSessionError(429, "同时授权的账号过多，请稍后重试。")

        # Completed sessions contain no codes/tokens and are only retained for
        # polling. Bound their memory usage even if clients never poll again.
        finished = [s for s in self._sessions.values() if s.status not in ACTIVE_STATUSES]
        for session in finished[:-32]:
            self._sessions.pop(session.session_id, None)

        self.pool.sync_accounts_from_disk()
        existing = self.pool.db.get_account(name)
        reserved = existing is not None
        if reserved and not self.pool.db.acquire_account_for_auth(name):
            raise AuthSessionError(409, "该账号正在运行任务或维护，请稍后再授权。")

        now = time.time()
        session = AuthSession(
            session_id=secrets.token_urlsafe(24), name=name, tier=tier,
            created_at=now, expires_at=now + self.settings.auth_timeout_seconds,
            reserved=reserved,
        )
        self._sessions[session.session_id] = session
        self._by_name[name] = session.session_id
        session.task = asyncio.create_task(self._run(session))
        return session.public()

    def _get(self, session_id: str) -> AuthSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise AuthSessionError(404, "授权会话不存在或服务已重启，请重新发起授权。")
        return session

    def get(self, session_id: str) -> dict[str, Any]:
        return self._get(session_id).public()

    async def submit_code(self, session_id: str, code: str) -> dict[str, Any]:
        session = self._get(session_id)
        if session.status != "awaiting_code":
            raise AuthSessionError(409, "当前会话不能提交授权码，请检查授权状态。")
        if time.time() >= session.expires_at:
            raise AuthSessionError(409, "授权链接已过期，请重新发起授权。")
        if (
            len(code) > 4096 or any(c in code for c in "\r\n\x00")
            or not re.fullmatch(r"[\x21-\x7e]+", code.strip())
        ):
            raise AuthSessionError(400, "请粘贴完整的单行 Google 授权码。")
        proc = session.process
        if proc is None or proc.returncode is not None or proc.stdin is None:
            raise AuthSessionError(409, "授权进程已结束，请重新发起授权。")
        session.status = "verifying"
        try:
            proc.stdin.write((code.strip() + "\n").encode("utf-8"))
            await asyncio.wait_for(proc.stdin.drain(), timeout=5.0)
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError) as exc:
            await self.cancel(session_id)
            raise AuthSessionError(409, "授权进程连接中断，请重新发起授权。") from exc
        return session.public()

    async def cancel(self, session_id: str) -> dict[str, Any]:
        session = self._get(session_id)
        if session.status in ACTIVE_STATUSES and session.task is not None:
            session.task.cancel()
            await asyncio.gather(session.task, return_exceptions=True)
            # A task can be cancelled before its coroutine enters its finally.
            self._finish(session, "cancelled")
        return session.public()

    async def stop(self) -> None:
        sessions = [s for s in self._sessions.values() if s.status in ACTIVE_STATUSES]
        for session in sessions:
            if session.task is not None:
                session.task.cancel()
        await asyncio.gather(
            *(s.task for s in sessions if s.task is not None), return_exceptions=True
        )
        for session in sessions:
            self._finish(session, "cancelled")

    def _finish(self, session: AuthSession, status: str) -> None:
        if session.reserved:
            self.pool.db.release_account_from_cleanup(session.name)
            session.reserved = False
        if self._by_name.get(session.name) == session.session_id:
            self._by_name.pop(session.name, None)
            self.on_change()
        if session.status in ACTIVE_STATUSES:
            session.status = status
        session.authorization_url = None
        session.output_tail = ""
        session.process = None

    async def _run(self, session: AuthSession) -> None:
        status = "failed"
        temporary: tempfile.TemporaryDirectory | None = None
        try:
            parent = self.settings.data_dir / ".auth"
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = tempfile.TemporaryDirectory(prefix="oauth-", dir=parent)
            home = Path(temporary.name)
            # Respect a slot-specific OAuth client config, without copying any
            # cached token: reauthorization must always produce a fresh link.
            custom_config = self.settings.accounts_dir / session.name / ".colab-cli-oauth-config.json"
            if custom_config.is_file():
                (home / custom_config.name).write_bytes(custom_config.read_bytes())
            await asyncio.wait_for(
                self._authenticate(session, home),
                timeout=max(0.01, session.expires_at - time.time()),
            )
            status = "succeeded"
        except asyncio.CancelledError:
            status = "cancelled"
        except asyncio.TimeoutError:
            status = "expired"
            session.error = "授权会话已超时，请重新生成链接后再试。"
        except FileNotFoundError:
            session.error = "服务器未找到 Colab CLI，请安装 google-colab-cli 并检查 CLI 路径。"
        except AuthSessionError as exc:
            session.error = str(exc)
        except Exception:
            # CLI stderr and exceptions may include OAuth codes or tokens.
            # Expose only controlled messages; never return raw subprocess logs.
            session.error = "授权未完成，请检查服务器网络与 Colab CLI 配置后重试。"
        finally:
            try:
                await self._stop_process(session.process)
            finally:
                try:
                    if temporary is not None:
                        temporary.cleanup()
                finally:
                    self._finish(session, status)

    async def _authenticate(self, session: AuthSession, home: Path) -> None:
        proc = await asyncio.create_subprocess_exec(
            self.settings.colab_bin, "--auth", "oauth2", "sessions",
            env=self.pool.build_cli_env(str(home)),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        session.process = proc
        output_task = asyncio.create_task(self._read_output(session, proc))
        try:
            try:
                await asyncio.wait_for(
                    session.ready.wait(), timeout=self.settings.auth_startup_timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                raise AuthSessionError(504, "生成授权链接超时，请检查 Colab CLI 后重试。") from exc
            await output_task
            returncode = await proc.wait()
            if returncode != 0 or session.status != "verifying":
                low = session.output_tail.lower()
                if "invalid_grant" in low:
                    message = "授权码无效、已使用或已过期，请重新生成链接并获取新的授权码。"
                elif "scope" in low:
                    message = "授权权限不完整，请重新授权并勾选全部请求的权限。"
                else:
                    message = "Colab CLI 未完成授权，请检查网络与 CLI 配置后重试。"
                raise AuthSessionError(400, message)

            # Newer CLI versions use our keyring; older versions use token.json.
            path = oauth_token_path(home)
            if not path.is_file():
                path = home / ".config" / "colab-cli" / "token.json"
            if not path.is_file():
                raise AuthSessionError(400, "未保存授权凭据，请检查 CLI 钥匙串配置后重试。")
            raw = path.read_text(encoding="utf-8")
            token = json.loads(raw)
            if not all(token.get(k) for k in ("refresh_token", "client_id", "client_secret")):
                raise AuthSessionError(400, "未取得可续期的授权凭据，请重新授权。")
            scopes = token.get("scopes", [])
            scopes = scopes.split() if isinstance(scopes, str) else scopes
            if not REQUIRED_SCOPES.issubset(set(scopes)):
                raise AuthSessionError(400, "授权权限不完整，请重新授权并勾选全部请求的权限。")

            target = (self.settings.accounts_dir / session.name).resolve()
            if target.parent != self.settings.accounts_dir.resolve():
                raise AuthSessionError(400, "账号目录不能指向账号池目录之外。")
            write_oauth_token(target, raw)
            if session.reserved:
                self.pool.update_account(session.name, tier=session.tier, clear_cooldown=True)
            else:
                self.pool.register_account_dir(session.name, session.tier)
            self.pool.db.invalidate_account_usage(session.name)
            # Release before enriching so the returned account is already idle.
            if session.reserved:
                self.pool.db.release_account_from_cleanup(session.name)
                session.reserved = False
            account = self.pool.db.get_account(session.name)
            session.account = self.pool.enrich_account(account) if account else None
        finally:
            output_task.cancel()
            await asyncio.gather(output_task, return_exceptions=True)

    async def _read_output(
        self, session: AuthSession, proc: asyncio.subprocess.Process
    ) -> None:
        assert proc.stdout is not None
        pending = b""
        try:
            while chunk := await proc.stdout.read(4096):
                pending += chunk
                session.output_tail = (session.output_tail + chunk.decode("utf-8", "replace"))[-8192:]
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    for match in GOOGLE_URL_RE.finditer(line.decode("utf-8", "replace")):
                        url = match.group(0)
                        parsed = urlsplit(url)
                        params = parse_qs(parsed.query)
                        if (
                            parsed.scheme == "https" and parsed.netloc == "accounts.google.com"
                            and params.get("redirect_uri") == [REMOTE_REDIRECT_URI]
                            and params.get("state")
                        ):
                            session.authorization_url = url
                            if session.status == "starting":
                                session.status = "awaiting_code"
                            session.ready.set()
                if len(pending) > 65536:
                    raise AuthSessionError(400, "CLI 输出格式异常，请检查 CLI 版本。")
        finally:
            # Wake startup even when the process exits without an OAuth URL.
            session.ready.set()

    @staticmethod
    async def _stop_process(proc: asyncio.subprocess.Process | None) -> None:
        if proc is None or proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=10)
                if killer.returncode:
                    raise OSError("Could not terminate CLI process tree.")
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, asyncio.TimeoutError):
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        await asyncio.wait_for(proc.wait(), timeout=10)

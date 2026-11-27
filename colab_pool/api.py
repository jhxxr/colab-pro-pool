"""FastAPI HTTP REST server and Web Console host for Multi-Account Colab Pro Pool."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import inspect
import json
import mimetypes
import shutil
import tarfile
import time
import uuid
import zlib
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, ValidationError

from . import __version__
from .account_auth import AccountAuthManager, AuthSessionError
from .account_usage import AccountUsageManager
from .access_keys import KeyPolicyError, McpKeyStore, normalize_models, require_key_job
from .config import PoolSettings
from .db import AccountBusyError, PoolDatabase
from .pool_manager import AccountPoolManager
from .proxy_config import proxy_summary, validate_proxy_url
from .runner import ColabJobScheduler
from .resources import build_resource_catalog
from .utils import (
    SAFE_NAME_RE,
    dir_size_bytes,
    read_log_from_offset,
    read_log_tail,
    safe_extract_tar,
    safe_join,
)


class InlineFileItem(BaseModel):
    path: str = Field(..., description="Relative file path inside workspace, e.g. 'utils/model.py'")
    content: str = Field(..., description="UTF-8 text or base64 content")
    encoding: str = Field("utf-8", description="'utf-8' or 'base64'")


class JobSubmitRequest(BaseModel):
    name: str = Field("colab-exp", description="Human/agent readable experiment name")
    gpu: str = Field("T4", description="GPU type: T4, L4, A100, H100 (Pro+), G4 (Pro+), or CPU")
    tpu: str = Field("", description="Optional TPU type (e.g. v5e1, v6e1); overrides gpu if set")
    high_mem: bool = Field(False, description="Request high-RAM runtime profile")
    priority: int = Field(0, ge=-10, le=100, description="Scheduling priority (higher runs first)")
    entrypoint: str = Field("train.py", description="Main .py or .ipynb file to run")
    script_content: str | None = Field(
        None, description="UTF-8 code for `entrypoint` (convenient for direct AI agent submission)"
    )
    extra_files: list[InlineFileItem] = Field(
        default_factory=list, description="Additional project files uploaded inline as JSON"
    )
    workspace_tar_gz_b64: str | None = Field(
        None, description="Optional base64-encoded .tar.gz archive extracted into workspace"
    )
    script_args: list[str] = Field(default_factory=list, description="CLI arguments passed to entrypoint")
    pip_packages: list[str] = Field(
        default_factory=list, description="Python packages to install before running"
    )
    dataset_commands: list[str] = Field(
        default_factory=list,
        description="Shell commands run inside Colab before training (e.g. wget/curl/hf/gdown public datasets)",
    )
    env_vars: dict[str, str] = Field(
        default_factory=dict, description="Environment variables injected into training process"
    )
    drive_profile: str = Field(
        "",
        description="Name of pre-authorized rclone Drive profile on server (mounts to /content/drive/MyDrive)",
    )
    drive_subpath: str = Field(
        "", description="Optional subfolder in Google Drive to scope mount/sync"
    )
    drive_sync_outputs_to: str = Field(
        "",
        description="Optional Google Drive folder path where /content/outputs is copied after training",
    )
    timeout_seconds: int = Field(43200, ge=60, le=86400, description="Max runtime seconds (default 12h)")


class JobRetryRequest(BaseModel):
    name: str | None = None
    gpu: str | None = None
    tpu: str | None = None
    high_mem: bool | None = None
    priority: int | None = Field(default=None, ge=-10, le=100)
    timeout_seconds: int | None = Field(default=None, ge=60, le=86400)


class AccountRegisterRequest(BaseModel):
    name: str = Field(..., description="Account slot identifier, e.g. 'pro_01'")
    tier: str = Field("pro", description="Subscription tier: 'pro', 'pro+', or 'free'")
    proxy_id: str | None = Field(default=None, description="Optional assigned proxy ID")


class ProxyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    url: SecretStr = Field(..., description="HTTP(S) proxy URL, optionally with credentials")


class ProxyUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    url: SecretStr | None = Field(default=None, description="Replace proxy URL when provided")


class AccountResetRequest(BaseModel):
    enabled: bool = Field(True, description="Enable or disable this account slot")


class McpKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    cu_limit: float = Field(..., description="Finite, non-negative lifetime CU allowance")
    allowed_models: list[str] = Field(...)


class McpKeyUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    cu_limit: float | None = Field(None, description="Finite, non-negative lifetime CU allowance")
    allowed_models: list[str] | None = None
    enabled: bool | None = None


class AccountAuthCodeRequest(BaseModel):
    code: SecretStr = Field(..., description="One-time code from the Google authorization page")


class AccountUpdateRequest(BaseModel):
    tier: str | None = Field(default=None, description="Subscription tier: 'pro', 'pro+', or 'free'")
    enabled: bool | None = Field(default=None, description="Enable or disable this account slot")
    clear_cooldown: bool = Field(default=False, description="Clear cooldown and error state")
    proxy_id: str | None = Field(default=None, description="Assigned proxy ID; null removes assignment")


def _extract_token(authorization: str | None, x_api_key: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    if x_api_key:
        return x_api_key.strip()
    return ""


def _model_fields_set(model: BaseModel) -> set[str]:
    fields = getattr(model, "model_fields_set", None)
    return fields if fields is not None else getattr(model, "__fields_set__", set())


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    # In particular, an account's baseline balance is not the client's balance.
    private = {"cu_meter_at", "cu_rate", "cu_baseline_balance", "cu_session_used"}
    return {name: value for name, value in job.items() if name not in private}


def _build_job_record(
    job_id: str,
    req: JobSubmitRequest,
    ws_dir: Path,
    log_path: Path,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "name": req.name,
        "status": "queued",
        "gpu": req.gpu.upper(),
        "tpu": req.tpu,
        "high_mem": 1 if req.high_mem else 0,
        "priority": req.priority,
        "entrypoint": req.entrypoint,
        "script_args_json": json.dumps(req.script_args),
        "pip_packages_json": json.dumps(req.pip_packages),
        "dataset_commands_json": json.dumps(req.dataset_commands),
        "env_vars_json": json.dumps(req.env_vars),
        "drive_profile": req.drive_profile,
        "drive_subpath": req.drive_subpath,
        "drive_sync_outputs_to": req.drive_sync_outputs_to,
        "timeout_seconds": req.timeout_seconds,
        "workspace_dir": str(ws_dir),
        "log_path": str(log_path),
        "created_at": time.time(),
    }


def _resolve_static_dir() -> Path:
    pkg_static = Path(__file__).resolve().parent / "static"
    if (pkg_static / "index.html").is_file():
        return pkg_static
    dev_static = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    return dev_static


def create_app(settings: PoolSettings | None = None) -> FastAPI:
    cfg = settings or PoolSettings.from_env()
    cfg.ensure_dirs()
    db = PoolDatabase(cfg.db_path)
    keys = McpKeyStore(db)
    pool = AccountPoolManager(cfg, db)
    scheduler = ColabJobScheduler(cfg, db, pool)
    account_auth = AccountAuthManager(cfg, pool, scheduler.notify_new_job)
    account_usage = AccountUsageManager(pool)

    async def shutdown() -> None:
        try:
            await account_usage.stop()
        finally:
            try:
                await account_auth.stop()
            finally:
                await scheduler.stop()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await scheduler.start()
        await account_usage.start()
        try:
            yield
        finally:
            await shutdown()

    fastapi_kwargs: dict[str, Any] = {
        "title": "Colab Pro Pool Orchestrator API",
        "version": __version__,
        "description": (
            "Multi-account Google Colab Pro GPU/TPU scheduler for AI Agents. "
            "Supports automatic Pro account leasing & failover, non-interactive rclone "
            "Google Drive mounting, public dataset bootstrap hooks, and artifact retrieval."
        ),
    }
    supports_lifespan = "lifespan" in inspect.signature(FastAPI.__init__).parameters
    if supports_lifespan:
        fastapi_kwargs["lifespan"] = lifespan

    app = FastAPI(**fastapi_kwargs)
    if not supports_lifespan:
        app.add_event_handler("startup", scheduler.start)
        app.add_event_handler("startup", account_usage.start)
        app.add_event_handler("shutdown", shutdown)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.settings = cfg
    app.state.db = db
    app.state.pool = pool
    app.state.scheduler = scheduler
    app.state.account_auth = account_auth
    app.state.account_usage = account_usage
    app.state.mcp_keys = keys

    def _is_token_valid(token: str) -> bool:
        if not cfg.api_key:
            return not keys.list() and not token.startswith("cpool_")
        return hmac.compare_digest(
            token.encode("utf-8"), cfg.api_key.encode("utf-8")
        )

    def verify_api_key(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> None:
        token = _extract_token(authorization, x_api_key)
        if not _is_token_valid(token):
            if keys.authenticate(token):
                raise HTTPException(status_code=403, detail="此操作需要管理员密钥。")
            raise HTTPException(status_code=401, detail="Invalid or missing API key.")

    def verify_job_key(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any] | None:
        token = _extract_token(authorization, x_api_key)
        # Even in legacy no-auth mode an explicitly supplied MCP key remains
        # restricted. Once issued keys exist, anonymous access is closed.
        key = keys.authenticate(token)
        if key:
            return key
        if cfg.api_key and _is_token_valid(token):
            return None
        if not cfg.api_key and not token and not keys.list():
            return None
        raise HTTPException(status_code=401, detail="Invalid, disabled or missing API key.")

    def verify_owned_job(job_id: str, key: dict[str, Any] | None = Depends(verify_job_key)) -> None:
        job = db.get_job(job_id)
        if job is None or (key and job.get("mcp_key_id") != key["key_id"]):
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    def _check_job_policy(req: JobSubmitRequest, key: dict[str, Any] | None) -> None:
        try:
            if key:
                require_key_job(keys.get(key["key_id"]), {
                    "gpu": req.gpu, "tpu": req.tpu, "drive_profile": req.drive_profile,
                })
                model = normalize_models([req.tpu or req.gpu])[0]
                if bool(req.tpu) != (model in {"v5e1", "v6e1"}):
                    raise ValueError("TPU 型号请通过 tpu 字段指定，gpu 字段仅支持 GPU 或 CPU。")
                if req.tpu:
                    req.tpu, req.gpu = model, "CPU"
                else:
                    req.gpu = model
        except KeyPolicyError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _create_job(record: dict[str, Any], key: dict[str, Any] | None) -> dict[str, Any]:
        record["mcp_key_id"] = key["key_id"] if key else None
        try:
            return db.create_job(record)
        except KeyPolicyError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    def _safe_write_rel(root: Path, rel_path: str, data: bytes) -> Path:
        try:
            dest = safe_join(root, rel_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def _validate_drive_profile(profile: str) -> None:
        if not profile:
            return
        if not SAFE_NAME_RE.match(profile):
            raise HTTPException(
                status_code=400,
                detail="Drive profile must contain only letters, digits, underscores, or hyphens.",
            )
        conf_file = cfg.drives_dir / f"{profile}.conf"
        if not conf_file.exists():
            raise HTTPException(
                status_code=400,
                detail=f"Drive profile '{profile}' does not exist in {cfg.drives_dir}.",
            )

    def _validate_proxy_name(name: str) -> str:
        normalized = name.strip()
        if not normalized or any(ord(char) < 32 for char in normalized):
            raise HTTPException(status_code=400, detail="代理名称不能为空或包含控制字符。")
        return normalized

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        stats = db.get_stats()
        acct = stats["accounts"]
        jobs = stats["jobs"]
        return {
            "status": "ok",
            "version": __version__,
            "accounts_total": acct["total"],
            "accounts_idle": acct["idle"],
            "accounts_busy": acct["busy"],
            "accounts_cooldown": acct["cooldown"],
            "jobs_queued": jobs["queued"],
            "jobs_running": jobs["running"],
            "jobs_provisioning": jobs["provisioning"],
        }

    @app.get("/api/v1/auth/check")
    async def auth_check(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        token = _extract_token(authorization, x_api_key)
        return {
            "auth_required": bool(cfg.api_key or keys.list()),
            "authenticated": _is_token_valid(token),
        }

    @app.get("/api/v1/mcp-keys", dependencies=[Depends(verify_api_key)])
    async def list_mcp_keys() -> dict[str, Any]:
        return {"keys": keys.list()}

    @app.post("/api/v1/mcp-keys", status_code=201, dependencies=[Depends(verify_api_key)])
    async def create_mcp_key(req: McpKeyCreateRequest) -> dict[str, Any]:
        if not cfg.api_key:
            raise HTTPException(status_code=409, detail="请先设置 COLAB_POOL_API_KEY 管理员密钥，再创建 MCP 密钥。")
        try:
            return keys.create(req.name, req.cu_limit, req.allowed_models)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/api/v1/mcp-keys/{key_id}", dependencies=[Depends(verify_api_key)])
    async def update_mcp_key(key_id: str, req: McpKeyUpdateRequest) -> dict[str, Any]:
        patch = {name: getattr(req, name) for name in _model_fields_set(req)}
        if any(value is None for value in patch.values()):
            raise HTTPException(status_code=400, detail="密钥设置不能为 null。")
        try:
            result = keys.update(key_id, patch)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="MCP 密钥不存在。")
        scheduler.notify_new_job()
        return result

    @app.get("/api/v1/mcp/status")
    async def mcp_status(key: dict[str, Any] | None = Depends(verify_job_key)) -> dict[str, Any]:
        if key is None:
            return {"accounts": account_usage.list_accounts(), "drive_profiles": pool.list_drive_profiles()}
        stats = db.get_stats()
        allowed = json.loads(key["allowed_models_json"])
        return {
            "key": keys.summary(keys.get(key["key_id"])),
            "allowed_models": allowed,
            "pool": {k: stats["accounts"][k] for k in ("idle", "busy", "cooldown")},
            "drive_profiles": [],
            "metering_note": "按官方 CU/h 和运行时长估算，按账号余额减少向上校正；额度耗尽会停止任务，查询和停机有延迟，可能少量超额。",
        }

    @app.get("/api/v1/stats", dependencies=[Depends(verify_api_key)])
    async def get_pool_stats() -> dict[str, Any]:
        pool.sync_accounts_from_disk()
        stats = db.get_stats()
        stats["jobs_disk_bytes"] = dir_size_bytes(cfg.jobs_dir)
        stats["drive_profiles_count"] = len(pool.list_drive_profiles())
        return stats

    @app.get("/api/v1/accounts", dependencies=[Depends(verify_api_key)])
    async def list_accounts() -> dict[str, Any]:
        pool.sync_accounts_from_disk()
        account_usage.refresh_all()
        return {
            "accounts": account_usage.list_accounts(),
            "drive_profiles": pool.list_drive_profiles(),
        }

    @app.get("/api/v1/proxies", dependencies=[Depends(verify_api_key)])
    async def list_proxies() -> dict[str, Any]:
        # Database records contain proxy credentials; only expose safe summaries.
        return {"proxies": [proxy_summary(item) for item in db.list_proxies()]}

    @app.post("/api/v1/proxies", status_code=201, dependencies=[Depends(verify_api_key)])
    async def create_proxy(req: ProxyCreateRequest) -> dict[str, Any]:
        try:
            proxy_url = validate_proxy_url(req.url.get_secret_value())
            created = db.create_proxy(
                uuid.uuid4().hex, _validate_proxy_name(req.name), proxy_url
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return proxy_summary(created)

    @app.patch("/api/v1/proxies/{proxy_id}", dependencies=[Depends(verify_api_key)])
    async def update_proxy(proxy_id: str, req: ProxyUpdateRequest) -> dict[str, Any]:
        fields_set = _model_fields_set(req)
        name = _validate_proxy_name(req.name) if req.name is not None else None
        proxy_url: str | None = None
        if "url" in fields_set and req.url is not None:
            try:
                proxy_url = validate_proxy_url(req.url.get_secret_value())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        existing_proxy = db.get_proxy(proxy_id)
        if (
            proxy_url is not None and existing_proxy is not None
            and proxy_url != existing_proxy["proxy_url"]
            and account_auth.is_proxy_in_use(proxy_id)
        ):
            raise HTTPException(status_code=409, detail="该代理正用于账号授权，请稍后再修改地址。")
        try:
            updated = db.update_proxy(proxy_id, name=name, proxy_url=proxy_url)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail="代理不存在。")
        return proxy_summary(updated)

    @app.delete("/api/v1/proxies/{proxy_id}", dependencies=[Depends(verify_api_key)])
    async def delete_proxy(proxy_id: str) -> dict[str, Any]:
        if account_auth.is_proxy_in_use(proxy_id):
            raise HTTPException(status_code=409, detail="该代理正用于账号授权，请稍后再删除。")
        try:
            deleted = db.delete_proxy(proxy_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="代理不存在。")
        return {"deleted": proxy_id}

    @app.get("/api/v1/resources", dependencies=[Depends(verify_api_key)])
    async def list_resources() -> dict[str, Any]:
        pool.sync_accounts_from_disk()
        account_usage.refresh_all()
        return build_resource_catalog(db)

    @app.post("/api/v1/accounts", dependencies=[Depends(verify_api_key)])
    async def register_account(req: AccountRegisterRequest) -> dict[str, Any]:
        _ensure_not_authorizing(req.name)
        if req.proxy_id is not None and db.get_proxy(req.proxy_id) is None:
            raise HTTPException(status_code=400, detail="所选代理不存在，请刷新列表后重试。")
        try:
            acct = pool.register_account_dir(req.name, req.tier)
            if req.proxy_id is not None:
                acct = pool.update_account(req.name, proxy_id=req.proxy_id, set_proxy=True) or acct
        except AccountBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        scheduler.notify_new_job()
        return acct

    def _ensure_not_authorizing(name: str) -> None:
        if account_auth.is_authorizing(name):
            raise HTTPException(status_code=409, detail="该账号正在授权，请完成或取消后再修改。")

    @app.post("/api/v1/account-auth", status_code=202, dependencies=[Depends(verify_api_key)])
    async def start_account_auth(req: AccountRegisterRequest) -> dict[str, Any]:
        # A usage query can refresh credentials. Stop it before replacing them.
        await account_usage.cancel(req.name)
        try:
            fields_set = _model_fields_set(req)
            if "proxy_id" in fields_set:
                return account_auth.start(
                    req.name, req.tier, proxy_id=req.proxy_id,
                    reuse_existing_proxy=False,
                )
            return account_auth.start(req.name, req.tier)
        except AuthSessionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.get("/api/v1/account-auth/{session_id}", dependencies=[Depends(verify_api_key)])
    async def get_account_auth(session_id: str) -> dict[str, Any]:
        try:
            return account_auth.get(session_id)
        except AuthSessionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.post("/api/v1/account-auth/{session_id}/code", dependencies=[Depends(verify_api_key)])
    async def submit_account_auth_code(
        session_id: str, req: AccountAuthCodeRequest
    ) -> dict[str, Any]:
        try:
            return await account_auth.submit_code(session_id, req.code.get_secret_value())
        except AuthSessionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.delete("/api/v1/account-auth/{session_id}", dependencies=[Depends(verify_api_key)])
    async def cancel_account_auth(session_id: str) -> dict[str, Any]:
        try:
            return await account_auth.cancel(session_id)
        except AuthSessionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.post("/api/v1/accounts/{name}/reset", dependencies=[Depends(verify_api_key)])
    async def reset_account(name: str, req: AccountResetRequest) -> dict[str, Any]:
        _ensure_not_authorizing(name)
        updated = pool.reset_account(name, enabled=req.enabled)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Account '{name}' not found.")
        scheduler.notify_new_job()
        return updated

    @app.patch("/api/v1/accounts/{name}", dependencies=[Depends(verify_api_key)])
    async def update_account(name: str, req: AccountUpdateRequest) -> dict[str, Any]:
        _ensure_not_authorizing(name)
        fields_set = _model_fields_set(req)
        set_proxy = "proxy_id" in fields_set
        if set_proxy:
            current = db.get_account(name)
            if current is None:
                raise HTTPException(status_code=404, detail=f"Account '{name}' not found.")
            if req.proxy_id != current.get("proxy_id") and current.get("status") in {"busy", "maintenance"}:
                raise HTTPException(status_code=409, detail="账号正在运行或维护，暂不能更改代理。")
            if req.proxy_id is not None and db.get_proxy(req.proxy_id) is None:
                raise HTTPException(status_code=400, detail="所选代理不存在，请刷新列表后重试。")
        try:
            updated = pool.update_account(
                name,
                tier=req.tier,
                enabled=req.enabled,
                clear_cooldown=req.clear_cooldown,
                proxy_id=req.proxy_id,
                set_proxy=set_proxy,
            )
        except AccountBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Account '{name}' not found.")
        scheduler.notify_new_job()
        return updated

    @app.delete("/api/v1/accounts/{name}", dependencies=[Depends(verify_api_key)])
    async def delete_account(
        name: str, remove_disk: bool = Query(False)
    ) -> dict[str, Any]:
        _ensure_not_authorizing(name)
        await account_usage.cancel(name)
        try:
            deleted = pool.delete_account(name, remove_disk=remove_disk)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Account '{name}' not found.")
        return {"deleted": name}

    @app.post("/api/v1/pool/cleanup", dependencies=[Depends(verify_api_key)])
    async def cleanup_orphan_runtimes() -> dict[str, Any]:
        results = await pool.cleanup_orphan_runtimes(skip_busy=True)
        return {"results": results}

    @app.post("/api/v1/pool/sync", dependencies=[Depends(verify_api_key)])
    async def sync_pool_accounts() -> dict[str, Any]:
        pool.sync_accounts_from_disk()
        account_usage.refresh_all()
        return {"accounts": account_usage.list_accounts()}

    @app.post("/api/v1/accounts/{name}/usage/refresh", status_code=202,
              dependencies=[Depends(verify_api_key)])
    async def refresh_account_usage(name: str) -> dict[str, Any]:
        _ensure_not_authorizing(name)
        account = db.get_account(name)
        if account is None:
            raise HTTPException(status_code=404, detail=f"Account '{name}' not found.")
        account_usage.refresh(name, force=True)
        return account_usage.enrich_account(account)

    @app.post("/api/v1/pool/usage/refresh", status_code=202,
              dependencies=[Depends(verify_api_key)])
    async def refresh_pool_usage() -> dict[str, Any]:
        pool.sync_accounts_from_disk()
        account_usage.refresh_all(force=True)
        return {"accounts": account_usage.list_accounts()}

    @app.post("/api/v1/jobs")
    async def submit_job_json(req: JobSubmitRequest, key: dict[str, Any] | None = Depends(verify_job_key)) -> dict[str, Any]:
        _check_job_policy(req, key)
        _validate_drive_profile(req.drive_profile)

        job_id = uuid.uuid4().hex[:12]
        job_dir = cfg.jobs_dir / job_id
        ws_dir = job_dir / "workspace"
        log_path = job_dir / "job.log"

        try:
            ws_dir.mkdir(parents=True, exist_ok=True)

            if req.workspace_tar_gz_b64:
                try:
                    raw_tar = base64.b64decode(req.workspace_tar_gz_b64, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise HTTPException(
                        status_code=400, detail=f"Invalid base64 in workspace_tar_gz_b64: {exc}"
                    ) from exc
                try:
                    with tarfile.open(fileobj=BytesIO(raw_tar), mode="r:gz") as tf:
                        safe_extract_tar(tf, ws_dir)
                except (tarfile.TarError, ValueError, EOFError, zlib.error) as exc:
                    raise HTTPException(
                        status_code=400, detail=f"Invalid or unsafe tar archive: {exc}"
                    ) from exc

            if req.script_content is not None:
                _safe_write_rel(ws_dir, req.entrypoint, req.script_content.encode("utf-8"))

            for item in req.extra_files:
                if item.encoding.lower() == "base64":
                    try:
                        payload = base64.b64decode(item.content, validate=True)
                    except (binascii.Error, ValueError) as exc:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Invalid base64 in extra file '{item.path}': {exc}",
                        ) from exc
                else:
                    payload = item.content.encode("utf-8")
                _safe_write_rel(ws_dir, item.path, payload)

            try:
                entrypoint_path = safe_join(ws_dir, req.entrypoint)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            if not entrypoint_path.is_file():
                raise HTTPException(
                    status_code=400,
                    detail=f"Entrypoint '{req.entrypoint}' was not provided in script_content, extra_files, or workspace_tar_gz_b64.",
                )

            record = _build_job_record(job_id, req, ws_dir, log_path)
            created = _create_job(record, key)
        except HTTPException:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise

        scheduler.notify_new_job()
        return _public_job(created)

    @app.post("/api/v1/jobs/upload")
    async def submit_job_multipart(
        files: list[UploadFile] = File(..., description="Training script(s) or project files"),
        spec_json: str = Form("{}", description="JSON string matching JobSubmitRequest fields"),
        key: dict[str, Any] | None = Depends(verify_job_key),
    ) -> dict[str, Any]:
        try:
            spec_dict = json.loads(spec_json)
            if not isinstance(spec_dict, dict):
                raise ValueError("spec_json must decode to a JSON object.")
            req = JobSubmitRequest(**spec_dict)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid spec_json: {exc}") from exc

        _check_job_policy(req, key)
        _validate_drive_profile(req.drive_profile)

        job_id = uuid.uuid4().hex[:12]
        job_dir = cfg.jobs_dir / job_id
        ws_dir = job_dir / "workspace"
        log_path = job_dir / "job.log"

        try:
            ws_dir.mkdir(parents=True, exist_ok=True)
            for upload in files:
                fname = upload.filename or "train.py"
                data = await upload.read()
                if fname.endswith(".tar.gz") or fname.endswith(".tgz"):
                    try:
                        with tarfile.open(fileobj=BytesIO(data), mode="r:gz") as tf:
                            safe_extract_tar(tf, ws_dir)
                    except (tarfile.TarError, ValueError, EOFError, zlib.error) as exc:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Invalid or unsafe tar archive '{fname}': {exc}",
                        ) from exc
                else:
                    _safe_write_rel(ws_dir, fname, data)

            try:
                entrypoint_path = safe_join(ws_dir, req.entrypoint)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            if not entrypoint_path.is_file():
                raise HTTPException(
                    status_code=400,
                    detail=f"Entrypoint '{req.entrypoint}' missing among uploaded files.",
                )

            record = _build_job_record(job_id, req, ws_dir, log_path)
            created = _create_job(record, key)
        except HTTPException:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise

        scheduler.notify_new_job()
        return _public_job(created)

    @app.get("/api/v1/jobs")
    async def list_jobs(
        status: str | None = Query(None),
        gpu: str | None = Query(None),
        account: str | None = Query(None),
        search: str | None = Query(None),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        key: dict[str, Any] | None = Depends(verify_job_key),
    ) -> dict[str, Any]:
        jobs = db.list_jobs(
            status=status,
            gpu=gpu,
            account=account,
            search=search,
            limit=limit,
            offset=offset,
            mcp_key_id=key["key_id"] if key else None,
        )
        total = db.count_jobs(
            status=status,
            gpu=gpu,
            account=account,
            search=search,
            mcp_key_id=key["key_id"] if key else None,
        )
        return {
            "jobs": [_public_job(job) for job in jobs],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(verify_owned_job)])
    async def get_job(job_id: str) -> dict[str, Any]:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        outputs_dir = Path(job["workspace_dir"]).parent / "outputs"
        output_files: list[str] = []
        output_file_details: list[dict[str, Any]] = []
        if outputs_dir.exists():
            entries = sorted(
                (p for p in outputs_dir.rglob("*") if p.is_file()),
                key=lambda p: p.relative_to(outputs_dir).as_posix(),
            )
            for p in entries:
                rel = p.relative_to(outputs_dir).as_posix()
                output_files.append(rel)
                output_file_details.append({"path": rel, "size": p.stat().st_size})
        job["output_files"] = output_files
        job["output_file_details"] = output_file_details
        return _public_job(job)

    @app.get("/api/v1/jobs/{job_id}/logs", dependencies=[Depends(verify_owned_job)])
    async def get_job_logs(
        job_id: str,
        tail_lines: int = Query(200, ge=1, le=5000),
        offset: int | None = Query(None, ge=0),
    ) -> dict[str, Any]:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        log_path = Path(job["log_path"])
        if not log_path.exists():
            return {
                "job_id": job_id,
                "status": job["status"],
                "assigned_account": job["assigned_account"],
                "gpu_detected": job.get("gpu_detected", ""),
                "total_lines": 0,
                "next_offset": 0,
                "file_size": 0,
                "logs": "",
            }

        if offset is not None:
            text, next_offset, file_size = read_log_from_offset(log_path, offset=offset)
            returned_lines = len(text.splitlines()) if text else 0
        else:
            text, file_size, returned_lines = read_log_tail(log_path, tail_lines=tail_lines)
            next_offset = file_size

        return {
            "job_id": job_id,
            "status": job["status"],
            "assigned_account": job["assigned_account"],
            "gpu_detected": job.get("gpu_detected", ""),
            "total_lines": returned_lines,
            "next_offset": next_offset,
            "file_size": file_size,
            "logs": text,
        }

    @app.get("/api/v1/jobs/{job_id}/logs/stream", dependencies=[Depends(verify_owned_job)])
    async def stream_job_logs(
        job_id: str,
        offset: int = Query(0, ge=0),
    ) -> StreamingResponse:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

        async def event_generator():
            cursor = offset
            last_state: tuple[Any, ...] | None = None
            last_ping = time.monotonic()
            log_path = Path(job["log_path"])

            try:
                while True:
                    cur_job = db.get_job(job_id)
                    if cur_job is None:
                        yield f"event: done\ndata: {json.dumps({'status': 'deleted', 'offset': cursor})}\n\n"
                        return

                    while True:
                        chunk, next_off, _ = read_log_from_offset(log_path, offset=cursor)
                        if not chunk:
                            break
                        cursor = next_off
                        payload = json.dumps(
                            {"chunk": chunk, "offset": cursor}, ensure_ascii=False
                        )
                        yield f"event: log\ndata: {payload}\n\n"
                        last_ping = time.monotonic()

                    state = (
                        cur_job["status"],
                        cur_job["assigned_account"],
                        cur_job.get("gpu_detected", ""),
                        json.dumps(cur_job.get("runtime_info", {}), sort_keys=True),
                        cur_job.get("exit_code"),
                        cur_job.get("error_message", ""),
                    )
                    if state != last_state:
                        last_state = state
                        status_payload = json.dumps(
                            {
                                "status": cur_job["status"],
                                "assigned_account": cur_job["assigned_account"],
                                "gpu_detected": cur_job.get("gpu_detected", ""),
                                "runtime_info": cur_job.get("runtime_info", {}),
                                "exit_code": cur_job.get("exit_code"),
                                "error_message": cur_job.get("error_message", ""),
                            },
                            ensure_ascii=False,
                        )
                        yield f"event: status\ndata: {status_payload}\n\n"
                        last_ping = time.monotonic()

                    if cur_job["status"] in {"succeeded", "failed", "cancelled"}:
                        # Final flush check after terminal state is observed
                        while True:
                            chunk, next_off, _ = read_log_from_offset(log_path, offset=cursor)
                            if not chunk:
                                break
                            cursor = next_off
                            payload = json.dumps(
                                {"chunk": chunk, "offset": cursor}, ensure_ascii=False
                            )
                            yield f"event: log\ndata: {payload}\n\n"
                        done_payload = json.dumps(
                            {"status": cur_job["status"], "offset": cursor},
                            ensure_ascii=False,
                        )
                        yield f"event: done\ndata: {done_payload}\n\n"
                        return

                    now = time.monotonic()
                    if now - last_ping >= 15.0:
                        yield ": ping\n\n"
                        last_ping = now

                    await asyncio.sleep(0.35)
            except asyncio.CancelledError:
                return

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/v1/jobs/{job_id}/cancel", dependencies=[Depends(verify_owned_job)])
    async def cancel_job(job_id: str) -> dict[str, Any]:
        updated = await scheduler.cancel_job(job_id)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        return _public_job(updated)

    @app.post("/api/v1/jobs/{job_id}/retry", dependencies=[Depends(verify_owned_job)])
    async def retry_job(
        job_id: str,
        req: JobRetryRequest | None = Body(default=None),
    ) -> dict[str, Any]:
        orig = db.get_job(job_id)
        if orig is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        orig_ws = Path(orig["workspace_dir"])
        if not orig_ws.is_dir():
            raise HTTPException(
                status_code=400,
                detail=f"Original workspace for job '{job_id}' is missing on disk.",
            )

        overrides = req or JobRetryRequest()
        drive_profile = orig.get("drive_profile", "")
        _validate_drive_profile(drive_profile)
        owner = keys.get(orig["mcp_key_id"]) if orig.get("mcp_key_id") else None
        if orig.get("mcp_key_id"):
            policy_req = JobSubmitRequest(
                gpu=overrides.gpu if overrides.gpu is not None else orig["gpu"],
                tpu=overrides.tpu if overrides.tpu is not None else orig.get("tpu", ""),
                drive_profile=drive_profile,
            )
            if owner is None:
                raise HTTPException(status_code=403, detail="原任务的 MCP 密钥不存在。")
            _check_job_policy(policy_req, owner)

        new_job_id = uuid.uuid4().hex[:12]
        new_job_dir = cfg.jobs_dir / new_job_id
        new_ws = new_job_dir / "workspace"
        new_log = new_job_dir / "job.log"

        try:
            new_job_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(orig_ws, new_ws)
            record = {
                "job_id": new_job_id,
                "name": overrides.name or orig["name"],
                "status": "queued",
                "gpu": policy_req.gpu if owner else (overrides.gpu or orig["gpu"]).upper(),
                "tpu": policy_req.tpu if owner else (overrides.tpu if overrides.tpu is not None else orig.get("tpu", "")),
                "high_mem": 1 if (overrides.high_mem if overrides.high_mem is not None else orig["high_mem"]) else 0,
                "priority": overrides.priority if overrides.priority is not None else orig.get("priority", 0),
                "entrypoint": orig["entrypoint"],
                "script_args_json": json.dumps(orig.get("script_args", [])),
                "pip_packages_json": json.dumps(orig.get("pip_packages", [])),
                "dataset_commands_json": json.dumps(orig.get("dataset_commands", [])),
                "env_vars_json": json.dumps(orig.get("env_vars", {})),
                "drive_profile": drive_profile,
                "drive_subpath": orig.get("drive_subpath", ""),
                "drive_sync_outputs_to": orig.get("drive_sync_outputs_to", ""),
                "timeout_seconds": overrides.timeout_seconds if overrides.timeout_seconds is not None else orig.get("timeout_seconds", 43200),
                "workspace_dir": str(new_ws),
                "log_path": str(new_log),
                "created_at": time.time(),
            }
            created = _create_job(record, owner)
        except Exception:
            shutil.rmtree(new_job_dir, ignore_errors=True)
            raise

        scheduler.notify_new_job()
        return _public_job(created)

    @app.delete("/api/v1/jobs/{job_id}", dependencies=[Depends(verify_owned_job)])
    async def delete_job(job_id: str) -> dict[str, Any]:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        if job["status"] in {"provisioning", "running"}:
            raise HTTPException(
                status_code=409,
                detail="Cannot delete an active job; cancel it first.",
            )
        db.delete_job(job_id)
        base = cfg.jobs_dir.resolve()
        job_dir = Path(job["workspace_dir"]).parent.resolve()
        if job_dir != base and job_dir.is_relative_to(base) and job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
        return {"deleted": job_id}

    @app.get("/api/v1/jobs/{job_id}/artifacts", dependencies=[Depends(verify_owned_job)])
    async def download_job_artifacts(job_id: str) -> FileResponse:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        archive = job.get("artifacts_archive", "")
        if not archive or not Path(archive).exists():
            raise HTTPException(
                status_code=404,
                detail="No artifacts archive available for this job yet.",
            )
        return FileResponse(
            path=archive,
            media_type="application/gzip",
            filename=f"colab_job_{job_id}_outputs.tar.gz",
        )

    @app.get("/api/v1/jobs/{job_id}/files/{file_path:path}", dependencies=[Depends(verify_owned_job)])
    async def get_job_output_file(job_id: str, file_path: str) -> FileResponse:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        outputs_dir = Path(job["workspace_dir"]).parent / "outputs"
        if not outputs_dir.exists():
            raise HTTPException(status_code=404, detail="Outputs directory not found.")
        try:
            target = safe_join(outputs_dir, file_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"File '{file_path}' not found.")
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return FileResponse(path=target, media_type=media_type, filename=target.name)

    @app.get("/api/v1/jobs/{job_id}/workspace", dependencies=[Depends(verify_owned_job)])
    async def inspect_job_workspace(
        job_id: str,
        file: str | None = Query(None),
    ) -> dict[str, Any]:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        ws_dir = Path(job["workspace_dir"])
        files: list[dict[str, Any]] = []
        if ws_dir.exists():
            for p in sorted(
                (item for item in ws_dir.rglob("*") if item.is_file()),
                key=lambda item: item.relative_to(ws_dir).as_posix(),
            ):
                files.append(
                    {
                        "path": p.relative_to(ws_dir).as_posix(),
                        "size": p.stat().st_size,
                    }
                )

        if file is None:
            return {
                "files": files,
                "selected_file": None,
                "content": None,
                "truncated": False,
            }

        try:
            target = safe_join(ws_dir, file)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"Workspace file '{file}' not found.")

        max_preview = 262144
        size = target.stat().st_size
        with target.open("rb") as f:
            raw = f.read(max_preview)
        return {
            "files": files,
            "selected_file": file,
            "content": raw.decode("utf-8", errors="replace"),
            "truncated": size > max_preview,
        }

    # -------------------- Web Frontend SPA Hosting --------------------
    static_dir = _resolve_static_dir()
    index_file = static_dir / "index.html"
    if index_file.is_file():
        assets_dir = static_dir / "assets"
        if assets_dir.is_dir():
            app.mount(
                "/assets",
                StaticFiles(directory=str(assets_dir)),
                name="frontend_assets",
            )

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str) -> FileResponse:
            clean = full_path.lstrip("/")
            if clean == "api" or clean.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not Found")
            if clean:
                try:
                    candidate = safe_join(static_dir, clean)
                    if candidate.is_file():
                        return FileResponse(path=candidate)
                except ValueError:
                    pass
            return FileResponse(path=index_file)
    else:

        @app.get("/", include_in_schema=False)
        async def root_info() -> dict[str, Any]:
            return {
                "service": "colab-pro-pool",
                "version": __version__,
                "docs": "/docs",
                "healthz": "/healthz",
            }

    return app


def main() -> None:
    cfg = PoolSettings.from_env()
    uvicorn.run(
        "colab_pool.api:create_app",
        factory=True,
        host=cfg.host,
        port=cfg.port,
        reload=False,
    )


if __name__ == "__main__":
    main()

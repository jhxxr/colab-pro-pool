"""Restricted MCP identities and a persistent, transactionally updated CU ledger."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
import time
import uuid
from typing import TYPE_CHECKING, Any

from .resources import RESOURCE_CATALOG

if TYPE_CHECKING:
    from .db import PoolDatabase


class KeyPolicyError(ValueError):
    def __init__(self, message: str, status_code: int = 403) -> None:
        super().__init__(message)
        self.status_code = status_code


def normalize_models(models: list[str]) -> list[str]:
    supported = {item[0].lower(): item[0] for item in RESOURCE_CATALOG}
    result = []
    for model in models:
        canonical = supported.get(model.strip().lower())
        if canonical is None:
            raise ValueError(f"不支持的机型：{model}")
        if canonical not in result:
            result.append(canonical)
    if not result:
        raise ValueError("请至少允许一种机型。")
    return result


def requested_model(job: dict[str, Any]) -> str:
    return str(job.get("tpu") or job.get("gpu") or "T4").strip().lower()


def require_key_job(key: dict[str, Any] | None, job: dict[str, Any]) -> None:
    if not key or not key["enabled"]:
        raise KeyPolicyError("MCP 密钥已停用或不存在。")
    if key["cu_used"] >= key["cu_limit"]:
        raise KeyPolicyError("MCP 密钥的 CU 额度已耗尽。", 429)
    if requested_model(job) not in {m.lower() for m in json.loads(key["allowed_models_json"])}:
        raise KeyPolicyError("该 MCP 密钥不允许使用所选机型。")
    if job.get("drive_profile"):
        raise KeyPolicyError("MCP 密钥不能使用管理员托管的云盘档案。")


def settle_meter(conn: sqlite3.Connection, job: dict[str, Any], now: float,
                 minimum: float = 0.0, initial_rate: float | None = None) -> None:
    """Charge one interval exactly once, optionally reconcile a balance decrease.

    The account must be exclusively occupied by this pool job. CU is an
    estimate using official CLI rates, reconciled upwards with observed balance
    decreases. Account top-ups never refund already consumed key allowance.
    """
    if job["cu_meter_at"] is None:
        return
    if job["cu_rate"] is None and initial_rate is None and minimum <= 0:
        # Keep the allocation start until the first confirmed rate can price
        # that interval; watchdog ticks must not erase unpriced runtime.
        return
    now = max(now, job["cu_meter_at"])
    rate = job["cu_rate"] if job["cu_rate"] is not None else (initial_rate or 0.0)
    amount = max(0.0, (now - job["cu_meter_at"]) * rate / 3600,
                 minimum - job["cu_session_used"])
    conn.execute(
        "UPDATE jobs SET cu_used = cu_used + ?, cu_session_used = cu_session_used + ?, cu_meter_at = ? WHERE job_id = ?",
        (amount, amount, now, job["job_id"]),
    )
    conn.execute("UPDATE mcp_keys SET cu_used = cu_used + ? WHERE key_id = ?", (amount, job["mcp_key_id"]))


class McpKeyStore:
    def __init__(self, db: PoolDatabase) -> None:
        self.db = db

    @staticmethod
    def summary(key: dict[str, Any]) -> dict[str, Any]:
        return {
            k: key[k] for k in ("key_id", "name", "token_prefix", "cu_limit", "cu_used",
                               "created_at", "updated_at", "last_used_at")
        } | {
            "enabled": bool(key["enabled"]),
            "cu_remaining": max(0.0, key["cu_limit"] - key["cu_used"]),
            "allowed_models": json.loads(key["allowed_models_json"]),
        }

    def get(self, key_id: str) -> dict[str, Any] | None:
        with self.db._conn() as conn:
            row = conn.execute("SELECT * FROM mcp_keys WHERE key_id = ?", (key_id,)).fetchone()
            return dict(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        with self.db._conn() as conn:
            return [self.summary(dict(row)) for row in conn.execute("SELECT * FROM mcp_keys ORDER BY created_at DESC")]

    def create(self, name: str, cu_limit: float, models: list[str]) -> dict[str, Any]:
        name, models = self._validate(name, cu_limit, models)
        token = "cpool_" + secrets.token_urlsafe(32)
        key_id, now = uuid.uuid4().hex, time.time()
        with self.db._conn(immediate=True) as conn:
            conn.execute(
                """INSERT INTO mcp_keys (key_id, name, token_hash, token_prefix, cu_limit,
                   allowed_models_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (key_id, name, hashlib.sha256(token.encode()).hexdigest(), token[:14],
                 cu_limit, json.dumps(models), now, now),
            )
        return self.summary(self.get(key_id)) | {"token": token}

    @staticmethod
    def _validate(name: str, cu_limit: float, models: list[str]) -> tuple[str, list[str]]:
        name = name.strip()
        if not name or len(name) > 64 or any(ord(c) < 32 for c in name):
            raise ValueError("密钥名称须为 1–64 个字符，不能包含控制字符。")
        if not math.isfinite(cu_limit) or cu_limit < 0:
            raise ValueError("CU 总额度必须是大于或等于 0 的有限数值。")
        return name, normalize_models(models)

    def update(self, key_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        with self.db._conn(immediate=True) as conn:
            row = conn.execute("SELECT * FROM mcp_keys WHERE key_id = ?", (key_id,)).fetchone()
            if not row:
                return None
            key = dict(row)
            name, models = self._validate(patch.get("name", key["name"]),
                                         patch.get("cu_limit", key["cu_limit"]),
                                         patch.get("allowed_models", json.loads(key["allowed_models_json"])))
            conn.execute(
                "UPDATE mcp_keys SET name = ?, cu_limit = ?, allowed_models_json = ?, enabled = ?, updated_at = ? WHERE key_id = ?",
                (name, patch.get("cu_limit", key["cu_limit"]), json.dumps(models),
                 int(patch.get("enabled", key["enabled"])), time.time(), key_id),
            )
            return self.summary(dict(conn.execute("SELECT * FROM mcp_keys WHERE key_id = ?", (key_id,)).fetchone()))

    def authenticate(self, token: str) -> dict[str, Any] | None:
        if not token.startswith("cpool_") or len(token) > 128:
            return None
        with self.db._conn(immediate=True) as conn:
            row = conn.execute("SELECT * FROM mcp_keys WHERE token_hash = ? AND enabled = 1",
                               (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if row:
                conn.execute("UPDATE mcp_keys SET last_used_at = ? WHERE key_id = ?", (time.time(), row["key_id"]))
            return dict(row) if row else None

    def check_job(self, job: dict[str, Any]) -> None:
        if job.get("mcp_key_id"):
            require_key_job(self.get(job["mcp_key_id"]), job)

    def begin_meter(self, job_id: str, balance: float, now: float | None = None) -> None:
        with self.db._conn(immediate=True) as conn:
            conn.execute(
                "UPDATE jobs SET cu_meter_at = ?, cu_rate = NULL, cu_baseline_balance = ?, cu_session_used = 0 WHERE job_id = ?",
                (time.time() if now is None else now, balance, job_id),
            )

    def meter(self, job_id: str, *, now: float | None = None,
              balance: float | None = None, rate: float | None = None, finish: bool = False) -> None:
        if rate is not None and (not math.isfinite(rate) or rate < 0):
            raise ValueError("Invalid CU rate")
        if balance is not None and not math.isfinite(balance):
            raise ValueError("Invalid CU balance")
        timestamp = time.time() if now is None else now
        with self.db._conn(immediate=True) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row or (row["cu_meter_at"] is None and balance is None):
                return
            job = dict(row)
            if job["cu_baseline_balance"] is None:
                return
            if job["cu_meter_at"] is None:
                # Reconcile a final balance after the VM stopped without billing
                # time spent waiting for that last account-usage request.
                job["cu_meter_at"] = timestamp
            minimum = max(0.0, job["cu_baseline_balance"] - balance) if balance is not None else 0.0
            settle_meter(conn, job, timestamp, minimum, initial_rate=rate)
            if rate is not None:
                conn.execute("UPDATE jobs SET cu_rate = ? WHERE job_id = ?", (rate, job_id))
            if finish:
                conn.execute("UPDATE jobs SET cu_meter_at = NULL WHERE job_id = ?", (job_id,))
            elif row["cu_meter_at"] is None:
                conn.execute("UPDATE jobs SET cu_meter_at = NULL WHERE job_id = ?", (job_id,))

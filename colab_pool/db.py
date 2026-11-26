"""SQLite persistence for Colab Pro account pool leases and experiment jobs."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    name TEXT PRIMARY KEY,
    home_dir TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'pro',
    status TEXT NOT NULL DEFAULT 'idle',
    current_job_id TEXT,
    cooldown_until REAL NOT NULL DEFAULT 0,
    last_used_at REAL NOT NULL DEFAULT 0,
    total_runs INTEGER NOT NULL DEFAULT 0,
    total_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    gpu TEXT NOT NULL,
    tpu TEXT NOT NULL DEFAULT '',
    high_mem INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    gpu_detected TEXT NOT NULL DEFAULT '',
    entrypoint TEXT NOT NULL,
    script_args_json TEXT NOT NULL DEFAULT '[]',
    pip_packages_json TEXT NOT NULL DEFAULT '[]',
    dataset_commands_json TEXT NOT NULL DEFAULT '[]',
    env_vars_json TEXT NOT NULL DEFAULT '{}',
    drive_profile TEXT NOT NULL DEFAULT '',
    drive_subpath TEXT NOT NULL DEFAULT '',
    drive_sync_outputs_to TEXT NOT NULL DEFAULT '',
    timeout_seconds INTEGER NOT NULL DEFAULT 43200,
    assigned_account TEXT NOT NULL DEFAULT '',
    session_name TEXT NOT NULL DEFAULT '',
    attempted_accounts_json TEXT NOT NULL DEFAULT '[]',
    workspace_dir TEXT NOT NULL,
    log_path TEXT NOT NULL,
    artifacts_archive TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    error_message TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);

CREATE TABLE IF NOT EXISTS resource_observations (
    job_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    high_mem INTEGER NOT NULL DEFAULT 0,
    actual_model TEXT NOT NULL,
    runtime_info_json TEXT NOT NULL,
    consumption_rate_hourly REAL NOT NULL,
    observed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resource_observations_time
ON resource_observations(observed_at DESC);
"""


class PoolDatabase:
    """Thread-safe SQLite state store using WAL mode and immediate write transactions."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _conn(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(
                str(self.db_path), timeout=10.0, isolation_level=None
            )
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA busy_timeout=5000;")
                if immediate:
                    conn.execute("BEGIN IMMEDIATE;")
                    try:
                        yield conn
                        conn.execute("COMMIT;")
                    except Exception:
                        conn.execute("ROLLBACK;")
                        raise
                else:
                    yield conn
            finally:
                conn.close()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = sqlite3.connect(str(self.db_path), timeout=10.0)
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute("PRAGMA busy_timeout=5000;")
                conn.executescript(SCHEMA_SQL)

                existing_cols = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
                }
                if "priority" not in existing_cols:
                    conn.execute(
                        "ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;"
                    )
                if "gpu_detected" not in existing_cols:
                    conn.execute(
                        "ALTER TABLE jobs ADD COLUMN gpu_detected TEXT NOT NULL DEFAULT '';"
                    )
                if "runtime_info_json" not in existing_cols:
                    conn.execute("ALTER TABLE jobs ADD COLUMN runtime_info_json TEXT NOT NULL DEFAULT '{}';")
                account_cols = {
                    row[1] for row in conn.execute("PRAGMA table_info(accounts)")
                }
                usage_columns = {
                    "cu_balance": "REAL",
                    "cu_consumption_rate": "REAL",
                    "cu_active_assignments": "INTEGER",
                    "cu_updated_at": "REAL",
                    "cu_checked_at": "REAL",
                    "cu_error": "TEXT NOT NULL DEFAULT ''",
                    "cu_generation": "TEXT NOT NULL DEFAULT ''",
                }
                for column, definition in usage_columns.items():
                    if column not in account_cols:
                        conn.execute(f"ALTER TABLE accounts ADD COLUMN {column} {definition}")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_jobs_priority_queue "
                    "ON jobs(status, priority DESC, created_at ASC);"
                )
                conn.commit()
            finally:
                conn.close()

    # -------------------- Account Management --------------------

    def upsert_account(
        self, name: str, home_dir: str, tier: str = "pro", enabled: bool = True
    ) -> dict[str, Any]:
        with self._conn(immediate=True) as conn:
            existing = conn.execute(
                "SELECT * FROM accounts WHERE name = ?", (name,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO accounts (name, home_dir, tier, status, enabled, cu_generation)
                    VALUES (?, ?, ?, 'idle', ?, ?)
                    """,
                    (name, home_dir, tier, 1 if enabled else 0, uuid.uuid4().hex),
                )
            else:
                conn.execute(
                    """
                    UPDATE accounts
                    SET home_dir = ?, tier = ?, enabled = ?
                    WHERE name = ?
                    """,
                    (home_dir, tier, 1 if enabled else 0, name),
                )
                if existing["home_dir"] != home_dir:
                    conn.execute(
                        """UPDATE accounts SET cu_balance = NULL, cu_consumption_rate = NULL,
                           cu_active_assignments = NULL, cu_updated_at = NULL,
                           cu_checked_at = NULL, cu_error = '', cu_generation = ?
                           WHERE name = ?""",
                        (uuid.uuid4().hex, name),
                    )
            row = conn.execute(
                "SELECT * FROM accounts WHERE name = ?", (name,)
            ).fetchone()
            return dict(row)

    def invalidate_account_usage(self, name: str) -> None:
        """New credentials must never inherit a different Google account's balance."""
        with self._conn(immediate=True) as conn:
            conn.execute(
                """
                UPDATE accounts SET cu_balance = NULL, cu_consumption_rate = NULL,
                    cu_active_assignments = NULL, cu_updated_at = NULL,
                    cu_checked_at = NULL, cu_error = '', cu_generation = ?
                WHERE name = ?
                """,
                (uuid.uuid4().hex, name),
            )

    def save_account_usage(
        self,
        name: str,
        home_dir: str,
        generation: str,
        *,
        checked_at: float,
        balance: float | None = None,
        consumption_rate: float | None = None,
        active_assignments: int | None = None,
        error: str = "",
    ) -> None:
        """Preserve last good values on failure; reject a removed/replaced slot."""
        with self._conn(immediate=True) as conn:
            if error:
                conn.execute(
                    """UPDATE accounts SET cu_checked_at = ?, cu_error = ?
                       WHERE name = ? AND home_dir = ? AND cu_generation = ?""",
                    (checked_at, error, name, home_dir, generation),
                )
            else:
                conn.execute(
                    """
                    UPDATE accounts SET cu_balance = ?, cu_consumption_rate = ?,
                        cu_active_assignments = ?, cu_updated_at = ?,
                        cu_checked_at = ?, cu_error = ''
                    WHERE name = ? AND home_dir = ? AND cu_generation = ?
                    """,
                    (balance, consumption_rate, active_assignments, checked_at,
                     checked_at, name, home_dir, generation),
                )

    def get_account(self, name: str) -> dict[str, Any] | None:
        with self._conn(immediate=False) as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE name = ?", (name,)
            ).fetchone()
            return dict(row) if row else None

    def refresh_expired_cooldowns(self, now: float | None = None) -> int:
        ts = time.time() if now is None else now
        with self._conn(immediate=True) as conn:
            cur = conn.execute(
                """
                UPDATE accounts
                SET status = 'idle', cooldown_until = 0
                WHERE status = 'cooldown' AND cooldown_until <= ?
                """,
                (ts,),
            )
            return cur.rowcount

    def acquire_idle_account(
        self,
        job_id: str,
        exclude_accounts: list[str] | None = None,
        require_pro_plus: bool = False,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Atomically lease the least-recently-used idle account."""
        ts = time.time() if now is None else now
        excluded = set(exclude_accounts or [])
        with self._conn(immediate=True) as conn:
            conn.execute(
                """
                UPDATE accounts
                SET status = 'idle', cooldown_until = 0
                WHERE status = 'cooldown' AND cooldown_until <= ?
                """,
                (ts,),
            )
            rows = conn.execute(
                """
                SELECT * FROM accounts
                WHERE enabled = 1 AND status = 'idle'
                ORDER BY last_used_at ASC, name ASC
                """
            ).fetchall()

            chosen: sqlite3.Row | None = None
            for row in rows:
                if row["name"] in excluded:
                    continue
                if require_pro_plus and row["tier"].lower() != "pro+":
                    continue
                chosen = row
                break

            if chosen is None:
                return None

            conn.execute(
                """
                UPDATE accounts
                SET status = 'busy', current_job_id = ?, last_used_at = ?
                WHERE name = ?
                """,
                (job_id, ts, chosen["name"]),
            )
            updated = conn.execute(
                "SELECT * FROM accounts WHERE name = ?", (chosen["name"],)
            ).fetchone()
            return dict(updated)

    def acquire_account_for_cleanup(self, name: str) -> bool:
        """Reserve a non-running account atomically against job scheduling."""
        with self._conn(immediate=True) as conn:
            cur = conn.execute(
                """
                UPDATE accounts SET status = 'maintenance'
                WHERE name = ? AND enabled = 1
                  AND status IN ('idle', 'cooldown') AND current_job_id IS NULL
                """,
                (name,),
            )
            return cur.rowcount == 1

    def release_account_from_cleanup(self, name: str) -> None:
        with self._conn(immediate=True) as conn:
            conn.execute(
                """
                UPDATE accounts
                SET status = CASE WHEN cooldown_until > ? THEN 'cooldown' ELSE 'idle' END
                WHERE name = ? AND status = 'maintenance'
                """,
                (time.time(), name),
            )

    def acquire_account_for_auth(self, name: str) -> bool:
        """Reserve even a disabled slot without changing its enabled flag."""
        with self._conn(immediate=True) as conn:
            cur = conn.execute(
                """
                UPDATE accounts SET status = 'maintenance'
                WHERE name = ? AND status IN ('idle', 'cooldown')
                  AND current_job_id IS NULL
                """,
                (name,),
            )
            return cur.rowcount == 1

    def release_account(
        self,
        name: str,
        *,
        cooldown_seconds: int = 0,
        error_message: str = "",
        increment_run: bool = True,
        now: float | None = None,
    ) -> None:
        ts = time.time() if now is None else now
        with self._conn(immediate=True) as conn:
            if cooldown_seconds > 0:
                conn.execute(
                    """
                    UPDATE accounts
                    SET status = 'cooldown',
                        current_job_id = NULL,
                        cooldown_until = ?,
                        total_failures = total_failures + 1,
                        last_error = ?
                    WHERE name = ?
                    """,
                    (ts + cooldown_seconds, error_message[:1000], name),
                )
            else:
                conn.execute(
                    """
                    UPDATE accounts
                    SET status = 'idle',
                        current_job_id = NULL,
                        cooldown_until = 0,
                        total_runs = total_runs + ?,
                        last_error = CASE WHEN ? != '' THEN ? ELSE last_error END
                    WHERE name = ?
                    """,
                    (1 if increment_run else 0, error_message, error_message[:1000], name),
                )

    def reset_account(self, name: str, enabled: bool = True) -> dict[str, Any] | None:
        return self.update_account(name, enabled=enabled, clear_cooldown=True)

    def update_account(
        self,
        name: str,
        *,
        tier: str | None = None,
        enabled: bool | None = None,
        clear_cooldown: bool = False,
    ) -> dict[str, Any] | None:
        with self._conn(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                return None

            clauses: list[str] = []
            params: list[Any] = []
            if tier is not None:
                clauses.append("tier = ?")
                params.append(tier)
            if enabled is not None:
                clauses.append("enabled = ?")
                params.append(1 if enabled else 0)
            if clear_cooldown:
                clauses.append(
                    "status = CASE WHEN status = 'maintenance' THEN 'maintenance' "
                    "WHEN current_job_id IS NOT NULL THEN 'busy' ELSE 'idle' END"
                )
                clauses.append("cooldown_until = 0")
                clauses.append("last_error = ''")

            if clauses:
                params.append(name)
                conn.execute(
                    f"UPDATE accounts SET {', '.join(clauses)} WHERE name = ?",
                    params,
                )

            updated = conn.execute(
                "SELECT * FROM accounts WHERE name = ?", (name,)
            ).fetchone()
            return dict(updated)

    def delete_account(self, name: str) -> bool:
        with self._conn(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                return False
            if row["status"] in {"busy", "maintenance"}:
                raise ValueError(f"Cannot delete busy account '{name}'.")
            conn.execute("DELETE FROM accounts WHERE name = ?", (name,))
            return True

    def reset_stale_busy_accounts_on_boot(self) -> None:
        """Recover accounts & running jobs interrupted by an unexpected server restart."""
        now = time.time()
        with self._conn(immediate=True) as conn:
            conn.execute(
                """
                UPDATE accounts
                SET status = 'idle', current_job_id = NULL
                WHERE status IN ('busy', 'maintenance')
                """
            )
            conn.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    error_message = 'Server restarted while job was running; orphan runtime scheduled for cleanup.',
                    finished_at = ?
                WHERE status IN ('provisioning', 'running')
                """,
                (now,),
            )

    def list_accounts(self) -> list[dict[str, Any]]:
        self.refresh_expired_cooldowns()
        with self._conn(immediate=False) as conn:
            rows = conn.execute(
                "SELECT * FROM accounts ORDER BY name ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    # -------------------- Job Management --------------------

    def save_resource_observation(self, observation: dict[str, Any]) -> None:
        with self._conn(immediate=True) as conn:
            conn.execute(
                """INSERT INTO resource_observations
                   (job_id, resource_id, high_mem, actual_model, runtime_info_json,
                    consumption_rate_hourly, observed_at)
                   VALUES (:job_id, :resource_id, :high_mem, :actual_model, :runtime_info_json,
                           :consumption_rate_hourly, :observed_at)
                   ON CONFLICT(job_id) DO UPDATE SET
                     resource_id=excluded.resource_id, high_mem=excluded.high_mem,
                     actual_model=excluded.actual_model, runtime_info_json=excluded.runtime_info_json,
                     consumption_rate_hourly=excluded.consumption_rate_hourly,
                     observed_at=excluded.observed_at
                   WHERE excluded.observed_at > resource_observations.observed_at""",
                observation,
            )

    def list_resource_observations(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM resource_observations ORDER BY observed_at DESC LIMIT 500"
            ).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                item["high_mem"] = bool(item["high_mem"])
                item["runtime_info"] = json.loads(item.pop("runtime_info_json"))
                results.append(item)
            return results

    def create_job(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = {"priority": 0, **record}
        with self._conn(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, name, status, gpu, tpu, high_mem, priority, entrypoint,
                    script_args_json, pip_packages_json, dataset_commands_json,
                    env_vars_json, drive_profile, drive_subpath,
                    drive_sync_outputs_to, timeout_seconds, workspace_dir,
                    log_path, created_at
                ) VALUES (
                    :job_id, :name, :status, :gpu, :tpu, :high_mem, :priority, :entrypoint,
                    :script_args_json, :pip_packages_json, :dataset_commands_json,
                    :env_vars_json, :drive_profile, :drive_subpath,
                    :drive_sync_outputs_to, :timeout_seconds, :workspace_dir,
                    :log_path, :created_at
                )
                """,
                payload,
            )
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (payload["job_id"],)
            ).fetchone()
            return self._deserialize_job(row)

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get_job(job_id)
        columns = ", ".join(f"{k} = ?" for k in fields.keys())
        values = list(fields.values()) + [job_id]
        with self._conn(immediate=True) as conn:
            conn.execute(f"UPDATE jobs SET {columns} WHERE job_id = ?", values)
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return self._deserialize_job(row) if row else None

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._conn(immediate=False) as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return self._deserialize_job(row) if row else None

    def delete_job(self, job_id: str) -> bool:
        with self._conn(immediate=True) as conn:
            cur = conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            return cur.rowcount > 0

    @staticmethod
    def _build_job_filter_clause(
        status: str | None = None,
        gpu: str | None = None,
        account: str | None = None,
        search: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status and status.strip():
            clauses.append("status = ?")
            params.append(status.strip())
        if gpu and gpu.strip():
            clauses.append("UPPER(gpu) = ?")
            params.append(gpu.strip().upper())
        if account and account.strip():
            clauses.append("assigned_account = ?")
            params.append(account.strip())
        if search and search.strip():
            like = f"%{search.strip().lower()}%"
            clauses.append(
                "(LOWER(name) LIKE ? OR LOWER(job_id) LIKE ? "
                "OR LOWER(assigned_account) LIKE ? OR LOWER(entrypoint) LIKE ?)"
            )
            params.extend([like, like, like, like])
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where_sql, params

    def list_jobs(
        self,
        status: str | None = None,
        limit: int = 50,
        *,
        gpu: str | None = None,
        account: str | None = None,
        search: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where_sql, params = self._build_job_filter_clause(
            status=status, gpu=gpu, account=account, search=search
        )
        with self._conn(immediate=False) as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return [self._deserialize_job(r) for r in rows]

    def count_jobs(
        self,
        status: str | None = None,
        *,
        gpu: str | None = None,
        account: str | None = None,
        search: str | None = None,
    ) -> int:
        where_sql, params = self._build_job_filter_clause(
            status=status, gpu=gpu, account=account, search=search
        )
        with self._conn(immediate=False) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM jobs {where_sql}",
                params,
            ).fetchone()
            return int(row["cnt"]) if row else 0

    def list_queued_jobs(self) -> list[dict[str, Any]]:
        with self._conn(immediate=False) as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY priority DESC, created_at ASC"
            ).fetchall()
            return [self._deserialize_job(r) for r in rows]

    def get_stats(self) -> dict[str, Any]:
        self.refresh_expired_cooldowns()
        with self._conn(immediate=False) as conn:
            acct_rows = conn.execute("SELECT status, enabled FROM accounts").fetchall()
            accounts_stats = {
                "total": len(acct_rows),
                "idle": sum(1 for a in acct_rows if a["enabled"] and a["status"] == "idle"),
                "busy": sum(1 for a in acct_rows if a["status"] == "busy"),
                "cooldown": sum(1 for a in acct_rows if a["status"] == "cooldown"),
                "disabled": sum(1 for a in acct_rows if not a["enabled"]),
            }

            job_rows = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM jobs GROUP BY status"
            ).fetchall()
            jobs_stats = {
                "queued": 0,
                "provisioning": 0,
                "running": 0,
                "succeeded": 0,
                "failed": 0,
                "cancelled": 0,
                "total": 0,
            }
            for row in job_rows:
                st = row["status"]
                cnt = int(row["cnt"])
                jobs_stats["total"] += cnt
                if st in jobs_stats:
                    jobs_stats[st] = cnt

            gpu_rows = conn.execute(
                "SELECT UPPER(gpu) AS gpu_type, COUNT(*) AS cnt FROM jobs GROUP BY UPPER(gpu)"
            ).fetchall()
            gpu_counts = {str(r["gpu_type"]): int(r["cnt"]) for r in gpu_rows}

            completed = jobs_stats["succeeded"] + jobs_stats["failed"]
            success_rate = (
                round(jobs_stats["succeeded"] / completed, 4) if completed > 0 else None
            )

            dur_row = conn.execute(
                """
                SELECT AVG(finished_at - started_at) AS avg_dur
                FROM jobs
                WHERE status = 'succeeded'
                  AND started_at IS NOT NULL
                  AND finished_at IS NOT NULL
                  AND finished_at >= started_at
                """
            ).fetchone()
            avg_duration = (
                round(float(dur_row["avg_dur"]), 2)
                if dur_row and dur_row["avg_dur"] is not None
                else None
            )

            return {
                "accounts": accounts_stats,
                "jobs": jobs_stats,
                "gpu_counts": gpu_counts,
                "success_rate": success_rate,
                "avg_duration_seconds": avg_duration,
            }

    @staticmethod
    def _deserialize_job(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["high_mem"] = bool(d["high_mem"])
        d["priority"] = int(d.get("priority", 0) or 0)
        d["gpu_detected"] = d.get("gpu_detected", "") or ""
        d["runtime_info"] = json.loads(d.pop("runtime_info_json", "{}"))
        d["script_args"] = json.loads(d.pop("script_args_json", "[]"))
        d["pip_packages"] = json.loads(d.pop("pip_packages_json", "[]"))
        d["dataset_commands"] = json.loads(d.pop("dataset_commands_json", "[]"))
        d["env_vars"] = json.loads(d.pop("env_vars_json", "{}"))
        d["attempted_accounts"] = json.loads(d.pop("attempted_accounts_json", "[]"))
        return d

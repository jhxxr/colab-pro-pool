"""Account pool discovery, orphan session reaper, and Drive profile inspection."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .config import PoolSettings
from .db import PoolDatabase
from .remote_bootstrap import parse_first_rclone_remote
from .proxy_config import redact_proxy_credentials
from .utils import SAFE_NAME_RE

logger = logging.getLogger("colab_pool.pool")
_AUTO_PROXY = object()

SESSION_ID_RE = re.compile(r"\b([a-z][a-z0-9\-]{1,62})\b")
VALID_TIERS = {"free", "pro", "pro+"}


class AccountPoolManager:
    """Manages isolated HOME directories for multiple Google Colab Pro accounts."""

    def __init__(self, settings: PoolSettings, db: PoolDatabase) -> None:
        self.settings = settings
        self.db = db

    @staticmethod
    def detect_auth_configured(home_dir: str | Path) -> bool:
        """Check whether an account HOME appears to have cached CLI OAuth/ADC credentials."""
        cli_dir = Path(home_dir) / ".config" / "colab-cli"
        if not cli_dir.is_dir():
            return False
        try:
            for item in cli_dir.iterdir():
                if not item.is_file():
                    continue
                if item.name in {"sessions.json", "settings.json"}:
                    continue
                if item.stat().st_size > 0:
                    return True
        except OSError:
            return False
        return False

    def enrich_account(
        self, acct: dict[str, Any], now: float | None = None
    ) -> dict[str, Any]:
        """Return account dict augmented with auth status and live cooldown remaining."""
        ts = time.time() if now is None else now
        enriched = dict(acct)
        enriched["usage"] = {
            "remaining_cu": enriched.pop("cu_balance", None),
            "consumption_rate_hourly": enriched.pop("cu_consumption_rate", None),
            "active_assignments": enriched.pop("cu_active_assignments", None),
            "updated_at": enriched.pop("cu_updated_at", None),
            "checked_at": enriched.pop("cu_checked_at", None),
            "error": enriched.pop("cu_error", ""),
            "refreshing": False,
        }
        enriched.pop("cu_generation", None)
        usage = enriched["usage"]
        usage["stale"] = bool(
            usage["error"] or usage["updated_at"] is None
            or ts - usage["updated_at"] >= max(5.0, self.settings.usage_cache_seconds)
        )
        enriched["auth_configured"] = self.detect_auth_configured(acct["home_dir"])
        proxy_id = acct.get("proxy_id")
        proxy = self.db.get_proxy(proxy_id) if proxy_id else None
        enriched["proxy_name"] = proxy["name"] if proxy else None
        if acct.get("status") == "cooldown":
            remaining = int(max(0.0, float(acct.get("cooldown_until", 0.0)) - ts))
        else:
            remaining = 0
        enriched["cooldown_remaining_seconds"] = remaining
        enriched["runtime"] = None
        if acct.get("current_job_id"):
            job = self.db.get_job(acct["current_job_id"])
            if job and job["status"] in {"provisioning", "running"} and job["assigned_account"] == acct["name"]:
                enriched["runtime"] = {
                    "job_id": job["job_id"], "job_name": job["name"], "status": job["status"],
                    "gpu": job["gpu"], "tpu": job["tpu"], "high_mem": job["high_mem"],
                    "gpu_detected": job["gpu_detected"], "hardware": job["runtime_info"],
                }
        return enriched

    @staticmethod
    def set_account_enabled_marker(home_dir: str | Path, enabled: bool) -> None:
        """Persist `.disabled` marker on disk so `sync_accounts_from_disk` preserves UI toggles."""
        hd = Path(home_dir)
        if not hd.exists():
            return
        marker = hd / ".disabled"
        if enabled:
            marker.unlink(missing_ok=True)
        else:
            marker.write_text("disabled\n", encoding="utf-8")

    def sync_accounts_from_disk(self) -> list[dict[str, Any]]:
        """Discover all subdirectories under `accounts_dir` and register them."""
        self.settings.ensure_dirs()
        discovered: list[dict[str, Any]] = []
        for entry in sorted(self.settings.accounts_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if (entry / ".removed").exists():
                continue
            tier_file = entry / "tier.txt"
            tier = "pro"
            if tier_file.exists():
                raw_tier = tier_file.read_text(encoding="utf-8").strip().lower()
                if raw_tier in VALID_TIERS:
                    tier = raw_tier
            disabled_marker = entry / ".disabled"
            acct = self.db.upsert_account(
                name=entry.name,
                home_dir=str(entry.resolve()),
                tier=tier,
                enabled=not disabled_marker.exists(),
            )
            discovered.append(self.enrich_account(acct))
        return discovered

    def register_account_dir(self, name: str, tier: str = "pro") -> dict[str, Any]:
        """Create or update an isolated account HOME directory."""
        if not SAFE_NAME_RE.fullmatch(name):
            raise ValueError(
                "Account name must contain only letters, digits, underscores, or hyphens."
            )
        tier_norm = tier.strip().lower()
        if tier_norm not in VALID_TIERS:
            raise ValueError("Tier must be 'free', 'pro', or 'pro+'.")
        acct_dir = (self.settings.accounts_dir / name).resolve()
        if acct_dir.parent != self.settings.accounts_dir.resolve():
            raise ValueError("Account directory must stay inside the accounts directory.")
        (acct_dir / ".config" / "colab-cli").mkdir(parents=True, exist_ok=True)
        (acct_dir / "tier.txt").write_text(tier_norm + "\n", encoding="utf-8")
        (acct_dir / ".removed").unlink(missing_ok=True)
        self.set_account_enabled_marker(acct_dir, True)
        acct = self.db.upsert_account(
            name=name, home_dir=str(acct_dir), tier=tier_norm, enabled=True
        )
        return self.enrich_account(acct)

    def reset_account(self, name: str, enabled: bool = True) -> dict[str, Any] | None:
        """Reset cooldown/error state and persist enabled flag to disk."""
        existing = self.db.get_account(name)
        if existing is None:
            return None
        self.set_account_enabled_marker(existing["home_dir"], enabled)
        updated = self.db.reset_account(name, enabled=enabled)
        return self.enrich_account(updated) if updated else None

    def update_account(
        self,
        name: str,
        *,
        tier: str | None = None,
        enabled: bool | None = None,
        clear_cooldown: bool = False,
        proxy_id: str | None = None,
        set_proxy: bool = False,
    ) -> dict[str, Any] | None:
        """Update account tier, enabled state, or cooldown both in DB and on disk."""
        existing = self.db.get_account(name)
        if existing is None:
            return None

        tier_norm: str | None = None
        if tier is not None:
            tier_norm = tier.strip().lower()
            if tier_norm not in VALID_TIERS:
                raise ValueError("Tier must be 'free', 'pro', or 'pro+'.")
            hd = Path(existing["home_dir"])
            if hd.exists():
                (hd / "tier.txt").write_text(tier_norm + "\n", encoding="utf-8")

        if enabled is not None:
            self.set_account_enabled_marker(existing["home_dir"], enabled)

        if set_proxy and proxy_id is not None and self.db.get_proxy(proxy_id) is None:
            raise ValueError("所选代理不存在，请刷新列表后重试。")

        updated = self.db.update_account(
            name, tier=tier_norm, enabled=enabled, clear_cooldown=clear_cooldown,
            proxy_id=proxy_id, set_proxy=set_proxy,
        )
        return self.enrich_account(updated) if updated else None

    def delete_account(self, name: str, remove_disk: bool = False) -> bool:
        """Delete an idle/cooldown account from DB and optionally remove its directory."""
        existing = self.db.get_account(name)
        if existing is None:
            return False
        home_dir = Path(existing["home_dir"])
        marker = home_dir / ".removed"
        had_marker = marker.exists()
        # Persist exclusion before deleting DB state. A failed marker write must
        # leave the account registered, not silently resurrect it on the next scan.
        if home_dir.is_dir():
            marker.write_text("removed\n", encoding="utf-8")
        try:
            deleted = self.db.delete_account(name)
        except Exception:
            if not had_marker:
                marker.unlink(missing_ok=True)
            raise
        if deleted and remove_disk:
            base = self.settings.accounts_dir.resolve()
            target = Path(existing["home_dir"]).resolve()
            if target != base and target.is_relative_to(base) and target.exists():
                shutil.rmtree(target, ignore_errors=True)
        return deleted

    def list_accounts_enriched(self) -> list[dict[str, Any]]:
        now = time.time()
        return [self.enrich_account(a, now=now) for a in self.db.list_accounts()]

    def list_drive_profiles(self) -> list[dict[str, str]]:
        """List available pre-authorized rclone Google Drive `.conf` profiles."""
        self.settings.ensure_dirs()
        profiles: list[dict[str, str]] = []
        for conf_path in sorted(self.settings.drives_dir.glob("*.conf")):
            try:
                remote = parse_first_rclone_remote(conf_path.read_text(encoding="utf-8"))
            except Exception as exc:
                remote = f"invalid ({exc})"
            profiles.append(
                {
                    "profile": conf_path.stem,
                    "remote_name": remote,
                    "path": str(conf_path),
                }
            )
        return profiles

    def build_cli_env(
        self, home_dir: str, *, proxy_id: str | None | object = _AUTO_PROXY
    ) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = home_dir
        # Python's expanduser() uses USERPROFILE on Windows, ignoring HOME.
        env["USERPROFILE"] = home_dir
        env["XDG_CONFIG_HOME"] = str(Path(home_dir) / ".config")
        env["COLAB_POOL_ACCOUNT_HOME"] = home_dir
        # Upstream uses a fixed keyring service/username. HOME alone does not
        # isolate an OS keyring and containers often have no OS keyring at all.
        env["PYTHON_KEYRING_BACKEND"] = "colab_pool.keyring_backend.AccountKeyring"
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (package_root, env.get("PYTHONPATH", "")) if part
        )
        env["PYTHONUNBUFFERED"] = "1"
        selected_proxy = (
            self.db.get_proxy_url_for_home(home_dir)
            if proxy_id is _AUTO_PROXY
            else self.db.get_proxy_url(proxy_id) if isinstance(proxy_id, str) else None
        )
        if selected_proxy:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                env[key] = selected_proxy
            for key in ("http_proxy", "https_proxy", "all_proxy"):
                env[key] = selected_proxy
            # Do not inherit server-level bypass rules that could send Google
            # traffic directly instead of through this account's selected proxy.
            env["NO_PROXY"] = ""
            env["no_proxy"] = ""
        return env

    async def cleanup_orphan_runtimes(self, skip_busy: bool = True) -> dict[str, str]:
        """Stop orphan runtimes while reserving each eligible account.

        `skip_busy` is retained for compatibility; active/maintenance accounts
        are always protected, including when it is False.
        """
        results: dict[str, str] = {}
        accounts = self.db.list_accounts()
        for acct in accounts:
            name = acct["name"]
            if not acct["enabled"]:
                continue
            home_dir = acct["home_dir"]
            token_dir = Path(home_dir) / ".config" / "colab-cli"
            if not token_dir.exists():
                continue
            # Always reserve against the live DB state, not the list snapshot.
            # Active jobs must never be stopped by an orphan sweep.
            if not self.db.acquire_account_for_cleanup(name):
                results[name] = "skipped (busy)"
                continue
            proc: asyncio.subprocess.Process | None = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.settings.colab_bin,
                    "stop",
                    env=self.build_cli_env(home_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=25.0)
                output = redact_proxy_credentials(
                    out_bytes.decode("utf-8", errors="replace")
                ).strip()
                results[name] = output or f"exit={proc.returncode}"
            except FileNotFoundError:
                results[name] = f"CLI binary '{self.settings.colab_bin}' not installed yet"
                break
            except Exception as exc:
                logger.warning("Orphan cleanup on %s raised: %s", name, exc)
                results[name] = f"error: {exc}"
            finally:
                try:
                    if proc is not None and proc.returncode is None:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        await proc.communicate()
                finally:
                    self.db.release_account_from_cleanup(name)
        return results

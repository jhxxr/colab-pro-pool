"""Configuration settings for Colab Pro Pool."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_data_dir() -> Path:
    env_val = os.environ.get("COLAB_POOL_DATA_DIR")
    if env_val:
        return Path(env_val).expanduser().resolve()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return Path("/var/lib/colab-pool")
    return (Path.home() / ".local" / "share" / "colab-pool").resolve()


def _parse_cors_origins(raw: str | None) -> tuple[str, ...]:
    if not raw or not raw.strip():
        return ("*",)
    parts = tuple(item.strip() for item in raw.split(",") if item.strip())
    return parts or ("*",)


@dataclass(frozen=True)
class PoolSettings:
    """Runtime settings loaded from environment variables."""

    data_dir: Path
    accounts_dir: Path
    drives_dir: Path
    jobs_dir: Path
    db_path: Path
    colab_bin: str
    api_key: str
    default_cooldown_seconds: int
    poll_interval_seconds: float
    cleanup_orphans_on_startup: bool
    host: str
    port: int
    cors_origins: tuple[str, ...] = ("*",)
    auth_timeout_seconds: float = 600.0
    auth_startup_timeout_seconds: float = 30.0
    usage_cache_seconds: float = 60.0
    usage_timeout_seconds: float = 25.0

    @classmethod
    def from_env(cls) -> "PoolSettings":
        base = _default_data_dir()
        accounts_dir = Path(
            os.environ.get("COLAB_POOL_ACCOUNTS_DIR", str(base / "accounts"))
        ).expanduser().resolve()
        drives_dir = Path(
            os.environ.get("COLAB_POOL_DRIVES_DIR", str(base / "drives"))
        ).expanduser().resolve()
        jobs_dir = Path(
            os.environ.get("COLAB_POOL_JOBS_DIR", str(base / "jobs"))
        ).expanduser().resolve()
        db_path = Path(
            os.environ.get("COLAB_POOL_DB_PATH", str(base / "pool.sqlite3"))
        ).expanduser().resolve()
        return cls(
            data_dir=base,
            accounts_dir=accounts_dir,
            drives_dir=drives_dir,
            jobs_dir=jobs_dir,
            db_path=db_path,
            colab_bin=os.environ.get("COLAB_POOL_CLI_BIN", "colab"),
            api_key=os.environ.get("COLAB_POOL_API_KEY", ""),
            default_cooldown_seconds=int(
                os.environ.get("COLAB_POOL_COOLDOWN_SECONDS", "1800")
            ),
            poll_interval_seconds=float(
                os.environ.get("COLAB_POOL_POLL_INTERVAL", "2.0")
            ),
            cleanup_orphans_on_startup=os.environ.get(
                "COLAB_POOL_CLEANUP_ORPHANS", "1"
            ).lower()
            in {"1", "true", "yes"},
            host=os.environ.get("COLAB_POOL_HOST", "0.0.0.0"),
            port=int(os.environ.get("COLAB_POOL_PORT", "8080")),
            cors_origins=_parse_cors_origins(os.environ.get("COLAB_POOL_CORS_ORIGINS")),
            auth_timeout_seconds=float(
                os.environ.get("COLAB_POOL_AUTH_TIMEOUT_SECONDS", "600")
            ),
            auth_startup_timeout_seconds=float(
                os.environ.get("COLAB_POOL_AUTH_STARTUP_TIMEOUT_SECONDS", "30")
            ),
            usage_cache_seconds=float(
                os.environ.get("COLAB_POOL_USAGE_CACHE_SECONDS", "60")
            ),
            usage_timeout_seconds=float(
                os.environ.get("COLAB_POOL_USAGE_TIMEOUT_SECONDS", "25")
            ),
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        self.drives_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

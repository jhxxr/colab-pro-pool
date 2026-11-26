"""Persistent profile configuration for the `cpool` / `colab-pool` CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_SERVER_URL = "http://127.0.0.1:8080"
DEFAULT_GPU = "T4"
DEFAULT_TIMEOUT_SECONDS = 43200


def get_config_path() -> Path:
    """Return path to the CLI configuration file."""
    override = os.environ.get("COLAB_POOL_CONFIG_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".config" / "colab-pool" / "config.json").resolve()


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load CLI config from disk, returning empty defaults if missing."""
    cfg_path = path or get_config_path()
    if not cfg_path.is_file():
        return {"current_profile": "default", "profiles": {}}
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"current_profile": "default", "profiles": {}}
        profiles = raw.get("profiles")
        if not isinstance(profiles, dict):
            profiles = {}
        current = str(raw.get("current_profile") or "default")
        return {"current_profile": current, "profiles": profiles}
    except Exception:
        return {"current_profile": "default", "profiles": {}}


def save_config(cfg: dict[str, Any], path: Path | None = None) -> Path:
    """Persist CLI config to disk with restrictive permissions on POSIX."""
    cfg_path = path or get_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        try:
            os.chmod(cfg_path, 0o600)
        except OSError:
            pass
    return cfg_path


def resolve_profile(
    profile_name: str | None = None,
    *,
    server_override: str | None = None,
    api_key_override: str | None = None,
    path: Path | None = None,
) -> tuple[str, dict[str, Any]]:
    """Resolve effective profile settings using CLI > env > saved profile > defaults."""
    cfg = load_config(path)
    profiles: dict[str, Any] = cfg.get("profiles", {})
    chosen = (
        (profile_name or "").strip()
        or os.environ.get("COLAB_POOL_PROFILE", "").strip()
        or str(cfg.get("current_profile") or "default")
    )
    saved = profiles.get(chosen)
    saved_dict = dict(saved) if isinstance(saved, dict) else {}

    env_server = os.environ.get("COLAB_POOL_SERVER_URL", "").strip()
    env_key = os.environ.get("COLAB_POOL_API_KEY", "").strip()

    server = (
        (server_override.strip() if server_override else "")
        or env_server
        or str(saved_dict.get("server") or "").strip()
        or DEFAULT_SERVER_URL
    ).rstrip("/")

    api_key = (
        (api_key_override.strip() if api_key_override is not None and api_key_override.strip() else "")
        or env_key
        or str(saved_dict.get("api_key") or "").strip()
    )

    default_gpu = str(saved_dict.get("default_gpu") or DEFAULT_GPU).strip().upper() or DEFAULT_GPU
    try:
        default_timeout = int(saved_dict.get("default_timeout", DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        default_timeout = DEFAULT_TIMEOUT_SECONDS

    return chosen, {
        "server": server,
        "api_key": api_key,
        "default_gpu": default_gpu,
        "default_timeout": default_timeout,
    }


def save_profile(
    name: str,
    *,
    server: str,
    api_key: str,
    default_gpu: str = DEFAULT_GPU,
    default_timeout: int = DEFAULT_TIMEOUT_SECONDS,
    set_current: bool = True,
    path: Path | None = None,
) -> Path:
    """Create or update a named profile in config.json."""
    cfg = load_config(path)
    profiles = cfg.setdefault("profiles", {})
    profiles[name] = {
        "server": server.strip().rstrip("/"),
        "api_key": api_key.strip(),
        "default_gpu": default_gpu.strip().upper() or DEFAULT_GPU,
        "default_timeout": int(default_timeout),
    }
    if set_current:
        cfg["current_profile"] = name
    return save_config(cfg, path)


def delete_profile(name: str, path: Path | None = None) -> bool:
    """Remove a saved profile from config.json."""
    cfg = load_config(path)
    profiles = cfg.get("profiles", {})
    if name not in profiles:
        return False
    del profiles[name]
    if cfg.get("current_profile") == name:
        cfg["current_profile"] = next(iter(profiles.keys()), "default")
    save_config(cfg, path)
    return True


def set_current_profile(name: str, path: Path | None = None) -> bool:
    """Switch the active profile if it exists in saved profiles."""
    cfg = load_config(path)
    profiles = cfg.get("profiles", {})
    if name not in profiles:
        return False
    cfg["current_profile"] = name
    save_config(cfg, path)
    return True

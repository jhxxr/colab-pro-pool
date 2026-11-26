"""Private, per-account OAuth storage shared with the CLI keyring adapter."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


OAUTH_TOKEN_FILENAME = "pool-oauth.json"


def oauth_token_path(home_dir: str | Path) -> Path:
    return Path(home_dir) / ".config" / "colab-cli" / OAUTH_TOKEN_FILENAME


def write_oauth_token(home_dir: str | Path, token_json: str) -> None:
    """Replace credentials atomically; never expose a partially written token."""
    target = oauth_token_path(home_dir)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".oauth-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(token_json)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)

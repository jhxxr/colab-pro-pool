"""Validation and safe presentation helpers for account-specific HTTP proxies."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


SUPPORTED_PROXY_SCHEMES = {"http", "https"}
_URL_CREDENTIALS_RE = re.compile(r"(?i)(https?://)[^\s/@]*@")


def validate_proxy_url(value: str) -> str:
    """Validate an HTTP(S) proxy URL without returning or logging its credentials."""
    url = value.strip()
    if not url or any(char.isspace() or char == "\x00" for char in url):
        raise ValueError("请输入有效的 HTTP 或 HTTPS 代理地址。")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError("代理地址格式无效，请检查主机名和端口。") from exc
    if parts.scheme.lower() not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError("代理仅支持 http:// 或 https:// 地址。")
    if not parts.hostname or port is None or not (1 <= port <= 65535):
        raise ValueError("代理地址必须包含有效主机名和端口。")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError("代理地址不能包含路径、查询参数或片段。")
    return url


def proxy_summary(proxy: dict[str, object]) -> dict[str, object]:
    """Return display-safe proxy metadata; never include username/password or URL."""
    parts = urlsplit(str(proxy["proxy_url"]))
    hostname = parts.hostname or ""
    host_display = f"[{hostname}]" if ":" in hostname else hostname
    endpoint = f"{parts.scheme.lower()}://{host_display}:{parts.port}"
    return {
        "proxy_id": proxy["proxy_id"],
        "name": proxy["name"],
        "endpoint": endpoint,
        "has_auth": parts.username is not None,
        "assigned_accounts": int(proxy.get("assigned_accounts", 0)),
        "created_at": proxy["created_at"],
        "updated_at": proxy["updated_at"],
    }


def redact_proxy_credentials(text: str) -> str:
    """Remove URL userinfo from CLI output before it is stored in job logs."""
    return _URL_CREDENTIALS_RE.sub(r"\1***@", text)

"""Standard Model Context Protocol (MCP) stdio server for Colab Pro Pool.

Connects local AI agents (Claude Code, Hermes Agent, Codex, Cursor, OpenClaw)
to your remote `colab-pro-pool` Linux server over HTTP/HTTPS.

Environment variables:
  COLAB_POOL_SERVER_URL: e.g. http://your-server-ip:8080 (default: http://127.0.0.1:8080)
  COLAB_POOL_API_KEY:    Restricted MCP key issued by the pool administrator
"""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
from pathlib import Path
from typing import Any

import httpx

from . import __version__
from .utils import pack_local_dir_b64, safe_extract_tar

MCP_PROTOCOL_VERSION = "2024-11-05"


def _client() -> httpx.Client:
    base_url = os.environ.get("COLAB_POOL_SERVER_URL", "http://127.0.0.1:8080").rstrip("/")
    api_key = os.environ.get("COLAB_POOL_API_KEY", "")
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return httpx.Client(base_url=base_url, headers=headers, timeout=120.0)


def _pack_local_dir_b64(local_dir: str) -> str:
    return pack_local_dir_b64(local_dir)


TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "name": "colab_pool_status",
        "description": (
            "Check this MCP key's remaining CU allowance and allowed GPU/TPU/CPU models, "
            "plus pool availability. Always check before submitting a job."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "submit_colab_job",
        "description": (
            "Submit a Python script (.py), notebook (.ipynb), or local project directory to run "
            "on an idle Google Colab Pro GPU/TPU account within this key's CU allowance and "
            "allowed models. Administrator credentials additionally support Google Drive "
            "mounting (`drive_profile`) to `/content/drive/MyDrive`. Supports running "
            "public dataset download commands inside Colab before training. Write outputs to "
            "`/content/outputs` (or `$COLAB_OUTPUT_DIR`) to retrieve them automatically."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Experiment name", "default": "ai-exp"},
                "gpu": {
                    "type": "string",
                    "description": "GPU accelerator: T4, L4, A100 (Pro), H100 or G4 (Pro+), or CPU",
                    "default": "T4",
                },
                "tpu": {
                    "type": "string",
                    "description": "Optional TPU type (v5e1, v6e1); overrides gpu when non-empty",
                    "default": "",
                },
                "high_mem": {"type": "boolean", "default": False},
                "priority": {
                    "type": "integer",
                    "default": 0,
                    "description": "Scheduling priority (-10 to 100; higher runs first)",
                },
                "entrypoint": {
                    "type": "string",
                    "description": "Entrypoint script/notebook relative to workspace (e.g. 'train.py')",
                    "default": "train.py",
                },
                "script_content": {
                    "type": "string",
                    "description": "Inline UTF-8 Python script content (optional if local_workspace_dir is provided)",
                },
                "local_workspace_dir": {
                    "type": "string",
                    "description": "Optional local directory path to tar.gz and upload as the job workspace",
                },
                "script_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CLI arguments passed to entrypoint",
                },
                "pip_packages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Packages installed via uv/pip before running",
                },
                "dataset_commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Shell commands executed inside Colab VM to download/extract public datasets",
                },
                "drive_profile": {
                    "type": "string",
                    "description": "Administrator-only rclone Drive profile; leave empty for restricted MCP keys",
                    "default": "",
                },
                "drive_subpath": {
                    "type": "string",
                    "description": "Optional subpath inside Google Drive to scope mount/sync",
                    "default": "",
                },
                "drive_sync_outputs_to": {
                    "type": "string",
                    "description": "Optional Google Drive folder path to copy /content/outputs into on exit",
                    "default": "",
                },
                "env_vars": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": "Extra environment variables passed to training script",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "default": 43200,
                    "description": "Maximum job runtime in seconds",
                },
            },
            "required": ["entrypoint"],
        },
    },
    {
        "name": "list_colab_jobs",
        "description": "List this key's recent Colab jobs, optionally filtered by status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Optional status filter: queued, provisioning, running, succeeded, failed, cancelled",
                },
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "get_colab_job",
        "description": "Get job status, assigned Pro account, output file list, and recent tail logs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "tail_lines": {"type": "integer", "default": 120},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "retry_colab_job",
        "description": "Clone and re-queue an existing Colab job (optionally overriding the GPU type).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "gpu": {"type": "string", "description": "Optional GPU override (e.g. A100, H100)"},
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "download_colab_artifacts",
        "description": "Download and extract `/content/outputs` artifacts for a completed job to a local directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "dest_dir": {"type": "string", "description": "Local directory to extract outputs into"},
            },
            "required": ["job_id", "dest_dir"],
        },
    },
    {
        "name": "cancel_colab_job",
        "description": "Cancel a queued or running Colab job and immediately stop its remote GPU VM.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
            },
            "required": ["job_id"],
        },
    },
]


def _handle_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    with _client() as client:
        if name == "colab_pool_status":
            resp = client.get("/api/v1/mcp/status")
            resp.raise_for_status()
            return resp.json()

        if name == "submit_colab_job":
            payload: dict[str, Any] = {
                "name": arguments.get("name", "ai-exp"),
                "gpu": arguments.get("gpu", "T4"),
                "tpu": arguments.get("tpu", ""),
                "high_mem": bool(arguments.get("high_mem", False)),
                "priority": int(arguments.get("priority", 0)),
                "entrypoint": arguments["entrypoint"],
                "script_args": arguments.get("script_args", []),
                "pip_packages": arguments.get("pip_packages", []),
                "dataset_commands": arguments.get("dataset_commands", []),
                "env_vars": arguments.get("env_vars", {}),
                "drive_profile": arguments.get("drive_profile", ""),
                "drive_subpath": arguments.get("drive_subpath", ""),
                "drive_sync_outputs_to": arguments.get("drive_sync_outputs_to", ""),
                "timeout_seconds": int(arguments.get("timeout_seconds", 43200)),
            }
            if arguments.get("script_content") is not None:
                payload["script_content"] = arguments["script_content"]
            if arguments.get("local_workspace_dir"):
                payload["workspace_tar_gz_b64"] = _pack_local_dir_b64(
                    arguments["local_workspace_dir"]
                )
            resp = client.post("/api/v1/jobs", json=payload)
            resp.raise_for_status()
            return resp.json()

        if name == "list_colab_jobs":
            params: dict[str, Any] = {"limit": int(arguments.get("limit", 20))}
            if arguments.get("status"):
                params["status"] = arguments["status"]
            resp = client.get("/api/v1/jobs", params=params)
            resp.raise_for_status()
            return resp.json()

        if name == "get_colab_job":
            job_id = arguments["job_id"]
            tail_lines = int(arguments.get("tail_lines", 120))
            j_resp = client.get(f"/api/v1/jobs/{job_id}")
            j_resp.raise_for_status()
            l_resp = client.get(f"/api/v1/jobs/{job_id}/logs", params={"tail_lines": tail_lines})
            l_resp.raise_for_status()
            result = j_resp.json()
            result["recent_logs"] = l_resp.json().get("logs", "")
            return result

        if name == "retry_colab_job":
            job_id = arguments["job_id"]
            body: dict[str, Any] = {}
            if arguments.get("gpu"):
                body["gpu"] = arguments["gpu"]
            resp = client.post(f"/api/v1/jobs/{job_id}/retry", json=body)
            resp.raise_for_status()
            return resp.json()

        if name == "download_colab_artifacts":
            job_id = arguments["job_id"]
            dest = Path(arguments["dest_dir"]).expanduser().resolve()
            dest.mkdir(parents=True, exist_ok=True)
            resp = client.get(f"/api/v1/jobs/{job_id}/artifacts")
            resp.raise_for_status()
            with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tf:
                safe_extract_tar(tf, dest)
            extracted = [
                p.relative_to(dest).as_posix()
                for p in sorted(dest.rglob("*"))
                if p.is_file()
            ]
            return {"job_id": job_id, "dest_dir": str(dest), "extracted_files": extracted}

        if name == "cancel_colab_job":
            job_id = arguments["job_id"]
            resp = client.post(f"/api/v1/jobs/{job_id}/cancel")
            resp.raise_for_status()
            return resp.json()

    raise ValueError(f"Unknown tool: {name}")


def handle_jsonrpc_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Process a single MCP JSON-RPC 2.0 message; returns None for notifications."""
    method = msg.get("method", "")
    msg_id = msg.get("id")

    if msg_id is None:
        return None

    try:
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "colab-pro-pool", "version": __version__},
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS_SCHEMA}}
        if method == "tools/call":
            params = msg.get("params", {})
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {}) or {}
            try:
                data = _handle_tool_call(tool_name, arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}
                        ],
                        "isError": False,
                    },
                }
            except Exception as tool_exc:
                error = str(tool_exc)
                if isinstance(tool_exc, httpx.HTTPStatusError):
                    try:
                        error = str(tool_exc.response.json().get("detail", error))
                    except (ValueError, AttributeError):
                        pass
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Error: {error}"}],
                        "isError": True,
                    },
                }

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32603, "message": str(exc)},
        }


def main() -> None:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        msg = json.loads(line)
        reply = handle_jsonrpc_message(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

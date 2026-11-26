#!/usr/bin/env python3
"""Smoke-test the installed MCP entry point and its JSON-RPC stdio protocol."""

from __future__ import annotations

import importlib.metadata
import json
import shutil
import subprocess
import sys


def main() -> None:
    scripts = {
        entry.name: entry.value
        for entry in importlib.metadata.distribution("colab-pro-pool").entry_points
        if entry.group == "console_scripts"
    }
    assert scripts.get("colab-pool-mcp") == "colab_pool.mcp_server:main", scripts
    assert all(name not in scripts for name in ("cpool", "colab-pool", "colab-pool-client")), scripts

    executable = sys.argv[1] if len(sys.argv) > 1 else shutil.which("colab-pool-mcp")
    assert executable, "colab-pool-mcp entry point is not installed"
    requests = "".join(
        json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method}) + "\n"
        for req_id, method in ((1, "initialize"), (2, "tools/list"))
    )
    result = subprocess.run(
        [executable], input=requests, text=True, capture_output=True, check=True, timeout=10
    )
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(replies) == 2, result.stdout
    assert replies[0]["result"]["serverInfo"]["name"] == "colab-pro-pool"
    tool_names = {tool["name"] for tool in replies[1]["result"]["tools"]}
    assert "colab_pool_status" in tool_names and "submit_colab_job" in tool_names
    print("MCP entry point, initialization and tool listing passed.")


if __name__ == "__main__":
    main()

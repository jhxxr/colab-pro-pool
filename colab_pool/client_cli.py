"""Lightweight CLI client for AI bash agents & researchers to submit Colab jobs."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tarfile
import time
from pathlib import Path

import httpx

from .utils import pack_local_dir_b64, safe_extract_tar


def _make_client(server_url: str, api_key: str) -> httpx.Client:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return httpx.Client(base_url=server_url.rstrip("/"), headers=headers, timeout=120.0)


def _parse_env_pairs(raw_pairs: list[str]) -> dict[str, str]:
    env_vars: dict[str, str] = {}
    for item in raw_pairs:
        if "=" not in item:
            raise ValueError(f"Invalid --env entry '{item}'; expected KEY=VALUE.")
        key, val = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Empty variable name in --env entry '{item}'.")
        env_vars[key] = val
    return env_vars


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Colab Pro Pool Client CLI")
    parser.add_argument(
        "--server",
        default=os.environ.get("COLAB_POOL_SERVER_URL", "http://127.0.0.1:8080"),
        help="Colab Pool server base URL",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("COLAB_POOL_API_KEY", ""),
        help="Bearer API key",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pool", help="Show Pro account pool status and Drive profiles")

    p_jobs = sub.add_parser("jobs", help="List recent jobs")
    p_jobs.add_argument("--status", default="", help="Filter by status")
    p_jobs.add_argument("--limit", type=int, default=20, help="Max jobs to show")

    p_sub = sub.add_parser("submit", help="Submit a script or local directory to Colab Pro Pool")
    p_sub.add_argument("target", help="Local .py/.ipynb file or project directory")
    p_sub.add_argument("--entrypoint", default="", help="Entrypoint inside directory (default: target filename or train.py)")
    p_sub.add_argument("--name", default="cli-exp", help="Experiment name")
    p_sub.add_argument("--gpu", default="T4", help="GPU type: T4, L4, A100, H100, G4, or CPU")
    p_sub.add_argument("--tpu", default="", help="Optional TPU type: v5e1, v6e1")
    p_sub.add_argument("--high-mem", action="store_true", help="Request high-RAM VM")
    p_sub.add_argument("--priority", type=int, default=0, help="Scheduling priority (-10 to 100)")
    p_sub.add_argument("--drive", default="", help="Pre-authorized rclone Drive profile name")
    p_sub.add_argument("--drive-subpath", default="", help="Optional subpath inside Google Drive")
    p_sub.add_argument("--sync-outputs-to", default="", help="Copy /content/outputs to this Google Drive folder on finish")
    p_sub.add_argument("--pip", action="append", default=[], help="Extra pip package (repeatable)")
    p_sub.add_argument("--dataset-cmd", action="append", default=[], help="Shell command to download public datasets in Colab (repeatable)")
    p_sub.add_argument("--arg", action="append", default=[], help="Argument passed to entrypoint script (repeatable)")
    p_sub.add_argument("--env", action="append", default=[], help="Environment variable KEY=VALUE (repeatable)")
    p_sub.add_argument("--wait", action="store_true", help="Wait and stream logs until completion")
    p_sub.add_argument("--out-dir", default="", help="Download /content/outputs into this local directory when --wait finishes")

    p_logs = sub.add_parser("logs", help="Fetch job logs")
    p_logs.add_argument("job_id")
    p_logs.add_argument("--tail", type=int, default=200)

    p_pull = sub.add_parser("pull", help="Download and extract job outputs")
    p_pull.add_argument("job_id")
    p_pull.add_argument("dest_dir")

    p_cancel = sub.add_parser("cancel", help="Cancel job and release Colab VM")
    p_cancel.add_argument("job_id")

    p_retry = sub.add_parser("retry", help="Retry an existing job")
    p_retry.add_argument("job_id")
    p_retry.add_argument("--gpu", default="", help="Optional GPU override")

    args = parser.parse_args(argv)

    with _make_client(args.server, args.api_key) as client:
        if args.cmd == "pool":
            r = client.get("/api/v1/accounts")
            r.raise_for_status()
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "jobs":
            params: dict[str, object] = {"limit": args.limit}
            if args.status:
                params["status"] = args.status
            r = client.get("/api/v1/jobs", params=params)
            r.raise_for_status()
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "submit":
            try:
                env_vars = _parse_env_pairs(args.env)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 2

            target = Path(args.target).expanduser().resolve()
            payload: dict = {
                "name": args.name,
                "gpu": args.gpu,
                "tpu": args.tpu,
                "high_mem": args.high_mem,
                "priority": args.priority,
                "pip_packages": args.pip,
                "dataset_commands": args.dataset_cmd,
                "script_args": args.arg,
                "env_vars": env_vars,
                "drive_profile": args.drive,
                "drive_subpath": args.drive_subpath,
                "drive_sync_outputs_to": args.sync_outputs_to,
            }
            if target.is_dir():
                payload["entrypoint"] = args.entrypoint or "train.py"
                payload["workspace_tar_gz_b64"] = pack_local_dir_b64(str(target))
            elif target.is_file():
                payload["entrypoint"] = args.entrypoint or target.name
                payload["script_content"] = target.read_text(encoding="utf-8")
            else:
                print(f"Target not found: {target}", file=sys.stderr)
                return 1

            r = client.post("/api/v1/jobs", json=payload)
            r.raise_for_status()
            job = r.json()
            job_id = job["job_id"]
            print(json.dumps(job, ensure_ascii=False, indent=2))

            if not args.wait:
                return 0

            log_offset = 0
            while True:
                jr = client.get(f"/api/v1/jobs/{job_id}")
                jr.raise_for_status()
                jstate = jr.json()

                while True:
                    lr = client.get(
                        f"/api/v1/jobs/{job_id}/logs",
                        params={"offset": log_offset},
                    )
                    if lr.status_code != 200:
                        break
                    ldata = lr.json()
                    chunk = ldata.get("logs", "")
                    next_offset = int(ldata.get("next_offset", log_offset))
                    if not chunk:
                        break
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    log_offset = next_offset

                if jstate["status"] in {"succeeded", "failed", "cancelled"}:
                    print(
                        f"\n[colab-pool-client] Final status: {jstate['status']} "
                        f"(account={jstate['assigned_account']})"
                    )
                    if args.out_dir and jstate.get("artifacts_archive"):
                        _download_and_extract(client, job_id, Path(args.out_dir))
                    return 0 if jstate["status"] == "succeeded" else 1
                time.sleep(3.0)

        if args.cmd == "logs":
            r = client.get(f"/api/v1/jobs/{args.job_id}/logs", params={"tail_lines": args.tail})
            r.raise_for_status()
            print(r.json().get("logs", ""))
            return 0

        if args.cmd == "pull":
            _download_and_extract(client, args.job_id, Path(args.dest_dir))
            return 0

        if args.cmd == "cancel":
            r = client.post(f"/api/v1/jobs/{args.job_id}/cancel")
            r.raise_for_status()
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "retry":
            body = {"gpu": args.gpu} if args.gpu else {}
            r = client.post(f"/api/v1/jobs/{args.job_id}/retry", json=body)
            r.raise_for_status()
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
            return 0

    return 0


def _download_and_extract(client: httpx.Client, job_id: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    r = client.get(f"/api/v1/jobs/{job_id}/artifacts")
    r.raise_for_status()
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
        safe_extract_tar(tf, dest)
    print(f"[colab-pool-client] Extracted artifacts into {dest.resolve()}")


if __name__ == "__main__":
    raise SystemExit(main())

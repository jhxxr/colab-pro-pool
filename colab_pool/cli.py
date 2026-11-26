"""Unified `cpool` / `colab-pool` CLI for Colab Pro Pool."""

from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import unicodedata
import webbrowser
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import httpx

from .cli_config import (
    DEFAULT_GPU,
    DEFAULT_SERVER_URL,
    DEFAULT_TIMEOUT_SECONDS,
    delete_profile,
    get_config_path,
    load_config,
    resolve_profile,
    save_profile,
    set_current_profile,
)
from .utils import pack_local_dir_b64, safe_extract_tar, safe_join


def _use_color(as_json: bool) -> bool:
    return (not as_json) and sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _style(text: str, code: str, *, color: bool) -> str:
    if not color:
        return text
    return f"\033[{code}m{text}\033[0m"


def _status_style(status: str, *, color: bool) -> str:
    palette = {
        "idle": "32",
        "succeeded": "32",
        "running": "36",
        "busy": "36",
        "provisioning": "33",
        "queued": "33",
        "cooldown": "33",
        "failed": "31",
        "cancelled": "90",
        "disabled": "90",
    }
    code = palette.get(status.lower(), "0")
    return _style(status, code, color=color) if code != "0" else status


def _disp_width(text: str) -> int:
    w = 0
    for ch in text:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _truncate_disp(text: str, max_width: int) -> str:
    if max_width <= 3 or _disp_width(text) <= max_width:
        return text
    out: list[str] = []
    cur = 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if cur + cw > max_width - 3:
            break
        out.append(ch)
        cur += cw
    return "".join(out) + "..."


def _pad_disp(text: str, width: int) -> str:
    diff = max(0, width - _disp_width(text))
    return text + (" " * diff)


def _print_table(
    headers: list[str],
    rows: list[list[str]],
    *,
    max_col_widths: list[int] | None = None,
) -> None:
    if not rows:
        print("(empty)")
        return
    clean_rows: list[list[str]] = []
    for row in rows:
        cleaned = []
        for idx, cell in enumerate(row):
            s = str(cell).replace("\r", " ").replace("\n", " ")
            if max_col_widths and idx < len(max_col_widths) and max_col_widths[idx] > 0:
                s = _truncate_disp(s, max_col_widths[idx])
            cleaned.append(s)
        clean_rows.append(cleaned)

    widths = [_disp_width(h) for h in headers]
    for row in clean_rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], _disp_width(cell))

    header_line = "  ".join(_pad_disp(h, widths[i]) for i, h in enumerate(headers))
    sep_line = "  ".join("-" * widths[i] for i in range(len(headers)))
    print(header_line)
    print(sep_line)
    for row in clean_rows:
        print("  ".join(_pad_disp(cell, widths[i]) for i, cell in enumerate(row)))


def _fmt_bytes(num_bytes: int | float) -> str:
    val = float(num_bytes)
    if val <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while val >= 1024.0 and idx < len(units) - 1:
        val /= 1024.0
        idx += 1
    return f"{val:.0f} {units[idx]}" if (idx == 0 or val >= 10) else f"{val:.1f} {units[idx]}"


def _fmt_duration(seconds: float | int | None) -> str:
    if seconds is None or seconds < 0:
        return "-"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    if m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _fmt_time(epoch: float | None) -> str:
    if not epoch:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def _mask_secret(secret: str) -> str:
    if not secret:
        return "(none)"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}****{secret[-4:]}"


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


def _make_client(
    server_url: str, api_key: str, *, streaming: bool = False
) -> httpx.Client:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    timeout = (
        httpx.Timeout(connect=15.0, read=None, write=15.0, pool=15.0)
        if streaming
        else httpx.Timeout(120.0)
    )
    return httpx.Client(
        base_url=server_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
    )


def _iter_sse_events(resp: httpx.Response) -> Iterator[tuple[str, dict[str, Any]]]:
    event_name = "message"
    data_lines: list[str] = []
    for raw_line in resp.iter_lines():
        line = raw_line.rstrip("\r")
        if line == "":
            if data_lines:
                raw_data = "\n".join(data_lines)
                try:
                    payload = json.loads(raw_data)
                except json.JSONDecodeError:
                    payload = {"raw": raw_data}
                yield (event_name, payload if isinstance(payload, dict) else {"value": payload})
                event_name = "message"
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        raw_data = "\n".join(data_lines)
        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError:
            payload = {"raw": raw_data}
        yield (event_name, payload if isinstance(payload, dict) else {"value": payload})


def _stream_job_until_done(
    client: httpx.Client,
    job_id: str,
    *,
    initial_tail: int | None = 200,
    quiet: bool = False,
    stream_logs: bool = True,
) -> dict[str, Any]:
    cursor = 0
    last_status_sig: tuple[Any, ...] | None = None

    if stream_logs and initial_tail is not None and initial_tail > 0:
        lr = client.get(f"/api/v1/jobs/{job_id}/logs", params={"tail_lines": initial_tail})
        lr.raise_for_status()
        ldata = lr.json()
        tail_txt = ldata.get("logs", "")
        if tail_txt and not quiet:
            sys.stdout.write(tail_txt)
            if not tail_txt.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
        cursor = int(ldata.get("next_offset", 0))
        if ldata.get("status") in {"succeeded", "failed", "cancelled"}:
            jr = client.get(f"/api/v1/jobs/{job_id}")
            jr.raise_for_status()
            return jr.json()

    with client.stream(
        "GET", f"/api/v1/jobs/{job_id}/logs/stream", params={"offset": cursor}
    ) as resp:
        resp.raise_for_status()
        for event_name, payload in _iter_sse_events(resp):
            if event_name == "log":
                chunk = payload.get("chunk", "")
                if chunk and stream_logs and not quiet:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
            elif event_name == "status":
                sig = (
                    payload.get("status"),
                    payload.get("assigned_account"),
                    payload.get("gpu_detected"),
                )
                if sig != last_status_sig and not quiet:
                    last_status_sig = sig
                    acct = payload.get("assigned_account") or "-"
                    gpu_det = payload.get("gpu_detected") or ""
                    extra = f" gpu={gpu_det}" if gpu_det else ""
                    print(
                        f"[cpool] status={payload.get('status')} account={acct}{extra}",
                        file=sys.stderr,
                        flush=True,
                    )
            elif event_name == "done":
                break

    jr = client.get(f"/api/v1/jobs/{job_id}")
    jr.raise_for_status()
    return jr.json()


def _download_archive_or_file(
    client: httpx.Client,
    job_id: str,
    dest_dir: Path,
    *,
    single_file: str | None = None,
) -> list[str]:
    dest = dest_dir.expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    if single_file:
        rel = single_file.lstrip("/")
        target = safe_join(dest, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        rel_encoded = "/".join(quote(part, safe="") for part in rel.split("/") if part)
        resp = client.get(f"/api/v1/jobs/{job_id}/files/{rel_encoded}")
        resp.raise_for_status()
        target.write_bytes(resp.content)
        return [target.relative_to(dest).as_posix()]

    resp = client.get(f"/api/v1/jobs/{job_id}/artifacts")
    resp.raise_for_status()
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tf:
        safe_extract_tar(tf, dest)
    return [
        p.relative_to(dest).as_posix()
        for p in sorted(dest.rglob("*"))
        if p.is_file()
    ]


def _emit_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _format_http_error(exc: httpx.HTTPStatusError) -> str:
    status = exc.response.status_code
    detail = ""
    try:
        body = exc.response.json()
        if isinstance(body, dict) and "detail" in body:
            detail = str(body["detail"])
    except Exception:
        detail = exc.response.text.strip()[:300]
    msg = f"HTTP {status}: {detail}" if detail else f"HTTP {status}"
    if status == 401:
        msg += " (run `cpool login` or pass --api-key)"
    return msg


# -------------------- Subcommand Handlers --------------------


def _cmd_login(args: argparse.Namespace, profile_name: str, prof: dict[str, Any]) -> int:
    target_server = (
        (args.server_arg or "").strip()
        or getattr(args, "server", None)
        or prof["server"]
        or DEFAULT_SERVER_URL
    ).rstrip("/")
    api_key = getattr(args, "api_key", None)
    if api_key is None:
        api_key = prof["api_key"]
    api_key = api_key.strip()

    default_gpu = (args.gpu or prof["default_gpu"] or DEFAULT_GPU).strip().upper()
    default_timeout = int(args.timeout if args.timeout is not None else prof["default_timeout"])

    if not args.no_verify:
        with _make_client(target_server, api_key) as client:
            resp = client.get("/api/v1/auth/check")
            resp.raise_for_status()
            chk = resp.json()
            if chk.get("auth_required") and not chk.get("authenticated"):
                if not api_key and sys.stdin.isatty() and not args.json:
                    api_key = getpass.getpass("API Key: ").strip()
                    with _make_client(target_server, api_key) as client2:
                        resp2 = client2.get("/api/v1/auth/check")
                        resp2.raise_for_status()
                        chk = resp2.json()
                if not chk.get("authenticated"):
                    raise ValueError("Authentication failed: invalid or missing API key.")

    saved_path = save_profile(
        profile_name,
        server=target_server,
        api_key=api_key,
        default_gpu=default_gpu,
        default_timeout=default_timeout,
        set_current=True,
    )
    if args.json:
        _emit_json(
            {
                "profile": profile_name,
                "server": target_server,
                "default_gpu": default_gpu,
                "default_timeout": default_timeout,
                "config_path": str(saved_path),
            }
        )
    else:
        print(f"Saved profile '{profile_name}' -> {target_server} (config: {saved_path})")
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    cfg_cmd = getattr(args, "cfg_cmd", None) or "show"
    cfg_path = get_config_path()

    if cfg_cmd == "path":
        if args.json:
            _emit_json({"config_path": str(cfg_path)})
        else:
            print(str(cfg_path))
        return 0

    if cfg_cmd == "use":
        if not set_current_profile(args.name):
            raise ValueError(f"Profile '{args.name}' does not exist.")
        if args.json:
            _emit_json({"current_profile": args.name})
        else:
            print(f"Active profile switched to '{args.name}'.")
        return 0

    if cfg_cmd == "set-gpu":
        profile_name, prof = resolve_profile(getattr(args, "profile", None))
        gpu = args.gpu.strip().upper()
        save_profile(
            profile_name,
            server=prof["server"],
            api_key=prof["api_key"],
            default_gpu=gpu,
            default_timeout=prof["default_timeout"],
            set_current=False,
        )
        if args.json:
            _emit_json({"profile": profile_name, "default_gpu": gpu})
        else:
            print(f"Updated default_gpu={gpu} for profile '{profile_name}'.")
        return 0

    if cfg_cmd == "logout":
        target = args.name or resolve_profile(getattr(args, "profile", None))[0]
        removed = delete_profile(target)
        if not removed:
            raise ValueError(f"Profile '{target}' not found.")
        if args.json:
            _emit_json({"deleted_profile": target})
        else:
            print(f"Removed profile '{target}'.")
        return 0

    # show
    cfg = load_config()
    if args.json:
        masked_profiles = {}
        for pname, pval in cfg.get("profiles", {}).items():
            entry = dict(pval)
            entry["api_key"] = _mask_secret(str(entry.get("api_key", "")))
            masked_profiles[pname] = entry
        _emit_json(
            {
                "config_path": str(cfg_path),
                "current_profile": cfg.get("current_profile", "default"),
                "profiles": masked_profiles,
            }
        )
        return 0

    print(f"Config file   : {cfg_path}")
    print(f"Active profile: {cfg.get('current_profile', 'default')}")
    profiles = cfg.get("profiles", {})
    if not profiles:
        print("Saved profiles: (none - run `cpool login` to save a profile)")
        return 0
    rows = []
    cur = cfg.get("current_profile", "default")
    for pname, pval in profiles.items():
        mark = "*" if pname == cur else ""
        rows.append(
            [
                f"{mark}{pname}",
                str(pval.get("server", "")),
                _mask_secret(str(pval.get("api_key", ""))),
                str(pval.get("default_gpu", DEFAULT_GPU)),
                str(pval.get("default_timeout", DEFAULT_TIMEOUT_SECONDS)),
            ]
        )
    print()
    _print_table(["PROFILE", "SERVER", "API_KEY", "GPU", "TIMEOUT"], rows)
    return 0


def _cmd_tunnel(args: argparse.Namespace) -> int:
    cmd = [
        "ssh",
        "-N",
        "-L",
        f"{args.local_port}:{args.remote_host}:{args.remote_port}",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ExitOnForwardFailure=yes",
    ]
    for opt in args.ssh_opt:
        cmd.extend(["-o", opt])
    cmd.append(args.ssh_target)

    if args.print_only:
        if args.json:
            _emit_json({"command": cmd, "shell": shlex.join(cmd)})
        else:
            print(shlex.join(cmd))
        return 0

    if not shutil.which("ssh"):
        raise ValueError("`ssh` binary not found in PATH.")

    print(
        f"[cpool] Opening SSH tunnel http://127.0.0.1:{args.local_port} -> "
        f"{args.ssh_target} ({args.remote_host}:{args.remote_port}) ..."
    )
    print("[cpool] Press Ctrl+C to close tunnel.")
    try:
        proc = subprocess.run(cmd)
        return proc.returncode
    except KeyboardInterrupt:
        print("\n[cpool] Tunnel closed.")
        return 0


def _cmd_open(args: argparse.Namespace, prof: dict[str, Any]) -> int:
    url = prof["server"]
    opened = webbrowser.open(url)
    if args.json:
        _emit_json({"url": url, "opened": bool(opened)})
    else:
        print(f"Web Console: {url}" + ("" if opened else " (copy URL into browser)"))
    return 0


def _cmd_status(args: argparse.Namespace, prof: dict[str, Any]) -> int:
    with _make_client(prof["server"], prof["api_key"]) as client:
        sr = client.get("/api/v1/stats")
        sr.raise_for_status()
        ar = client.get("/api/v1/accounts")
        ar.raise_for_status()
        stats = sr.json()
        accounts_payload = ar.json()

    if args.json:
        _emit_json({"stats": stats, **accounts_payload})
        return 0

    color = _use_color(False)
    acct_s = stats["accounts"]
    job_s = stats["jobs"]
    rate = (
        f"{stats['success_rate'] * 100:.1f}%"
        if stats.get("success_rate") is not None
        else "-"
    )
    print(f"Server : {prof['server']}")
    print(
        f"Accounts: {acct_s['idle']} idle / {acct_s['total']} total "
        f"({acct_s['busy']} busy, {acct_s['cooldown']} cooldown, {acct_s['disabled']} disabled)"
    )
    print(
        f"Jobs    : {job_s['running']} running, {job_s['provisioning']} provisioning, "
        f"{job_s['queued']} queued | {job_s['succeeded']} ok / {job_s['failed']} fail "
        f"(success {rate}, avg {_fmt_duration(stats.get('avg_duration_seconds'))}, "
        f"disk {_fmt_bytes(stats.get('jobs_disk_bytes', 0))})"
    )
    print()

    accounts = accounts_payload.get("accounts", [])
    rows = []
    for a in accounts:
        effective = a["status"] if a.get("enabled", 1) else "disabled"
        cd_rem = a.get("cooldown_remaining_seconds", 0)
        cd_str = _fmt_duration(cd_rem) if cd_rem > 0 else "-"
        auth_str = "yes" if a.get("auth_configured") else "no"
        detail = (
            f"job:{a['current_job_id']}"
            if a.get("current_job_id")
            else (a.get("last_error") or "-")
        )
        rows.append(
            [
                a["name"],
                a["tier"],
                _status_style(effective, color=color),
                auth_str,
                cd_str,
                f"{a.get('total_runs', 0)}/{a.get('total_failures', 0)}",
                detail,
            ]
        )
    _print_table(
        ["ACCOUNT", "TIER", "STATUS", "AUTH", "COOLDOWN", "OK/FAIL", "JOB / LAST_ERROR"],
        rows,
        max_col_widths=[20, 6, 12, 5, 10, 8, 44],
    )
    return 0


def _cmd_accounts(args: argparse.Namespace, prof: dict[str, Any]) -> int:
    sub = getattr(args, "acct_cmd", None) or "list"
    with _make_client(prof["server"], prof["api_key"]) as client:
        if sub == "list":
            r = client.get("/api/v1/accounts")
            r.raise_for_status()
            data = r.json()
            if args.json:
                _emit_json(data)
                return 0
            color = _use_color(False)
            rows = []
            for a in data.get("accounts", []):
                effective = a["status"] if a.get("enabled", 1) else "disabled"
                rows.append(
                    [
                        a["name"],
                        a["tier"],
                        _status_style(effective, color=color),
                        "yes" if a.get("auth_configured") else "no",
                        _fmt_duration(a.get("cooldown_remaining_seconds", 0))
                        if a.get("cooldown_remaining_seconds", 0) > 0
                        else "-",
                        f"{a.get('total_runs', 0)}/{a.get('total_failures', 0)}",
                        a.get("last_error") or "-",
                    ]
                )
            _print_table(
                ["ACCOUNT", "TIER", "STATUS", "AUTH", "COOLDOWN", "OK/FAIL", "LAST_ERROR"],
                rows,
                max_col_widths=[20, 6, 12, 5, 10, 8, 44],
            )
            return 0

        if sub == "add":
            r = client.post(
                "/api/v1/accounts",
                json={"name": args.name, "tier": args.tier},
            )
            r.raise_for_status()
            acct = r.json()
            if args.json:
                _emit_json(acct)
            else:
                print(f"Registered account '{acct['name']}' (tier={acct['tier']}).")
                print(
                    f"Authorize on server: ./scripts/init_pro_account.sh {acct['name']} {acct['tier']}"
                )
            return 0

        if sub in {"enable", "disable", "reset", "tier"}:
            patch: dict[str, Any] = {}
            if sub == "enable":
                patch["enabled"] = True
            elif sub == "disable":
                patch["enabled"] = False
            elif sub == "reset":
                patch["clear_cooldown"] = True
            elif sub == "tier":
                patch["tier"] = args.tier_value
            r = client.patch(f"/api/v1/accounts/{args.name}", json=patch)
            r.raise_for_status()
            acct = r.json()
            if args.json:
                _emit_json(acct)
            else:
                print(
                    f"Account '{acct['name']}': tier={acct['tier']} "
                    f"enabled={bool(acct['enabled'])} status={acct['status']}"
                )
            return 0

        if sub == "rm":
            r = client.delete(
                f"/api/v1/accounts/{args.name}",
                params={"remove_disk": "true" if args.remove_disk else "false"},
            )
            r.raise_for_status()
            res = r.json()
            if args.json:
                _emit_json(res)
            else:
                print(f"Deleted account '{args.name}'.")
            return 0

        if sub == "sync":
            r = client.post("/api/v1/pool/sync")
            r.raise_for_status()
            res = r.json()
            if args.json:
                _emit_json(res)
            else:
                print(f"Synced {len(res.get('accounts', []))} account(s) from disk.")
            return 0

        if sub == "cleanup":
            r = client.post("/api/v1/pool/cleanup")
            r.raise_for_status()
            res = r.json()
            if args.json:
                _emit_json(res)
            else:
                results = res.get("results", {})
                if not results:
                    print("No accounts required orphan cleanup.")
                else:
                    rows = [[k, str(v)] for k, v in results.items()]
                    _print_table(["ACCOUNT", "CLEANUP_RESULT"], rows)
            return 0

    raise ValueError(f"Unknown accounts subcommand: {sub}")


def _cmd_run(args: argparse.Namespace, prof: dict[str, Any]) -> int:
    env_vars = _parse_env_pairs(args.env)
    target = Path(args.target).expanduser().resolve()
    gpu = (args.gpu or prof["default_gpu"]).upper()
    timeout_s = int(args.timeout if args.timeout is not None else prof["default_timeout"])

    payload: dict[str, Any] = {
        "name": args.name or target.stem or "cli-exp",
        "gpu": gpu,
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
        "timeout_seconds": timeout_s,
    }
    if target.is_dir():
        payload["entrypoint"] = args.entrypoint or "train.py"
        payload["workspace_tar_gz_b64"] = pack_local_dir_b64(target)
    elif target.is_file():
        payload["entrypoint"] = args.entrypoint or target.name
        payload["script_content"] = target.read_text(encoding="utf-8")
    else:
        raise ValueError(f"Target not found: {target}")

    should_wait = bool(args.wait or args.out_dir)
    with _make_client(prof["server"], prof["api_key"], streaming=should_wait) as client:
        r = client.post("/api/v1/jobs", json=payload)
        r.raise_for_status()
        job = r.json()
        job_id = job["job_id"]

        if not should_wait:
            if args.json:
                _emit_json(job)
            else:
                print(
                    f"Submitted job {job_id} (name={job['name']}, gpu={job['gpu']}, "
                    f"priority={job['priority']}, status={job['status']})"
                )
                print(f"Follow logs: cpool logs {job_id} -f")
            return 0

        if not args.json:
            print(
                f"[cpool] Submitted {job_id} (gpu={job['gpu']}, entrypoint={job['entrypoint']})",
                file=sys.stderr,
                flush=True,
            )
        try:
            final_job = _stream_job_until_done(
                client, job_id, initial_tail=None, quiet=bool(args.json), stream_logs=True
            )
        except KeyboardInterrupt:
            print(
                "\n[cpool] Detached from live stream; remote job continues running.",
                file=sys.stderr,
            )
            return 130

        downloaded: list[str] = []
        if args.out_dir and final_job.get("artifacts_archive"):
            downloaded = _download_archive_or_file(
                client, job_id, Path(args.out_dir)
            )
            if not args.json:
                print(
                    f"[cpool] Extracted {len(downloaded)} artifact(s) to {Path(args.out_dir).resolve()}",
                    file=sys.stderr,
                )

        if args.json:
            if downloaded:
                final_job["downloaded_files"] = downloaded
            _emit_json(final_job)
        else:
            print(
                f"\n[cpool] Final status: {final_job['status']} "
                f"(account={final_job.get('assigned_account') or '-'}, "
                f"exit_code={final_job.get('exit_code')})",
                file=sys.stderr,
            )
        return 0 if final_job["status"] == "succeeded" else 1


def _cmd_exec(args: argparse.Namespace, prof: dict[str, Any]) -> int:
    env_vars = _parse_env_pairs(args.env)
    if args.code is not None:
        code = args.code
    elif args.file:
        p = Path(args.file).expanduser().resolve()
        if not p.is_file():
            raise ValueError(f"Script file not found: {p}")
        code = p.read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        code = sys.stdin.read()
    else:
        raise ValueError("Provide -c/--code, -f/--file, or pipe Python code via stdin.")

    gpu = (args.gpu or prof["default_gpu"]).upper()
    timeout_s = int(args.timeout if args.timeout is not None else prof["default_timeout"])
    payload: dict[str, Any] = {
        "name": args.name,
        "gpu": gpu,
        "tpu": args.tpu,
        "high_mem": args.high_mem,
        "priority": args.priority,
        "entrypoint": "inline_exec.py",
        "script_content": code,
        "pip_packages": args.pip,
        "dataset_commands": args.dataset_cmd,
        "env_vars": env_vars,
        "timeout_seconds": timeout_s,
    }
    should_wait = not args.detach
    with _make_client(prof["server"], prof["api_key"], streaming=should_wait) as client:
        r = client.post("/api/v1/jobs", json=payload)
        r.raise_for_status()
        job = r.json()
        job_id = job["job_id"]

        if args.detach:
            if args.json:
                _emit_json(job)
            else:
                print(f"Submitted job {job_id} (status={job['status']})")
            return 0

        try:
            final_job = _stream_job_until_done(
                client, job_id, initial_tail=None, quiet=bool(args.json), stream_logs=True
            )
        except KeyboardInterrupt:
            print(
                "\n[cpool] Detached from live stream; remote job continues running.",
                file=sys.stderr,
            )
            return 130

        if args.out_dir and final_job.get("artifacts_archive"):
            _download_archive_or_file(client, job_id, Path(args.out_dir))

        if args.json:
            _emit_json(final_job)
        return 0 if final_job["status"] == "succeeded" else 1


def _cmd_jobs(args: argparse.Namespace, prof: dict[str, Any]) -> int:
    params: dict[str, Any] = {"limit": args.limit, "offset": args.offset}
    if args.status:
        params["status"] = args.status
    if args.gpu:
        params["gpu"] = args.gpu
    if args.account:
        params["account"] = args.account
    if args.search:
        params["search"] = args.search

    with _make_client(prof["server"], prof["api_key"]) as client:
        r = client.get("/api/v1/jobs", params=params)
        r.raise_for_status()
        data = r.json()

    if args.json:
        _emit_json(data)
        return 0

    color = _use_color(False)
    jobs = data.get("jobs", [])
    rows = []
    now = time.time()
    for j in jobs:
        dur = (
            (j["finished_at"] or now) - j["started_at"]
            if j.get("started_at")
            else None
        )
        hw = f"TPU:{j['tpu']}" if j.get("tpu") else j["gpu"]
        rows.append(
            [
                j["job_id"],
                j["name"],
                hw,
                _status_style(j["status"], color=color),
                j.get("assigned_account") or "-",
                _fmt_duration(dur),
                _fmt_time(j.get("created_at")),
            ]
        )
    _print_table(
        ["JOB_ID", "NAME", "HW", "STATUS", "ACCOUNT", "DURATION", "CREATED"],
        rows,
        max_col_widths=[14, 24, 10, 14, 16, 10, 20],
    )
    print(f"\nShowing {len(jobs)} of {data.get('total', len(jobs))} job(s).")
    return 0


def _cmd_show(args: argparse.Namespace, prof: dict[str, Any]) -> int:
    with _make_client(prof["server"], prof["api_key"]) as client:
        r = client.get(f"/api/v1/jobs/{args.job_id}")
        r.raise_for_status()
        job = r.json()

    if args.json:
        _emit_json(job)
        return 0

    dur = (
        (job.get("finished_at") or time.time()) - job["started_at"]
        if job.get("started_at")
        else None
    )
    print(f"Job ID      : {job['job_id']}")
    print(f"Name        : {job['name']}")
    print(f"Status      : {job['status']}")
    print(f"Hardware    : {job['gpu']}" + (f" (TPU {job['tpu']})" if job.get("tpu") else "") + (" [high-mem]" if job.get("high_mem") else ""))
    if job.get("gpu_detected"):
        print(f"Detected GPU: {job['gpu_detected']}")
    print(f"Account     : {job.get('assigned_account') or '-'}")
    if job.get("attempted_accounts"):
        print(f"Failover    : {' -> '.join(job['attempted_accounts'])}")
    print(f"Entrypoint  : {job['entrypoint']}")
    print(f"Priority    : {job.get('priority', 0)}")
    print(f"Duration    : {_fmt_duration(dur)}")
    print(f"Exit Code   : {job.get('exit_code') if job.get('exit_code') is not None else '-'}")
    print(f"Created At  : {_fmt_time(job.get('created_at'))}")
    if job.get("error_message"):
        print(f"Error       : {job['error_message']}")
    details = job.get("output_file_details") or []
    if details:
        print(f"Outputs ({len(details)}):")
        for item in details:
            print(f"  - {item['path']} ({_fmt_bytes(item.get('size', 0))})")
    return 0


def _cmd_logs(args: argparse.Namespace, prof: dict[str, Any]) -> int:
    if args.follow:
        with _make_client(prof["server"], prof["api_key"], streaming=True) as client:
            try:
                _stream_job_until_done(
                    client,
                    args.job_id,
                    initial_tail=args.tail,
                    quiet=False,
                    stream_logs=True,
                )
            except KeyboardInterrupt:
                print(
                    "\n[cpool] Detached from live stream; remote job continues running.",
                    file=sys.stderr,
                )
                return 0
        return 0

    with _make_client(prof["server"], prof["api_key"]) as client:
        r = client.get(
            f"/api/v1/jobs/{args.job_id}/logs",
            params={"tail_lines": args.tail},
        )
        r.raise_for_status()
        data = r.json()

    if args.json:
        _emit_json(data)
    else:
        logs = data.get("logs", "")
        if logs:
            print(logs)
    return 0


def _cmd_wait(args: argparse.Namespace, prof: dict[str, Any]) -> int:
    with _make_client(prof["server"], prof["api_key"], streaming=True) as client:
        try:
            final_job = _stream_job_until_done(
                client,
                args.job_id,
                initial_tail=args.tail,
                quiet=bool(args.quiet or args.json),
                stream_logs=not args.quiet,
            )
        except KeyboardInterrupt:
            print(
                "\n[cpool] Detached from wait; remote job continues running.",
                file=sys.stderr,
            )
            return 130

        downloaded: list[str] = []
        if args.out_dir and final_job.get("artifacts_archive"):
            downloaded = _download_archive_or_file(
                client, args.job_id, Path(args.out_dir)
            )
            if not args.json:
                print(
                    f"[cpool] Extracted {len(downloaded)} artifact(s) to {Path(args.out_dir).resolve()}",
                    file=sys.stderr,
                )

    if args.json:
        if downloaded:
            final_job["downloaded_files"] = downloaded
        _emit_json(final_job)
    else:
        print(
            f"\n[cpool] Final status: {final_job['status']} "
            f"(account={final_job.get('assigned_account') or '-'}, "
            f"exit_code={final_job.get('exit_code')})",
            file=sys.stderr,
        )
    return 0 if final_job["status"] == "succeeded" else 1


def _cmd_files(args: argparse.Namespace, prof: dict[str, Any]) -> int:
    with _make_client(prof["server"], prof["api_key"]) as client:
        if args.workspace:
            r = client.get(f"/api/v1/jobs/{args.job_id}/workspace")
            r.raise_for_status()
            data = r.json()
            files = data.get("files", [])
        else:
            r = client.get(f"/api/v1/jobs/{args.job_id}")
            r.raise_for_status()
            job = r.json()
            files = job.get("output_file_details") or [
                {"path": p, "size": 0} for p in (job.get("output_files") or [])
            ]

    if args.json:
        _emit_json({"job_id": args.job_id, "files": files})
        return 0
    rows = [[f["path"], _fmt_bytes(f.get("size", 0))] for f in files]
    _print_table(["PATH", "SIZE"], rows)
    return 0


def _cmd_pull(args: argparse.Namespace, prof: dict[str, Any]) -> int:
    default_dest = "." if args.file else f"./outputs_{args.job_id}"
    dest = Path(args.dest_dir or default_dest)
    with _make_client(prof["server"], prof["api_key"]) as client:
        pulled = _download_archive_or_file(
            client, args.job_id, dest, single_file=args.file
        )
    if args.json:
        _emit_json(
            {"job_id": args.job_id, "dest_dir": str(dest.resolve()), "files": pulled}
        )
    else:
        print(f"Downloaded {len(pulled)} file(s) into {dest.resolve()}:")
        for f in pulled:
            print(f"  - {f}")
    return 0


def _cmd_cancel(args: argparse.Namespace, prof: dict[str, Any]) -> int:
    with _make_client(prof["server"], prof["api_key"]) as client:
        r = client.post(f"/api/v1/jobs/{args.job_id}/cancel")
        r.raise_for_status()
        job = r.json()
    if args.json:
        _emit_json(job)
    else:
        print(f"Cancellation requested for {job['job_id']} (status={job['status']}).")
    return 0


def _cmd_retry(args: argparse.Namespace, prof: dict[str, Any]) -> int:
    body: dict[str, Any] = {}
    if args.gpu:
        body["gpu"] = args.gpu.upper()
    if args.priority is not None:
        body["priority"] = args.priority

    should_wait = bool(args.wait or args.out_dir)
    with _make_client(prof["server"], prof["api_key"], streaming=should_wait) as client:
        r = client.post(f"/api/v1/jobs/{args.job_id}/retry", json=body)
        r.raise_for_status()
        job = r.json()
        new_id = job["job_id"]
        if not should_wait:
            if args.json:
                _emit_json(job)
            else:
                print(f"Cloned {args.job_id} -> {new_id} (gpu={job['gpu']}, status={job['status']})")
            return 0

        try:
            final_job = _stream_job_until_done(
                client, new_id, initial_tail=None, quiet=bool(args.json), stream_logs=True
            )
        except KeyboardInterrupt:
            print(
                "\n[cpool] Detached from live stream; remote job continues running.",
                file=sys.stderr,
            )
            return 130

        if args.out_dir and final_job.get("artifacts_archive"):
            _download_archive_or_file(client, new_id, Path(args.out_dir))
        if args.json:
            _emit_json(final_job)
        return 0 if final_job["status"] == "succeeded" else 1


def _cmd_rm_job(args: argparse.Namespace, prof: dict[str, Any]) -> int:
    with _make_client(prof["server"], prof["api_key"]) as client:
        r = client.delete(f"/api/v1/jobs/{args.job_id}")
        r.raise_for_status()
        res = r.json()
    if args.json:
        _emit_json(res)
    else:
        print(f"Deleted job '{args.job_id}'.")
    return 0


# -------------------- Argument Parser --------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--server",
        default=argparse.SUPPRESS,
        help="Colab Pool server URL (overrides env & saved profile)",
    )
    common.add_argument(
        "--api-key",
        default=argparse.SUPPRESS,
        help="Bearer API key (overrides env & saved profile)",
    )
    common.add_argument(
        "--profile",
        default=argparse.SUPPRESS,
        help="Saved CLI profile name",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Output machine-readable JSON",
    )

    parser = argparse.ArgumentParser(
        prog="cpool",
        description="Colab Pro Pool unified CLI (compute pool, job runner, SSE logs & ops)",
    )
    parser.add_argument("--server", default=None, help="Colab Pool server URL")
    parser.add_argument("--api-key", default=None, help="Bearer API key")
    parser.add_argument("--profile", default=None, help="Saved CLI profile name")
    parser.add_argument(
        "--json", action="store_true", default=False, help="Output machine-readable JSON"
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    # login
    p_login = sub.add_parser(
        "login", parents=[common], help="Verify and save server URL & API key to local config"
    )
    p_login.add_argument("server_arg", nargs="?", default="", help="Server base URL (e.g. http://127.0.0.1:8080)")
    p_login.add_argument("--gpu", default="", help="Default GPU for this profile (default: T4)")
    p_login.add_argument("--timeout", type=int, default=None, help="Default timeout seconds")
    p_login.add_argument("--no-verify", action="store_true", help="Save without calling /api/v1/auth/check")

    # config
    p_cfg = sub.add_parser("config", parents=[common], help="Inspect or manage local CLI profiles")
    sub_cfg = p_cfg.add_subparsers(dest="cfg_cmd", required=False)
    sub_cfg.add_parser("show", parents=[common], help="Show active profile and saved profiles")
    sub_cfg.add_parser("path", parents=[common], help="Print config.json path")
    p_cfg_use = sub_cfg.add_parser("use", parents=[common], help="Switch active profile")
    p_cfg_use.add_argument("name")
    p_cfg_gpu = sub_cfg.add_parser("set-gpu", parents=[common], help="Set default GPU for active profile")
    p_cfg_gpu.add_argument("gpu")
    p_cfg_out = sub_cfg.add_parser("logout", parents=[common], help="Remove saved profile")
    p_cfg_out.add_argument("name", nargs="?", default="")

    # tunnel
    p_tun = sub.add_parser("tunnel", parents=[common], help="Open an SSH port-forwarding tunnel to the pool server")
    p_tun.add_argument("ssh_target", help="SSH destination, e.g. user@server-ip or ssh-config host")
    p_tun.add_argument("--local-port", type=int, default=8080)
    p_tun.add_argument("--remote-port", type=int, default=8080)
    p_tun.add_argument("--remote-host", default="127.0.0.1")
    p_tun.add_argument("--ssh-opt", action="append", default=[], help="Extra -o option passed to ssh")
    p_tun.add_argument("--print-only", action="store_true", help="Print SSH command without executing")

    # open
    sub.add_parser("open", parents=[common], help="Open the Web Console in your default browser")

    # status / pool
    sub.add_parser(
        "status",
        aliases=["pool"],
        parents=[common],
        help="Show pool KPIs, Pro/Pro+ account slots, and cooldowns",
    )

    # accounts / acct
    p_acct = sub.add_parser(
        "accounts",
        aliases=["acct"],
        parents=[common],
        help="Manage Colab Pro/Pro+ account slots",
    )
    sub_acct = p_acct.add_subparsers(dest="acct_cmd", required=False)
    sub_acct.add_parser("list", parents=[common], help="List accounts")
    p_acct_add = sub_acct.add_parser("add", parents=[common], help="Register new account slot")
    p_acct_add.add_argument("name")
    p_acct_add.add_argument("--tier", default="pro", choices=["free", "pro", "pro+"])
    p_acct_en = sub_acct.add_parser("enable", parents=[common], help="Enable account slot")
    p_acct_en.add_argument("name")
    p_acct_dis = sub_acct.add_parser("disable", parents=[common], help="Disable account slot")
    p_acct_dis.add_argument("name")
    p_acct_rst = sub_acct.add_parser("reset", parents=[common], help="Clear account cooldown and error")
    p_acct_rst.add_argument("name")
    p_acct_tier = sub_acct.add_parser("tier", parents=[common], help="Change account subscription tier")
    p_acct_tier.add_argument("name")
    p_acct_tier.add_argument("tier_value", choices=["free", "pro", "pro+"])
    p_acct_rm = sub_acct.add_parser("rm", parents=[common], help="Delete an account slot")
    p_acct_rm.add_argument("name")
    p_acct_rm.add_argument("--remove-disk", action="store_true", help="Also delete account directory on disk")
    sub_acct.add_parser("sync", parents=[common], help="Sync account directories from disk")
    sub_acct.add_parser("cleanup", parents=[common], help="Stop orphan runtimes on non-busy accounts")

    # run / submit
    p_run = sub.add_parser(
        "run",
        aliases=["submit"],
        parents=[common],
        help="Submit a local script, notebook, or project directory",
    )
    p_run.add_argument("target", help="Local .py/.ipynb file or project directory")
    p_run.add_argument("-e", "--entrypoint", default="", help="Entrypoint inside directory")
    p_run.add_argument("-n", "--name", default="", help="Experiment name")
    p_run.add_argument("--gpu", default="", help="GPU type: T4, L4, A100, H100, G4, or CPU")
    p_run.add_argument("--tpu", default="", help="Optional TPU type (e.g. v5e1, v6e1)")
    p_run.add_argument("--high-mem", action="store_true", help="Request high-RAM VM")
    p_run.add_argument("--priority", type=int, default=0, help="Scheduling priority (-10 to 100)")
    p_run.add_argument("--timeout", type=int, default=None, help="Max runtime seconds")
    p_run.add_argument("--pip", action="append", default=[], help="Extra pip package (repeatable)")
    p_run.add_argument("--dataset-cmd", action="append", default=[], help="Dataset shell command in Colab (repeatable)")
    p_run.add_argument("--arg", action="append", default=[], help="CLI argument passed to entrypoint (repeatable)")
    p_run.add_argument("--env", action="append", default=[], help="Environment variable KEY=VAL (repeatable)")
    p_run.add_argument("--drive", default="", help="Optional server rclone Drive profile")
    p_run.add_argument("--drive-subpath", default="", help="Optional Drive subpath")
    p_run.add_argument("--sync-outputs-to", default="", help="Optional Drive folder to copy outputs to")
    p_run.add_argument("-w", "--wait", action="store_true", help="Stream SSE logs until completion")
    p_run.add_argument("-o", "--out-dir", default="", help="Download & extract outputs into directory on completion")

    # exec
    p_exec = sub.add_parser(
        "exec",
        parents=[common],
        help="Run inline Python snippet or stdin script on the pool (streams by default)",
    )
    p_exec.add_argument("-c", "--code", default=None, help="Inline Python code")
    p_exec.add_argument("-f", "--file", default="", help="Local Python file to submit as inline script")
    p_exec.add_argument("-n", "--name", default="quick-exec", help="Experiment name")
    p_exec.add_argument("--gpu", default="", help="GPU type")
    p_exec.add_argument("--tpu", default="", help="TPU type")
    p_exec.add_argument("--high-mem", action="store_true")
    p_exec.add_argument("--priority", type=int, default=0)
    p_exec.add_argument("--timeout", type=int, default=None)
    p_exec.add_argument("--pip", action="append", default=[])
    p_exec.add_argument("--dataset-cmd", action="append", default=[])
    p_exec.add_argument("--env", action="append", default=[])
    p_exec.add_argument("-d", "--detach", action="store_true", help="Submit and return immediately without streaming")
    p_exec.add_argument("-o", "--out-dir", default="", help="Download & extract outputs on completion")

    # jobs / ls
    p_jobs = sub.add_parser("jobs", aliases=["ls"], parents=[common], help="List and filter jobs")
    p_jobs.add_argument("--status", default="", help="Filter by status")
    p_jobs.add_argument("--gpu", default="", help="Filter by GPU")
    p_jobs.add_argument("--account", default="", help="Filter by assigned account")
    p_jobs.add_argument("--search", default="", help="Search name/id/account/entrypoint")
    p_jobs.add_argument("--limit", type=int, default=20)
    p_jobs.add_argument("--offset", type=int, default=0)

    # show / get
    p_show = sub.add_parser("show", aliases=["get"], parents=[common], help="Show detailed metadata for a job")
    p_show.add_argument("job_id")

    # logs
    p_logs = sub.add_parser("logs", parents=[common], help="Fetch or follow (-f) real-time job logs")
    p_logs.add_argument("job_id")
    p_logs.add_argument("--tail", type=int, default=200)
    p_logs.add_argument("-f", "--follow", action="store_true", help="Follow live logs via SSE stream")

    # wait
    p_wait = sub.add_parser("wait", parents=[common], help="Attach to a job and wait until completion")
    p_wait.add_argument("job_id")
    p_wait.add_argument("-o", "--out-dir", default="", help="Download & extract outputs on completion")
    p_wait.add_argument("--quiet", action="store_true", help="Suppress live log output")
    p_wait.add_argument("--tail", type=int, default=200)

    # files
    p_files = sub.add_parser("files", parents=[common], help="List output or workspace files for a job")
    p_files.add_argument("job_id")
    p_files.add_argument("--workspace", action="store_true", help="List submitted workspace files instead of outputs")

    # pull
    p_pull = sub.add_parser("pull", parents=[common], help="Download output archive or a single output file")
    p_pull.add_argument("job_id")
    p_pull.add_argument("dest_dir", nargs="?", default="", help="Destination directory")
    p_pull.add_argument("--file", default="", help="Download only this relative output file path")

    # cancel
    p_cancel = sub.add_parser("cancel", parents=[common], help="Cancel a queued or running job")
    p_cancel.add_argument("job_id")

    # retry
    p_retry = sub.add_parser("retry", parents=[common], help="Clone and re-queue an existing job")
    p_retry.add_argument("job_id")
    p_retry.add_argument("--gpu", default="", help="Optional GPU override")
    p_retry.add_argument("--priority", type=int, default=None, help="Optional priority override")
    p_retry.add_argument("-w", "--wait", action="store_true", help="Stream SSE logs until completion")
    p_retry.add_argument("-o", "--out-dir", default="", help="Download outputs on completion")

    # rm / delete
    p_rm = sub.add_parser("rm", aliases=["delete"], parents=[common], help="Delete a finished job and its disk directory")
    p_rm.add_argument("job_id")

    # server / mcp wrappers
    sub.add_parser("server", parents=[common], help="Start the Colab Pro Pool HTTP/Web server")
    sub.add_parser("mcp", parents=[common], help="Start the Colab Pro Pool MCP stdio server")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json", False))

    try:
        cmd = args.cmd
        if cmd == "server":
            from .api import main as server_main

            server_main()
            return 0
        if cmd == "mcp":
            from .mcp_server import main as mcp_main

            mcp_main()
            return 0
        if cmd == "config":
            return _cmd_config(args)
        if cmd == "tunnel":
            return _cmd_tunnel(args)

        profile_name, prof = resolve_profile(
            getattr(args, "profile", None),
            server_override=getattr(args, "server", None),
            api_key_override=getattr(args, "api_key", None),
        )

        if cmd == "login":
            return _cmd_login(args, profile_name, prof)
        if cmd == "open":
            return _cmd_open(args, prof)
        if cmd in {"status", "pool"}:
            return _cmd_status(args, prof)
        if cmd in {"accounts", "acct"}:
            return _cmd_accounts(args, prof)
        if cmd in {"run", "submit"}:
            return _cmd_run(args, prof)
        if cmd == "exec":
            return _cmd_exec(args, prof)
        if cmd in {"jobs", "ls"}:
            return _cmd_jobs(args, prof)
        if cmd in {"show", "get"}:
            return _cmd_show(args, prof)
        if cmd == "logs":
            return _cmd_logs(args, prof)
        if cmd == "wait":
            return _cmd_wait(args, prof)
        if cmd == "files":
            return _cmd_files(args, prof)
        if cmd == "pull":
            return _cmd_pull(args, prof)
        if cmd == "cancel":
            return _cmd_cancel(args, prof)
        if cmd == "retry":
            return _cmd_retry(args, prof)
        if cmd in {"rm", "delete"}:
            return _cmd_rm_job(args, prof)

        raise ValueError(f"Unsupported command: {cmd}")
    except httpx.HTTPStatusError as exc:
        msg = _format_http_error(exc)
        if as_json:
            _emit_json({"error": msg, "status_code": exc.response.status_code})
        else:
            print(f"[cpool error] {msg}", file=sys.stderr)
        return 1
    except httpx.RequestError as exc:
        msg = (
            f"Connection failed ({exc}). Is the pool server running or SSH tunnel active?"
        )
        if as_json:
            _emit_json({"error": msg})
        else:
            print(f"[cpool error] {msg}", file=sys.stderr)
        return 1
    except ValueError as exc:
        if as_json:
            _emit_json({"error": str(exc)})
        else:
            print(f"[cpool error] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

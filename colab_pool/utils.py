"""Shared path-safety, archive extraction, and streaming log helpers."""

from __future__ import annotations

import base64
import fnmatch
import io
import os
import re
import shutil
import tarfile
from pathlib import Path, PurePosixPath, PureWindowsPath

SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")


def safe_join(root: Path, rel_path: str) -> Path:
    """Resolve `rel_path` strictly inside `root`, rejecting traversal or absolute paths."""
    if not rel_path or not isinstance(rel_path, str):
        raise ValueError("Relative path must be a non-empty string.")
    if "\x00" in rel_path:
        raise ValueError("Unsafe null byte in path.")

    normalized = rel_path.replace("\\", "/").strip()
    if not normalized or normalized in {".", "./"}:
        raise ValueError(f"Invalid relative file path: {rel_path}")

    posix_p = PurePosixPath(normalized)
    win_p = PureWindowsPath(rel_path)
    native_p = Path(normalized)

    if (
        posix_p.is_absolute()
        or win_p.is_absolute()
        or bool(win_p.drive)
        or native_p.is_absolute()
        or normalized.startswith("/")
        or ".." in posix_p.parts
        or ".." in win_p.parts
    ):
        raise ValueError(f"Unsafe relative file path: {rel_path}")

    root_resolved = root.resolve()
    dest = (root_resolved / posix_p.as_posix()).resolve()
    if dest == root_resolved or not dest.is_relative_to(root_resolved):
        raise ValueError(f"Path escapes target directory: {rel_path}")
    return dest


def safe_extract_tar(tf: tarfile.TarFile, dest: Path) -> None:
    """Safely extract regular files and directories from a tar archive into `dest`."""
    dest_resolved = dest.resolve()
    dest_resolved.mkdir(parents=True, exist_ok=True)

    for member in tf.getmembers():
        raw_name = member.name
        if not raw_name or "\x00" in raw_name:
            raise ValueError(f"Unsafe tar member name: {raw_name!r}")

        norm = raw_name.replace("\\", "/")
        if norm in {".", "./"} and member.isdir():
            continue

        # Reject symlinks, hardlinks, FIFOs, character/block devices, etc.
        if member.issym() or member.islnk() or not (member.isreg() or member.isdir()):
            raise ValueError(f"Unsupported or unsafe tar entry type for {raw_name!r}")

        target = safe_join(dest_resolved, norm)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        src_obj = tf.extractfile(member)
        if src_obj is None:
            raise ValueError(f"Unable to read regular file entry: {raw_name!r}")
        with src_obj, target.open("wb") as out_f:
            shutil.copyfileobj(src_obj, out_f, length=65536)

        if os.name != "nt" and member.mode:
            try:
                os.chmod(target, (member.mode & 0o755) | 0o600)
            except OSError:
                pass


def read_log_tail(
    log_path: Path, tail_lines: int = 200, chunk_size: int = 65536
) -> tuple[str, int, int]:
    """Read up to the last `tail_lines` lines from EOF without loading the full file.

    Returns `(text, file_size_bytes, returned_line_count)`.
    """
    if tail_lines <= 0 or not log_path.exists():
        size = log_path.stat().st_size if log_path.exists() else 0
        return ("", size, 0)

    file_size = log_path.stat().st_size
    if file_size == 0:
        return ("", 0, 0)

    chunks: list[bytes] = []
    newline_count = 0
    pos = file_size

    with log_path.open("rb") as f:
        while pos > 0:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            buf = f.read(read_size)
            chunks.append(buf)
            newline_count += buf.count(b"\n")
            # Collect one extra newline so we can drop any incomplete leading line when pos > 0
            if newline_count >= tail_lines + 1:
                break

    data = b"".join(reversed(chunks))
    if pos > 0:
        # Discard partial first line unless we reached BOF
        first_nl = data.find(b"\n")
        if first_nl != -1:
            data = data[first_nl + 1 :]

    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > tail_lines:
        lines = lines[-tail_lines:]
    return ("\n".join(lines), file_size, len(lines))


def _trim_incomplete_utf8_tail(data: bytes) -> bytes:
    """Trim up to 3 trailing bytes if they form an incomplete UTF-8 codepoint."""
    if not data:
        return data
    # Walk backwards up to 3 bytes from the end to check for a truncated multi-byte start byte.
    limit = min(3, len(data))
    for back in range(1, limit + 1):
        b = data[-back]
        if b < 0x80:
            return data  # ASCII ends cleanly
        if b >= 0xC0:
            # Start byte of a multi-byte sequence: determine expected length
            if (b & 0xE0) == 0xC0:
                expected = 2
            elif (b & 0xF0) == 0xE0:
                expected = 3
            elif (b & 0xF8) == 0xF0:
                expected = 4
            else:
                return data
            if back < expected:
                return data[:-back]
            return data
    return data


def read_log_from_offset(
    log_path: Path, offset: int = 0, max_bytes: int = 262144
) -> tuple[str, int, int]:
    """Read up to `max_bytes` starting at `offset` on clean UTF-8 boundaries.

    Returns `(text, next_offset, total_file_size)`.
    """
    if not log_path.exists():
        return ("", 0, 0)

    total_size = log_path.stat().st_size
    safe_offset = max(0, min(offset, total_size))
    if safe_offset >= total_size or max_bytes <= 0:
        return ("", safe_offset, total_size)

    with log_path.open("rb") as f:
        f.seek(safe_offset)
        raw = f.read(max_bytes)

    if not raw:
        return ("", safe_offset, total_size)

    if safe_offset + len(raw) < total_size:
        trimmed = _trim_incomplete_utf8_tail(raw)
        if trimmed:
            raw = trimmed

    text = raw.decode("utf-8", errors="replace")
    next_offset = safe_offset + len(raw)
    return (text, next_offset, total_size)


def dir_size_bytes(path: Path) -> int:
    """Recursively compute total byte size of regular files under `path`."""
    if not path.exists():
        return 0
    total = 0
    stack: list[Path] = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


DEFAULT_IGNORE_NAMES = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".DS_Store",
}
DEFAULT_IGNORE_PATTERNS = ("*.pyc", "*.pyo")


def _load_cpoolignore(root: Path) -> list[str]:
    ignore_file = root / ".cpoolignore"
    if not ignore_file.is_file():
        return []
    patterns: list[str] = []
    for raw_line in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        cleaned = line.rstrip("/")
        if cleaned:
            patterns.append(cleaned)
    return patterns


def _is_ignored_rel(
    rel_posix: str, rel_parts: tuple[str, ...], extra_patterns: list[str]
) -> bool:
    if any(part in DEFAULT_IGNORE_NAMES for part in rel_parts):
        return True
    for pat in DEFAULT_IGNORE_PATTERNS:
        if fnmatch.fnmatch(rel_posix, pat) or any(fnmatch.fnmatch(p, pat) for p in rel_parts):
            return True
    for pat in extra_patterns:
        if fnmatch.fnmatch(rel_posix, pat) or any(fnmatch.fnmatch(p, pat) for p in rel_parts):
            return True
    return False


def pack_local_dir_b64(local_dir: str | Path) -> str:
    """Pack a local directory into a base64-encoded `.tar.gz`, honoring `.cpoolignore`."""
    root = Path(local_dir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"local_workspace_dir does not exist: {root}")
    extra_patterns = _load_cpoolignore(root)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for item in sorted(root.rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(root)
            rel_posix = rel.as_posix()
            if _is_ignored_rel(rel_posix, rel.parts, extra_patterns):
                continue
            tf.add(item, arcname=rel_posix)
    return base64.b64encode(buf.getvalue()).decode("ascii")

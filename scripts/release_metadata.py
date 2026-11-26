"""Validate release versions and emit metadata for GitHub Actions (Python 3.12+)."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def main() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    if not VERSION_RE.fullmatch(version):
        raise SystemExit("Release versions must use MAJOR.MINOR.PATCH, for example 0.1.0.")

    module = ast.parse((ROOT / "colab_pool" / "__init__.py").read_text(encoding="utf-8"))
    package_version = next(
        ast.literal_eval(statement.value)
        for statement in module.body
        if isinstance(statement, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__version__"
                for target in statement.targets)
    )
    frontend = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    versions = [package_version, frontend["version"], lock["version"],
                lock["packages"][""]["version"]]
    if any(value != version for value in versions):
        raise SystemExit("Versions in pyproject.toml, __init__.py, package.json and package-lock.json must match.")

    tag = f"v{version}"
    ref = os.environ["GITHUB_REF"]
    if ref.startswith("refs/tags/") and ref != f"refs/tags/{tag}":
        raise SystemExit(f"Tag {ref.removeprefix('refs/tags/')} does not match project version {tag}.")
    if not ref.startswith("refs/tags/") and ref != f"refs/heads/{os.environ['DEFAULT_BRANCH']}":
        raise SystemExit("Manual releases must run from the default branch.")

    existing = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"],
        cwd=ROOT, capture_output=True, text=True,
    )
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if existing.returncode == 0 and existing.stdout.strip() != commit:
        raise SystemExit(f"{tag} already points to another commit. Bump the project version before releasing.")

    repository = os.environ["GITHUB_REPOSITORY"]
    request = Request(
        f"https://api.github.com/repos/{repository}/releases/latest",
        headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                 "Accept": "application/vnd.github+json"},
    )
    latest = True
    try:
        with urlopen(request, timeout=30) as response:
            current = json.load(response)["tag_name"].removeprefix("v")
        if not VERSION_RE.fullmatch(current):
            raise SystemExit(f"Cannot compare latest release {current!r} with a stable version.")
        latest = tuple(map(int, version.split("."))) >= tuple(map(int, current.split(".")))
    except HTTPError as error:
        if error.code != 404:
            raise

    values = {"version": version, "tag": tag, "image": f"ghcr.io/{repository.lower()}",
              "latest": str(latest).lower()}
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, value in values.items():
            print(f"{key}={value}", file=output)
    print(f"Validated {tag} at {commit}; update latest: {latest}")


if __name__ == "__main__":
    main()

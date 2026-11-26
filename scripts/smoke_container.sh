#!/usr/bin/env bash
# Verify the actual runtime image without allocating a Colab machine.
set -euo pipefail

IMAGE="${1:?Usage: smoke_container.sh IMAGE}"
CONTAINER="colab-pool-smoke-${RANDOM}-$$"
SMOKE_KEY="container-smoke-test-key"

cleanup() {
    docker logs "${CONTAINER}" || true
    docker rm -f "${CONTAINER}" >/dev/null || true
}
trap cleanup EXIT

docker run --rm --entrypoint colab "${IMAGE}" --help >/dev/null
docker run --rm -i --entrypoint python "${IMAGE}" - < "$(dirname "$0")/smoke_mcp.py"
docker run --rm --entrypoint rclone "${IMAGE}" version

docker run -d --name "${CONTAINER}" \
    -e COLAB_POOL_API_KEY="${SMOKE_KEY}" \
    -e COLAB_POOL_CLEANUP_ORPHANS=0 \
    "${IMAGE}"

for attempt in {1..30}; do
    if docker exec "${CONTAINER}" python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).close()" \
        >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

docker exec -i "${CONTAINER}" python - <<'PY'
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

base = "http://127.0.0.1:8080"
with urlopen(f"{base}/healthz", timeout=5) as response:
    assert json.load(response)["status"] == "ok"
with urlopen(f"{base}/", timeout=5) as response:
    html = response.read().decode()
    assert 'id="root"' in html and "/assets/" in html, "Built frontend is missing"
try:
    urlopen(f"{base}/api/v1/accounts", timeout=5)
except HTTPError as error:
    assert error.code == 401, error.code
else:
    raise AssertionError("Protected API accepted an unauthenticated request")
request = Request(f"{base}/api/v1/accounts", headers={
    "Authorization": f"Bearer {os.environ['COLAB_POOL_API_KEY']}"
})
with urlopen(request, timeout=5) as response:
    assert json.load(response)["accounts"] == []
print("Container health, frontend and API authentication passed.")
PY

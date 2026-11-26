FROM --platform=$BUILDPLATFORM node:22-bookworm-slim AS frontend-builder

WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm

ARG GOOGLE_COLAB_CLI_VERSION=0.8.0

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    openssh-client \
    rclone \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install official Google Colab CLI (Linux supported, Python 3.12+)
RUN pip install --no-cache-dir "google-colab-cli==${GOOGLE_COLAB_CLI_VERSION}"

COPY pyproject.toml README.md LICENSE /app/
COPY colab_pool /app/colab_pool
COPY --from=frontend-builder /build/colab_pool/static /app/colab_pool/static
COPY scripts /app/scripts

RUN pip install --no-cache-dir . && chmod +x /app/scripts/*.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COLAB_POOL_DATA_DIR=/var/lib/colab-pool \
    COLAB_POOL_HOST=0.0.0.0 \
    COLAB_POOL_PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).close()"

CMD ["colab-pool-server"]

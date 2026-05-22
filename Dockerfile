# BIJOTEL Docker image — self-hosted deploy of the v1.0.0 audit chain.
#
# Multi-stage build:
#   builder  — installs all extras into a venv (sentence-transformers
#              wheels are large; tree-sitter compiles native code).
#   runtime  — slim final image with venv copied across, no toolchain.
#
# Usage:
#   docker build -t bijotel:1.0.0 .
#   docker run --rm -p 8080:8080 \
#       -v $(pwd)/data:/data \
#       -e BIJOTEL_DB_PATH=/data/chain.db \
#       -e BIJOTEL_HMAC_SECRET=$(openssl rand -hex 32) \
#       bijotel:1.0.0
#
# /data is the canonical mount for the chain.db (per docker-compose.yml).
# /app is read-only (binaries + venv); the process never writes here.

# ───────────────────────── builder ─────────────────────────

FROM python:3.12-slim AS builder

# Build deps for tree-sitter (C extension compiled at install time)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Create venv that we'll copy into the runtime layer
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy the wheel (built by `python -m build` before `docker build`)
COPY dist/bijotel-1.0.0-py3-none-any.whl /tmp/

# Install with [api] for serve + [fingerprint] + [ast] for full layer coverage.
# Anthropic/OpenAI adapters are NOT included by default — image stays generic;
# users add `[anthropic,openai]` via a derived Dockerfile if they want the
# adapters bundled. Keeps the base image lean (~600MB → ~400MB).
RUN pip install --no-cache-dir \
    "/tmp/bijotel-1.0.0-py3-none-any.whl[api,fingerprint,ast]"

# ───────────────────────── runtime ─────────────────────────

FROM python:3.12-slim AS runtime

# Minimal runtime deps — no compilers, no headers
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash --uid 1000 bijotel

# Bring across the prepared venv
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BIJOTEL_DB_PATH=/data/chain.db

# Data directory for chain.db + CAS
RUN mkdir -p /data && chown -R bijotel:bijotel /data

USER bijotel
WORKDIR /home/bijotel

# Healthcheck: hit /health on the bound port.
# 0 = healthy, 1 = unhealthy. Docker swarm / k8s can act on this.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8080/health || exit 1

EXPOSE 8080

# Default command: serve on 0.0.0.0:8080 so docker port mapping works.
# Override via `docker run bijotel:1.0.0 <other-cmd>` (verify, regression, etc.).
ENTRYPOINT ["bijotel"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]

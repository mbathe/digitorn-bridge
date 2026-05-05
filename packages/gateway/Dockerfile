# syntax=docker/dockerfile:1.6
# ──────────────────────────────────────────────────────────────────
# Digitorn LLM Gateway - production image
# ──────────────────────────────────────────────────────────────────
# Multi-stage:
#   1. builder  - install deps into a venv
#   2. runtime  - slim image with only what's needed at runtime
# ──────────────────────────────────────────────────────────────────

ARG PYTHON_VERSION=3.12-slim-bookworm

# ── Stage 1: build ────────────────────────────────────────────────
FROM python:${PYTHON_VERSION} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src

RUN python -m venv /venv \
    && /venv/bin/pip install --upgrade pip \
    && /venv/bin/pip install .

# ── Stage 2: runtime ──────────────────────────────────────────────
FROM python:${PYTHON_VERSION} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/venv/bin:$PATH" \
    DIGITORN_GATEWAY_HOST=0.0.0.0 \
    DIGITORN_GATEWAY_PORT=8002 \
    DIGITORN_GATEWAY_MODELS_CONFIG_PATH=/app/config/models.yaml

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 gateway

COPY --from=builder /venv /venv
COPY --chown=gateway:gateway src /app/src
COPY --chown=gateway:gateway config /app/config

WORKDIR /app
USER gateway

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${DIGITORN_GATEWAY_PORT}/healthz" || exit 1

CMD ["uvicorn", "digitorn_gateway.main:app", \
     "--host", "0.0.0.0", "--port", "8002", \
     "--proxy-headers", "--forwarded-allow-ips=*"]

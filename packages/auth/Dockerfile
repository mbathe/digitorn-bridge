# syntax=docker/dockerfile:1.6
# ──────────────────────────────────────────────────────────────────
# digitorn-auth — production image
# ──────────────────────────────────────────────────────────────────
# Multi-stage:
#   1. builder  — install deps into a venv
#   2. runtime  — slim image, non-root user, healthcheck
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
    DIGITORN_AUTH_PORT=8001

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 auth

COPY --from=builder /venv /venv
COPY --chown=auth:auth alembic.ini /app/alembic.ini
COPY --chown=auth:auth alembic /app/alembic
COPY --chown=auth:auth src /app/src

WORKDIR /app
USER auth

# Where the auto-generated keypair + sqlite fallback land. Persist
# this volume in production so RSA keys survive container restarts.
VOLUME ["/home/auth/.digitorn"]

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${DIGITORN_AUTH_PORT}/health" || exit 1

CMD ["uvicorn", "digitorn_auth.server:app", \
     "--host", "0.0.0.0", "--port", "8001", \
     "--proxy-headers", "--forwarded-allow-ips=*"]

# syntax=docker/dockerfile:1.6
# ──────────────────────────────────────────────────────────────────
# Digitorn Hub — production image
# ──────────────────────────────────────────────────────────────────
# Multi-stage:
#   1. builder  — install deps, pre-download fastembed model
#   2. runtime  — slim image with only what's needed at runtime
#
# The fastembed model (~50 MB) is baked into the image so cold starts
# don't pay the download cost.
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

ENV FASTEMBED_CACHE_PATH=/opt/fastembed-cache
RUN /venv/bin/python - <<'PY'
import os
os.environ.setdefault("FASTEMBED_CACHE_PATH", "/opt/fastembed-cache")
from fastembed import TextEmbedding
TextEmbedding(model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
print("fastembed model warmed at", os.environ["FASTEMBED_CACHE_PATH"])
PY

# ── Stage 2: runtime ──────────────────────────────────────────────
FROM python:${PYTHON_VERSION} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/venv/bin:$PATH" \
    FASTEMBED_CACHE_PATH=/opt/fastembed-cache \
    HUB_HOST=0.0.0.0 \
    HUB_PORT=8001

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 10001 hub

COPY --from=builder /venv /venv
COPY --from=builder /opt/fastembed-cache /opt/fastembed-cache
COPY --chown=hub:hub alembic.ini /app/alembic.ini
COPY --chown=hub:hub alembic /app/alembic
COPY --chown=hub:hub src /app/src

WORKDIR /app
USER hub

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${HUB_PORT}/api/v1/health" || exit 1

CMD ["uvicorn", "digitorn_hub.main:app", "--host", "0.0.0.0", "--port", "8001", "--proxy-headers", "--forwarded-allow-ips=*"]

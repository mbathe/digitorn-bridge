# Test image: mirrors what scripts/bootstrap.sh installs on a real
# Ubuntu 24.04 box, MINUS systemd (Docker doesn't run init).
#
# Goal: validate that every package needed for the daemon to import
# and start is available + that the pip install succeeds. If this
# image builds and the daemon answers /health on :8000, the real
# bootstrap on Hetzner will too.
#
# Usage:
#   cd <repo-root>
#   docker compose -f tests/docker/docker-compose.test.yml up --build
#
# Then from another terminal:
#   curl http://127.0.0.1:8000/health

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# ── 1. Same APT packages as bootstrap.sh ─────────────────────────────
# build-essential + pkg-config because some Python wheels are built
# from source on first install (cryptography, asyncpg fall-back).
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg lsb-release \
      git build-essential pkg-config \
      python3.12 python3.12-venv python3.12-dev \
    && rm -rf /var/lib/apt/lists/*

# ── 2. Service user (mirror prod) ───────────────────────────────────
RUN useradd --system --create-home --home-dir /home/digitorn \
      --shell /bin/bash digitorn

# ── 3. Copy repo + install ──────────────────────────────────────────
# Copying after package install keeps that layer cached when only the
# repo changes — saves ~30s on each rebuild.
WORKDIR /opt/digitorn-bridge
COPY --chown=digitorn:digitorn . /opt/digitorn-bridge/

# venv + editable install. The -e is what catches the most issues —
# pyproject.toml resolution + every transitive dep gets pulled here.
RUN python3.12 -m venv /opt/digitorn-bridge/.venv \
 && /opt/digitorn-bridge/.venv/bin/pip install --upgrade pip wheel \
 && /opt/digitorn-bridge/.venv/bin/pip install -e \
       "/opt/digitorn-bridge[postgres,redis,rss,pdf,presentation]" \
 && chown -R digitorn:digitorn /opt/digitorn-bridge

# ── 4. Daemon config — minimal, points at the redis sibling ────────
RUN mkdir -p /home/digitorn/.digitorn \
 && chown -R digitorn:digitorn /home/digitorn/.digitorn

COPY --chown=digitorn:digitorn tests/docker/test-config.yaml \
     /home/digitorn/.digitorn/config.yaml

# ── 5. Drop privileges ──────────────────────────────────────────────
USER digitorn
WORKDIR /home/digitorn

# Bind to 0.0.0.0 inside the container so the published port works.
EXPOSE 8000

# Foreground entrypoint (no systemd).
CMD ["/opt/digitorn-bridge/.venv/bin/digitorn", "start", \
     "--host", "0.0.0.0", "--port", "8000"]

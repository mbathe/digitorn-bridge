"""Spawn an isolated daemon configured with local Ollama as the LLM.

Reuses ``refactor_baseline.harness`` for the heavy lifting (process
spawn, JWT minting, JWKS plumbing) but overrides the default-model
env vars + writes a different test app YAML so the chat path actually
hits a working LLM instead of the gateway mock.
"""

from __future__ import annotations

import os
from pathlib import Path

from tools.live_tests.refactor_baseline.harness import (
    DaemonHandle,
    spawn_test_daemon as _base_spawn,
)


GATEWAY_URL = "http://127.0.0.1:8002"
GATEWAY_MODEL = "claude-haiku-4-5"  # alias defined in the gateway catalogue


def spawn_real_llm_daemon(*, gateway_jwt: str = ""):
    """Spawn an isolated daemon routed through the production
    ``digitorn-gateway`` at 8002. The gateway already has model
    aliases configured (Claude / OpenAI / local Ollama) so chat calls
    actually hit a real LLM via the same single-egress path the
    production daemon uses.

    Caller must pass a ``gateway_jwt`` that the gateway accepts
    (kid=auth-2026-04 -- the cloud-issued JWT in
    ``~/.digitorn/test-auth.json``). The standard ``mint_test_jwt``
    output is signed with kid=auth-local-dev and will be REJECTED by
    the live gateway.
    """
    extra = {
        "DIGITORN_DEFAULT_MODEL__PROVIDER": "openai",
        "DIGITORN_DEFAULT_MODEL__MODEL": GATEWAY_MODEL,
        "DIGITORN_DEFAULT_MODEL__BACKEND": "openai_compat",
        "DIGITORN_DEFAULT_MODEL__BASE_URL": f"{GATEWAY_URL}/v1",
        "DIGITORN_DEFAULT_MODEL__API_KEY": gateway_jwt,
        "DIGITORN_DEFAULT_MODEL__MAX_TOKENS": "1024",
    }
    return _base_spawn(gateway_jwt=gateway_jwt, extra_env=extra)


# Backward-compat alias.
spawn_ollama_daemon = spawn_real_llm_daemon


def write_ollama_test_app(tmpdir: Path, *, app_id: str = "stab-chat",
                          gateway_jwt: str = "") -> Path:
    """Write a minimal chat app routed via the digitorn-gateway."""
    yaml_text = f"""app:
  app_id: {app_id}
  name: Stab Test Chat
  short_name: Stab
  version: 0.0.1
  description: Real-LLM stabilization test app.
runtime:
  mode: conversation
  workdir_mode: none
  tool_injection: direct
  max_turns: 20
  timeout: 120
agents:
- id: main
  role: assistant
  brain:
    provider: openai
    model: {GATEWAY_MODEL}
    backend: openai_compat
    config:
      base_url: {GATEWAY_URL}/v1
      api_key: {gateway_jwt}
    temperature: 0.3
    max_tokens: 1024
    context:
      max_tokens: 7000
      strategy: summarize
      keep_recent: 8
      auto_compact: true
  system_prompt: |
    You are a concise test assistant. Answer briefly. Follow the
    user's instructions exactly. When asked to repeat or echo, do so
    verbatim with no extra text.
tools:
  modules:
    memory:
      config:
        auto_remember: false
        working_memory: true
  capabilities:
    default_policy: auto
    grant:
    - module: memory
      actions:
      - set_goal
      - remember
      - task_create
      - task_update
"""
    target = tmpdir / f"{app_id}.yaml"
    target.write_text(yaml_text, encoding="utf-8")
    return target


def health_loop_stalls(daemon_url: str) -> dict:
    """Read the daemon's /health to capture event-loop watchdog stats.
    Useful to detect stalls accumulated during a test."""
    import httpx
    try:
        r = httpx.get(f"{daemon_url}/health", timeout=5.0)
        if r.status_code != 200:
            return {"status_code": r.status_code, "body": r.text[:200]}
        body = r.json() or {}
        wd = body.get("event_loop_watchdog") or {}
        return {
            "stalls_total": wd.get("stalls_total"),
            "last_stall_gap_ms": wd.get("last_stall_gap_ms"),
            "threshold_s": wd.get("threshold_s"),
            "active_turns": (body.get("workers", {}).get("turn_pool") or {}).get("active_turns"),
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

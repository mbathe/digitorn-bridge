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


OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "qwen25-7b-gpu:latest"


def spawn_ollama_daemon(*, gateway_jwt: str = ""):
    """Spawn an isolated daemon pointing its default model at the
    local Ollama. Returns a context manager yielding a ``DaemonHandle``."""
    extra = {
        # Override the default-model env vars so the daemon's brain
        # talks to Ollama instead of the gateway. Provider ``openai``
        # + ``backend openai_compat`` + Ollama's OpenAI-compatible
        # endpoint is the path with the lowest moving parts.
        "DIGITORN_DEFAULT_MODEL__PROVIDER": "openai",
        "DIGITORN_DEFAULT_MODEL__MODEL": OLLAMA_MODEL,
        "DIGITORN_DEFAULT_MODEL__BACKEND": "openai_compat",
        "DIGITORN_DEFAULT_MODEL__BASE_URL": f"{OLLAMA_URL}/v1",
        "DIGITORN_DEFAULT_MODEL__API_KEY": "ollama",
        "DIGITORN_DEFAULT_MODEL__MAX_TOKENS": "2048",
    }
    return _base_spawn(gateway_jwt=gateway_jwt, extra_env=extra)


def write_ollama_test_app(tmpdir: Path, *, app_id: str = "stab-chat") -> Path:
    """Write a minimal-but-real chat app pointed at Ollama.

    No middleware, no behavior classifier, no MCP -- just the brain +
    a single tool module so we can exercise tool_call events without
    extra LLM round-trips.
    """
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
    model: {OLLAMA_MODEL}
    backend: openai_compat
    config:
      base_url: {OLLAMA_URL}/v1
      api_key: ollama
      num_ctx: 32768
    temperature: 0.3
    max_tokens: 1024
    context:
      max_tokens: 16000
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

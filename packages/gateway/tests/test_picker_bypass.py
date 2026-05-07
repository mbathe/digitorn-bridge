"""Real tests for the credential-picker bypass fix.

This is the morning bug: a Digitorn user whose YAML referenced
``credential: { scope: per_user }`` saw the picker dialog even though
they were going to route via gateway anyway. The fix added two
helpers in ``inject_session_time.py``:

  - ``_gateway_will_route_for_brains`` -> pre-flight check
  - ``_brain_is_local`` -> safety: never skip for ollama/lm_studio

We test every input combo so the fix can't silently regress.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


DAEMON_SRC = Path(__file__).resolve().parent.parent.parent / "digitorn"
sys.path.insert(0, str(DAEMON_SRC))


@pytest.fixture
def helpers():
    from digitorn.core.credentials.inject_session_time import (
        _gateway_will_route_for_brains, _brain_is_local,
    )
    return _gateway_will_route_for_brains, _brain_is_local


# ── _gateway_will_route_for_brains ───────────────────────────────


@pytest.mark.parametrize("uid", [
    "", "  ", "local", "anonymous", "system", "admin",
    "LOCAL", " System ", "ANONYMOUS",
])
def test_anonymous_user_routes_locally(helpers, uid):
    """Anonymous / pseudo users keep the deployed provider, so brain
    creds DO need to be resolved (False = "do resolve")."""
    will_route, _ = helpers
    assert will_route(user_id=uid, app_id="app", byok_overrides={}) is False


def test_real_user_no_byok_routes_via_gateway(helpers):
    will_route, _ = helpers
    assert will_route(user_id="real-user", app_id="app", byok_overrides={}) is True


def test_real_user_with_byok_does_NOT_route(helpers):
    """When BYOK overrides exist, at least one agent has BYOK ON ->
    keep the resolution machinery armed."""
    will_route, _ = helpers
    assert will_route(
        user_id="real-user", app_id="app",
        byok_overrides={"agent_X": {"ref": "byok_anthropic"}},
    ) is False


def test_none_user_id_routes_locally(helpers):
    will_route, _ = helpers
    assert will_route(user_id=None, app_id="app", byok_overrides={}) is False


# ── _brain_is_local ─────────────────────────────────────────────


@pytest.mark.parametrize("name", ["ollama", "lm_studio", "vllm",
                                    "llama_cpp", "lmstudio"])
def test_local_provider_detected(helpers, name):
    _, is_local = helpers
    brain = type("_B", (), {"provider": name})()
    assert is_local(brain) is True


@pytest.mark.parametrize("name", ["anthropic", "openai", "deepseek",
                                    "github_copilot", "claude_code",
                                    "azure_openai", "vertex_ai"])
def test_remote_provider_not_local(helpers, name):
    _, is_local = helpers
    brain = type("_B", (), {"provider": name})()
    assert is_local(brain) is False


def test_uppercase_local_still_detected(helpers):
    """Case-insensitive normalisation."""
    _, is_local = helpers
    brain = type("_B", (), {"provider": "Ollama"})()
    assert is_local(brain) is True


def test_inline_brain_detected(helpers):
    """CompiledBrain shape with inline_config dict."""
    _, is_local = helpers
    brain = type("_B", (), {
        "provider": "",
        "inline_config": {"provider": "vllm"},
    })()
    assert is_local(brain) is True


def test_brain_without_provider_not_local(helpers):
    _, is_local = helpers
    brain = type("_B", (), {})()
    assert is_local(brain) is False

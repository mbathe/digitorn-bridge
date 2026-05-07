"""Real tests for ``digitorn.core.credentials.gateway_resolver``.

The resolver decides whether each session goes through the gateway.
The 5 escape rules are the only ways a Digitorn user does NOT route
via gateway. Every rule + every off-by-one is covered here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Daemon code lives in a separate package; make it importable.
DAEMON_SRC = Path(__file__).resolve().parent.parent.parent / "digitorn"
sys.path.insert(0, str(DAEMON_SRC))


@pytest.fixture
def resolver_imports():
    from digitorn.core.credentials import gateway_resolver
    return gateway_resolver


# ── Static contract ─────────────────────────────────────────────────


def test_no_whitelist_constant(resolver_imports):
    """Option C: no GATEWAY_SUPPORTED_PROVIDERS hardcoded."""
    assert not hasattr(resolver_imports, "GATEWAY_SUPPORTED_PROVIDERS")


def test_local_providers_set(resolver_imports):
    expected = {"ollama", "lm_studio", "vllm", "llama_cpp", "lmstudio"}
    assert set(resolver_imports.LOCAL_PROVIDERS) == expected


def test_non_user_ids(resolver_imports):
    expected = {"", "local", "anonymous", "system", "admin"}
    assert set(resolver_imports.NON_USER_IDS) == expected


# ── Resolver paths ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_brain_returns_deployed(resolver_imports, fake_settings):
    """Defensive: agent without brain → caller's deployed_provider unchanged."""
    deployed = object()
    agent = type("_A", (), {"brain": None})()
    out = await resolver_imports.resolve_session_provider(
        deployed_provider=deployed,
        agent=agent,
        user_id="real-user",
        app_id="some-app",
        modules={},
        settings=fake_settings,
    )
    assert out is deployed


@pytest.mark.asyncio
async def test_byok_keeps_deployed(resolver_imports, fake_settings, fake_agent):
    """Rule 1: BYOK ON → KEEP, no matter what else is true."""
    deployed = object()
    out = await resolver_imports.resolve_session_provider(
        deployed_provider=deployed,
        agent=fake_agent,
        user_id="real-user",
        app_id="some-app",
        modules={"llm_provider": object()},
        settings=fake_settings,
        byok_enabled=True,
    )
    assert out is deployed


@pytest.mark.asyncio
async def test_gateway_disabled_keeps_deployed(resolver_imports, fake_agent):
    """Rule 2: settings.runtime.gateway_enabled=False → KEEP."""
    deployed = object()
    class _S:
        class runtime:
            gateway_enabled = False
            gateway_base_url = "http://gw.test/v1"
    out = await resolver_imports.resolve_session_provider(
        deployed_provider=deployed,
        agent=fake_agent,
        user_id="real-user",
        app_id="some-app",
        modules={"llm_provider": object()},
        settings=_S(),
    )
    assert out is deployed


@pytest.mark.asyncio
@pytest.mark.parametrize("uid", ["", "local", "anonymous", "system", "admin",
                                   "  Local ", "ANONYMOUS", None])
async def test_anonymous_user_keeps_deployed(
    resolver_imports, fake_settings, fake_agent, uid,
):
    """Rule 3: anonymous / pseudo-user → KEEP. Case-insensitive, trim-tolerant."""
    deployed = object()
    out = await resolver_imports.resolve_session_provider(
        deployed_provider=deployed,
        agent=fake_agent,
        user_id=uid,
        app_id="some-app",
        modules={"llm_provider": object()},
        settings=fake_settings,
    )
    assert out is deployed


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["ollama", "lm_studio", "vllm",
                                            "llama_cpp", "lmstudio"])
async def test_local_provider_keeps_deployed(
    resolver_imports, fake_settings, provider_name,
):
    """Rule 4: brain.provider in LOCAL_PROVIDERS → KEEP."""
    deployed = object()
    brain = type("_B", (), {"provider": provider_name, "model": "any"})()
    agent = type("_A", (), {"brain": brain})()
    out = await resolver_imports.resolve_session_provider(
        deployed_provider=deployed,
        agent=agent,
        user_id="real-user",
        app_id="some-app",
        modules={"llm_provider": object()},
        settings=fake_settings,
    )
    assert out is deployed


@pytest.mark.asyncio
async def test_missing_llm_module_keeps_deployed(
    resolver_imports, fake_settings, fake_agent,
):
    """No llm_provider module → KEEP (defensive)."""
    deployed = object()
    out = await resolver_imports.resolve_session_provider(
        deployed_provider=deployed,
        agent=fake_agent,
        user_id="real-user",
        app_id="some-app",
        modules={},  # no llm_provider
        settings=fake_settings,
    )
    assert out is deployed


# ── Provider name resolution edge cases ────────────────────────────


def test_resolve_brain_provider_direct(resolver_imports):
    """The simplest path: brain.provider is set."""
    brain = type("_B", (), {"provider": "DeepSeek", "model": "deepseek-chat"})()
    deployed = type("_D", (), {})()
    name = resolver_imports._resolve_brain_provider_name(brain, deployed)
    assert name == "deepseek"  # case normalised


def test_resolve_brain_provider_inline(resolver_imports):
    """Brain compiled as inline_config dict (CompiledBrain shape)."""
    brain = type("_B", (), {
        "provider": "",
        "inline_config": {"provider": "Anthropic"},
        "provider_id": "",
    })()
    deployed = type("_D", (), {})()
    name = resolver_imports._resolve_brain_provider_name(brain, deployed)
    assert name == "anthropic"


def test_resolve_brain_provider_falls_back_to_hint(resolver_imports):
    """When brain has only provider_id, fall back to the deployed
    provider's provider_hint (set by the daemon when it instantiated
    the live provider)."""
    brain = type("_B", (), {
        "provider": "",
        "provider_id": "deepseek_main",
    })()
    deployed = type("_D", (), {"provider_hint": "deepseek"})()
    name = resolver_imports._resolve_brain_provider_name(brain, deployed)
    assert name == "deepseek"


def test_resolve_brain_provider_unknown_returns_pid(resolver_imports):
    """Unknown provider_id without a matching hint returns the pid raw."""
    brain = type("_B", (), {
        "provider": "",
        "provider_id": "weird_custom_thing",
    })()
    deployed = type("_D", (), {})()
    name = resolver_imports._resolve_brain_provider_name(brain, deployed)
    assert name == "weird_custom_thing"

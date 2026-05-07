"""Pytest fixtures for gateway tests.

Goal: every test runs in <100ms, deterministic, no network. We mock
``litellm.acompletion`` via respx (HTTP-level) when we want to assert
upstream-shape behaviours, and we hit ``ConfigCache`` / dispatchers
directly when we want unit-style coverage.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

# The gateway package isn't installed in editable mode in this repo,
# so make src importable from the tests dir.
ROOT = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(ROOT))

# Master key needed by cipher imports. Use a well-known dev key (32B).
os.environ.setdefault(
    "DIGITORN_GATEWAY_MASTER_KEY",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
)


# ── Cache reset ─────────────────────────────────────────────────────


@pytest.fixture
def fresh_cache(monkeypatch):
    """Fresh ConfigCache singleton + fresh ConnectionPool per test.
    Without this, tests poison each other through module-level state."""
    from digitorn_gateway import config_cache as cc
    from digitorn_gateway import connection_pool as cp

    new_cache = cc.ConfigCache()
    monkeypatch.setattr(cc, "_cache", new_cache)
    new_pool = cp.ConnectionPool()
    monkeypatch.setattr(cp, "_default_pool", new_pool)

    yield new_cache


# ── Sample data ─────────────────────────────────────────────────────


@pytest.fixture
def sample_provider(fresh_cache):
    """An openai_compat provider with our extras_metadata shape."""
    fresh_cache.upsert_provider(
        slug="testprovider",
        name="Test Provider",
        base_url="https://api.test.example.com",
        compat="openai_compat",
        env_var=None,
        auth_type="api_key",
        extra_metadata={
            "dispatch_headers": {"X-Test-Trace": "1"},
        },
    )
    return fresh_cache._providers["testprovider"]


@pytest.fixture
def sample_credential(fresh_cache, sample_provider):
    """A live api_key credential bound to ``testprovider``."""
    cid = uuid.uuid4()
    fresh_cache.upsert_credential(
        cid,
        provider_slug="testprovider",
        label="default",
        secret_data={"value": "sk-test-1234"},
        status="active",
        live_pool=True,
    )
    return fresh_cache._credentials[cid]


@pytest.fixture
def sample_model(fresh_cache, sample_provider):
    """A model alias on ``testprovider``."""
    fresh_cache.upsert_model(
        alias="testalias",
        provider_slug="testprovider",
        real_model_id="real-model-id",
        cost_per_1k_input=0.001,
        cost_per_1k_output=0.002,
        max_context=8192,
        is_custom=False,
    )
    return fresh_cache._models["testalias"]


@pytest.fixture
def sample_route(fresh_cache, sample_model, sample_credential, sample_provider):
    """A route binding the alias to the credential at priority 0."""
    rid = uuid.uuid4()
    fresh_cache.set_route(
        rid,
        alias="testalias",
        credential_id=sample_credential.id,
        priority=0,
        provider_slug=sample_model.provider_slug,
        real_model_id=sample_model.real_model_id,
        compat=sample_provider.compat,
        base_url=sample_provider.base_url,
        dispatch_headers={"X-Test-Trace": "1"},
    )
    return fresh_cache._routes["testalias"][0]


# ── Resolver fixtures ────────────────────────────────────────────────


@pytest.fixture
def fake_settings():
    """Lightweight stub mirroring the daemon ``Settings.runtime`` shape."""
    class _Runtime:
        gateway_enabled = True
        gateway_base_url = "http://gateway.test/v1"
    class _Settings:
        runtime = _Runtime()
    return _Settings()


@pytest.fixture
def fake_brain():
    """Brain with the ``provider`` attribute the resolver introspects."""
    class _Brain:
        provider = "anthropic"
        model = "claude-sonnet-4"
    return _Brain()


@pytest.fixture
def fake_agent(fake_brain):
    """Agent wrapper carrying the brain - the resolver only reads ``brain``."""
    class _Agent:
        brain = fake_brain
    return _Agent()

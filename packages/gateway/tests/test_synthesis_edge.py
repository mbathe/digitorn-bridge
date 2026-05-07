"""Edge cases of the synthetic-alias path in ``ConfigCache``.

The daemon historically sends ``<provider_slug>/<real_model>`` to the
gateway. Without proper synthesis these fell through to LiteLLM
which crashed on github_copilot specifically (LiteLLM's broken
native connector). The fix synthesises a CachedModel on the fly when
the prefix matches a known provider. We test every degenerate input.
"""
from __future__ import annotations

import uuid

import pytest


def test_synthesis_no_slash_returns_none(fresh_cache, sample_provider):
    """Bare alias not in the cache → None (the synthesis path is bypassed)."""
    assert fresh_cache._synthesize_model("just-a-bare-name") is None


def test_synthesis_unknown_prefix_returns_none(fresh_cache):
    """Prefix isn't a known provider → None."""
    assert fresh_cache._synthesize_model("nonexistent/whatever") is None


def test_synthesis_empty_suffix_returns_none(fresh_cache, sample_provider):
    """Trailing slash with no suffix → None."""
    assert fresh_cache._synthesize_model("testprovider/") is None


def test_synthesis_empty_prefix_returns_none(fresh_cache):
    """Leading slash → empty prefix → None."""
    assert fresh_cache._synthesize_model("/foo") is None


def test_synthesis_inherits_existing_alias(fresh_cache, sample_provider, sample_model):
    """When a real alias exists with the same (provider, real_model_id),
    we inherit its costs/max_context instead of synthesising zeros."""
    m = fresh_cache._synthesize_model("testprovider/real-model-id")
    assert m is not None
    assert m.cost_per_1k_input == 0.001  # from sample_model
    assert m.cost_per_1k_output == 0.002
    assert m.max_context == 8192


def test_synthesis_creates_zero_cost_when_no_match(fresh_cache, sample_provider):
    """No existing alias for that (provider, real_model) tuple → fresh
    synthesised entry with zero costs, the dispatch path tolerates that."""
    m = fresh_cache._synthesize_model("testprovider/never-aliased-model")
    assert m is not None
    assert m.alias == "testprovider/never-aliased-model"
    assert m.provider_slug == "testprovider"
    assert m.real_model_id == "never-aliased-model"
    assert m.cost_per_1k_input == 0.0
    assert m.cost_per_1k_output == 0.0


def test_synthesis_multi_slash_only_splits_first(fresh_cache, sample_provider):
    """Together AI gives ``meta-llama/Llama-3.3-70B-Instruct-Turbo``
    style models. After ``provider/`` prefix that becomes
    ``together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo`` -- split
    on FIRST slash only so the suffix keeps its slashes."""
    m = fresh_cache._synthesize_model("testprovider/meta-llama/Llama-3.3-70B")
    assert m is not None
    assert m.real_model_id == "meta-llama/Llama-3.3-70B"


def test_synthesis_chain_via_resolve_dispatch(
    fresh_cache, sample_provider, sample_credential,
):
    """End-to-end: an unknown alias matching ``<known>/<anything>``
    should resolve through the cache (with the provider's default
    credential) and produce a dispatchable ResolvedDispatch."""
    fresh_cache._provider_default_cred["testprovider"] = sample_credential.id
    resolved = fresh_cache.resolve_dispatch("testprovider/some-fresh-model")
    assert resolved is not None
    assert resolved.real_model_id == "some-fresh-model"
    assert resolved.provider_slug == "testprovider"


def test_synthesis_returns_none_when_no_credential(
    fresh_cache, sample_provider,
):
    """Provider exists but has no credential and no env var → resolver
    returns None and the gateway will surface a clean 404."""
    resolved = fresh_cache.resolve_dispatch("testprovider/foo")
    assert resolved is None

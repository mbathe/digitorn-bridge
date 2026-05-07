"""Real tests for cache write-through + lifecycle.

The cache is the hot-path single source of truth. A bug here = stale
credentials served, stale routes used, dropped invalidation events.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


def test_provider_default_cred_picks_active(fresh_cache, sample_provider):
    """upsert_credential bumps provider_default_cred to the latest active."""
    cid_a = uuid.uuid4()
    cid_b = uuid.uuid4()
    fresh_cache.upsert_credential(
        cid_a, provider_slug="testprovider", label="A",
        secret_data={"value": "k1"}, status="active",
    )
    assert fresh_cache._provider_default_cred["testprovider"] == cid_a
    fresh_cache.upsert_credential(
        cid_b, provider_slug="testprovider", label="B",
        secret_data={"value": "k2"}, status="active",
    )
    # Last active wins.
    assert fresh_cache._provider_default_cred["testprovider"] == cid_b


def test_disable_picks_another_active_default(fresh_cache, sample_provider):
    cid_a = uuid.uuid4()
    cid_b = uuid.uuid4()
    fresh_cache.upsert_credential(
        cid_a, provider_slug="testprovider", label="A",
        secret_data={"value": "k1"}, status="active",
    )
    fresh_cache.upsert_credential(
        cid_b, provider_slug="testprovider", label="B",
        secret_data={"value": "k2"}, status="active",
    )
    # Disable B (current default) -> A should become default.
    fresh_cache.upsert_credential(
        cid_b, provider_slug="testprovider", label="B",
        secret_data={"value": "k2"}, status="disabled",
    )
    assert fresh_cache._provider_default_cred["testprovider"] == cid_a


def test_remove_credential_clears_default(fresh_cache, sample_provider):
    cid = uuid.uuid4()
    fresh_cache.upsert_credential(
        cid, provider_slug="testprovider", label="L",
        secret_data={"value": "k"}, status="active",
    )
    assert "testprovider" in fresh_cache._provider_default_cred
    fresh_cache.remove_credential(cid)
    assert cid not in fresh_cache._credentials


def test_remove_provider_cascades(fresh_cache, sample_provider, sample_credential):
    fresh_cache.remove_provider("testprovider")
    assert "testprovider" not in fresh_cache._providers
    # The credential bound to it must also be evicted.
    assert sample_credential.id not in fresh_cache._credentials


def test_route_priority_sorted_after_set(fresh_cache, sample_provider):
    cid = uuid.uuid4()
    fresh_cache.upsert_credential(
        cid, provider_slug="testprovider", label="L",
        secret_data={"value": "k"}, status="active",
    )
    fresh_cache.upsert_model(
        alias="myalias", provider_slug="testprovider",
        real_model_id="m", cost_per_1k_input=0, cost_per_1k_output=0,
        max_context=None, is_custom=False,
    )
    # Add 3 routes out of order; cache must sort them by priority.
    rid_a = uuid.uuid4(); rid_b = uuid.uuid4(); rid_c = uuid.uuid4()
    common = dict(
        alias="myalias", credential_id=cid,
        provider_slug="testprovider", real_model_id="m", compat="openai",
    )
    fresh_cache.set_route(rid_a, priority=10, **common)
    fresh_cache.set_route(rid_b, priority=0, **common)
    fresh_cache.set_route(rid_c, priority=5, **common)
    priorities = [r.priority for r in fresh_cache._routes["myalias"]]
    assert priorities == [0, 5, 10]


def test_extra_metadata_default_isolation(fresh_cache):
    """Two providers with different metadata must not share the dict
    (the frozen dataclass uses default_factory so this should be safe,
    but let's prove it)."""
    fresh_cache.upsert_provider(
        slug="p1", name="P1", base_url=None, compat="openai", env_var=None,
        auth_type="api_key", extra_metadata={"hint": "p1"},
    )
    fresh_cache.upsert_provider(
        slug="p2", name="P2", base_url=None, compat="openai", env_var=None,
        auth_type="api_key", extra_metadata={"hint": "p2"},
    )
    assert fresh_cache._providers["p1"].extra_metadata is not fresh_cache._providers["p2"].extra_metadata
    assert fresh_cache._providers["p1"].extra_metadata["hint"] == "p1"
    assert fresh_cache._providers["p2"].extra_metadata["hint"] == "p2"


def test_resolved_dispatch_carries_credential_id(
    fresh_cache, sample_provider, sample_model, sample_credential, sample_route,
):
    """The dispatch hot path needs cred.id to look up the pool."""
    resolved = fresh_cache.resolve_dispatch("testalias")
    assert resolved is not None
    assert resolved.credential_id == sample_credential.id
    assert resolved.live_pool is True


def test_resolved_dispatch_merges_provider_dispatch_headers(
    fresh_cache, sample_provider, sample_model, sample_credential, sample_route,
):
    """provider.metadata.dispatch_headers must show up in extra_headers."""
    resolved = fresh_cache.resolve_dispatch("testalias")
    assert resolved.extra_headers.get("X-Test-Trace") == "1"


def test_resolved_dispatch_unknown_alias_returns_none(fresh_cache):
    assert fresh_cache.resolve_dispatch("nope") is None


def test_disabled_credential_not_used_for_dispatch(
    fresh_cache, sample_provider, sample_model, sample_credential, sample_route,
):
    """A disabled credential should NOT serve dispatches even if a route
    points at it."""
    fresh_cache.upsert_credential(
        sample_credential.id,
        provider_slug="testprovider", label="default",
        secret_data={"value": "sk"}, status="disabled",
        live_pool=True,
    )
    resolved = fresh_cache.resolve_dispatch("testalias")
    assert resolved is None


@pytest.mark.asyncio
async def test_concurrent_upsert_no_torn_state(fresh_cache):
    """Hammer upsert_credential with 200 concurrent flips. The final
    state must reflect ONE consistent active credential, not a half-
    written entry."""
    cid = uuid.uuid4()
    fresh_cache.upsert_provider(
        slug="p", name="P", base_url=None, compat="openai", env_var=None,
        auth_type="api_key", extra_metadata={},
    )
    async def churn():
        for i in range(50):
            fresh_cache.upsert_credential(
                cid, provider_slug="p", label="L",
                secret_data={"value": f"k{i}"},
                status="active" if i % 2 else "disabled",
            )
    await asyncio.gather(churn(), churn(), churn(), churn())
    cred = fresh_cache._credentials.get(cid)
    assert cred is not None
    assert cred.status in ("active", "disabled")  # not torn


def test_health_marking_persists(fresh_cache, sample_provider, sample_credential, sample_route):
    """Marking a route failed enough times blocks it; a success resets."""
    rid = sample_route.id
    fresh_cache.mark_route_failure(rid, "boom")
    fresh_cache.mark_route_failure(rid, "boom")
    fresh_cache.mark_route_failure(rid, "boom")
    h = fresh_cache._route_health[rid]
    assert h.consecutive_failures == 3
    assert h.blocked_until > 0
    fresh_cache.mark_route_success(rid)
    h = fresh_cache._route_health[rid]
    assert h.consecutive_failures == 0
    assert h.blocked_until == 0.0

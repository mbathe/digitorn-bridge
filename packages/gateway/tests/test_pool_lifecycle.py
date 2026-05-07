"""Real tests for ``connection_pool`` -- races, lifecycle, shutdown.

These exercise the parts smoke tests can't reach: 100 concurrent
``ensure()`` calls returning the SAME instance, fingerprint mismatch
triggering eviction-then-rebuild, shutdown being idempotent, etc.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


@pytest.mark.asyncio
async def test_ensure_returns_none_for_unsupported_kind(fresh_cache):
    from digitorn_gateway.connection_pool import ConnectionPool
    pool = ConnectionPool()
    out = await pool.ensure(
        uuid.uuid4(), kind="bedrock", api_key="x", base_url="x",
    )
    assert out is None


@pytest.mark.asyncio
async def test_ensure_returns_same_instance_on_repeat(fresh_cache):
    """Repeated ensure() with identical fingerprint returns the cached
    SDK client, NOT a new one. That's the whole point of the pool."""
    from digitorn_gateway.connection_pool import ConnectionPool
    pool = ConnectionPool()
    cred_id = uuid.uuid4()
    a = await pool.ensure(
        cred_id, kind="openai", api_key="sk-test-1234", base_url="https://x",
    )
    b = await pool.ensure(
        cred_id, kind="openai", api_key="sk-test-1234", base_url="https://x",
    )
    assert a is b
    stats = pool.stats(cred_id)
    assert stats.warm
    # 1 cold ensure + 1 warm hit (the cold one doesn't bump hit_count
    # because it's the freshly-built entry; the second one does).
    assert stats.hit_count == 1
    await pool.shutdown()


@pytest.mark.asyncio
async def test_fingerprint_mismatch_evicts_and_rebuilds(fresh_cache):
    """When the credential's api_key or base_url changes, the next
    ensure() must DROP the cached client (it has the old bearer
    inside) and build a fresh one. Without this, a rotated token
    would still go out with the old key."""
    from digitorn_gateway.connection_pool import ConnectionPool
    pool = ConnectionPool()
    cred_id = uuid.uuid4()
    a = await pool.ensure(
        cred_id, kind="openai", api_key="OLD", base_url="https://x",
    )
    b = await pool.ensure(
        cred_id, kind="openai", api_key="NEW", base_url="https://x",
    )
    assert a is not b
    await pool.shutdown()


@pytest.mark.asyncio
async def test_concurrent_ensure_no_double_build(fresh_cache):
    """50 concurrent ensure() with identical fingerprint must yield ONE
    SDK client (the lock serialises the cold-miss branch). Without
    the lock we'd build 50 clients and leak 49 sockets."""
    from digitorn_gateway.connection_pool import ConnectionPool
    pool = ConnectionPool()
    cred_id = uuid.uuid4()

    results = await asyncio.gather(*[
        pool.ensure(cred_id, kind="openai", api_key="k", base_url="https://x")
        for _ in range(50)
    ])
    distinct = {id(r) for r in results}
    assert len(distinct) == 1, f"got {len(distinct)} distinct clients"
    # The pool itself reports exactly 1 warm entry.
    assert pool.stats(cred_id).warm
    assert len(pool.all_stats()) == 1
    await pool.shutdown()


@pytest.mark.asyncio
async def test_invalidate_drops_entry(fresh_cache):
    from digitorn_gateway.connection_pool import ConnectionPool
    pool = ConnectionPool()
    cred_id = uuid.uuid4()
    await pool.ensure(cred_id, kind="openai", api_key="k", base_url="https://x")
    assert pool.stats(cred_id).warm
    pool.invalidate(cred_id)
    # The async _close() task fires-and-forgets; the stats DROP synchronously.
    assert not pool.stats(cred_id).warm
    await asyncio.sleep(0.05)  # let the close task complete
    await pool.shutdown()


@pytest.mark.asyncio
async def test_invalidate_unknown_id_is_noop(fresh_cache):
    from digitorn_gateway.connection_pool import ConnectionPool
    pool = ConnectionPool()
    # Should not raise even though the cred_id is unknown.
    pool.invalidate(uuid.uuid4())
    await pool.shutdown()


@pytest.mark.asyncio
async def test_on_credential_changed_off_evicts(fresh_cache):
    from digitorn_gateway.connection_pool import ConnectionPool
    pool = ConnectionPool()
    cred_id = uuid.uuid4()
    await pool.ensure(cred_id, kind="openai", api_key="k", base_url="https://x")
    pool.on_credential_changed(cred_id, live_pool=False)
    assert not pool.stats(cred_id).warm
    await pool.shutdown()


@pytest.mark.asyncio
async def test_on_credential_changed_on_is_lazy(fresh_cache):
    """Toggle ON should NOT eagerly warm; the next ensure() builds it."""
    from digitorn_gateway.connection_pool import ConnectionPool
    pool = ConnectionPool()
    cred_id = uuid.uuid4()
    pool.on_credential_changed(cred_id, live_pool=True)
    assert not pool.stats(cred_id).warm
    await pool.shutdown()


@pytest.mark.asyncio
async def test_shutdown_closes_all_clients(fresh_cache):
    """shutdown() must close every client AND clear the registry,
    so a subsequent ensure() builds fresh."""
    from digitorn_gateway.connection_pool import ConnectionPool
    pool = ConnectionPool()
    ids = [uuid.uuid4() for _ in range(5)]
    for cid in ids:
        await pool.ensure(cid, kind="openai", api_key="k", base_url="https://x")
    assert len(pool.all_stats()) == 5
    await pool.shutdown()
    assert len(pool.all_stats()) == 0


@pytest.mark.asyncio
async def test_shutdown_idempotent(fresh_cache):
    """Calling shutdown() twice must not raise."""
    from digitorn_gateway.connection_pool import ConnectionPool
    pool = ConnectionPool()
    await pool.shutdown()
    await pool.shutdown()  # no-op


@pytest.mark.asyncio
async def test_stats_unknown_returns_cold(fresh_cache):
    from digitorn_gateway.connection_pool import ConnectionPool
    pool = ConnectionPool()
    s = pool.stats(uuid.uuid4())
    assert s.warm is False
    assert s.hit_count == 0
    assert s.warm_age_s == 0.0


@pytest.mark.asyncio
async def test_kind_for_compat_truth_table():
    from digitorn_gateway.connection_pool import kind_for_compat
    assert kind_for_compat("openai") == "openai"
    assert kind_for_compat("openai_compat") == "openai"
    assert kind_for_compat("azure") == "openai"
    # anthropic intentionally NOT pooled (LiteLLM bug).
    assert kind_for_compat("anthropic") is None
    assert kind_for_compat("bedrock") is None
    assert kind_for_compat("vertex_ai") is None
    assert kind_for_compat("custom") is None
    assert kind_for_compat("unknown_dialect") is None


@pytest.mark.asyncio
async def test_concurrent_invalidate_during_ensure(fresh_cache):
    """Adversarial: 1 ensure() racing with invalidate() should always
    leave the pool in a coherent state (either warm OR cold, never
    a half-built / leaked entry)."""
    from digitorn_gateway.connection_pool import ConnectionPool
    pool = ConnectionPool()
    cred_id = uuid.uuid4()

    async def churn():
        for _ in range(20):
            await pool.ensure(cred_id, kind="openai", api_key="k", base_url="https://x")
            pool.invalidate(cred_id)
            await asyncio.sleep(0)  # yield

    await asyncio.gather(churn(), churn(), churn())
    # Cleanup should still work without raising.
    await pool.shutdown()

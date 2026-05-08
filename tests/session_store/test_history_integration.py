"""Integration: ``history.record()`` fan-out into the SessionStore.

Verifies the wire-up between the legacy persistence entry point and
the new SessionStore bridge. Three modes:

  * OFF (default) -- record() goes ONLY to the legacy path
  * SHADOW        -- record() goes to BOTH stores
  * PRIMARY       -- record() goes ONLY to the SessionStore (legacy DB
    skipped). The fast path the daemon flips to once shadow has
    validated byte-identical behavior.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.bridge import (
    BridgeMode, SessionStoreBridge,
    get_default_bridge, set_default_bridge,
)
from digitorn.core.runtime.session_store.store import InMemorySessionStore


@pytest.mark.asyncio
async def test_history_record_routes_to_bridge_when_set(tmp_root: Path):
    """history.record() with a registered bridge in SHADOW mode lands
    the row in the SessionStore. Does NOT require a live Postgres."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    bridge = SessionStoreBridge(store, mode=BridgeMode.SHADOW)
    set_default_bridge(bridge)
    try:
        await store.open("s", app_id="a", user_id="u")
        from digitorn.core.history import record
        await record(
            kind="event", type="user_message", session_id="s",
            user_id="u", role="user", content="hi", seq=1,
        )
        assert bridge.routed == 1
        state = store.state("s")
        assert state.last_seq == 1
        assert state.messages[0].content == "hi"
    finally:
        set_default_bridge(None)
        await store.stop()


@pytest.mark.asyncio
async def test_history_record_no_bridge_no_op(tmp_root: Path):
    """Without a registered bridge, the SessionStore is untouched."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    set_default_bridge(None)
    try:
        await store.open("s", app_id="a", user_id="u")
        from digitorn.core.history import record
        try:
            await record(
                kind="event", type="user_message", session_id="s",
                user_id="u", role="user", content="hi", seq=1,
            )
        except Exception:
            pass
        assert store.state("s").last_seq == 0
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_history_record_primary_skips_legacy(tmp_root: Path):
    """PRIMARY mode routes ONLY to the SessionStore. The legacy DB
    path is bypassed entirely (returns None immediately)."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    bridge = SessionStoreBridge(store, mode=BridgeMode.PRIMARY)
    set_default_bridge(bridge)
    try:
        await store.open("s", app_id="a", user_id="u")
        from digitorn.core.history import record
        result = await record(
            kind="event", type="token", session_id="s",
            user_id="u", role="assistant", content="x", seq=1,
        )
        assert result is None
        assert bridge.routed == 1
        assert store.state("s").last_seq == 1
    finally:
        set_default_bridge(None)
        await store.stop()


@pytest.mark.asyncio
async def test_history_record_off_mode_does_nothing(tmp_root: Path):
    """OFF mode: bridge is registered but explicitly disabled. The
    SessionStore receives nothing; legacy path is the only one."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    bridge = SessionStoreBridge(store, mode=BridgeMode.OFF)
    set_default_bridge(bridge)
    try:
        await store.open("s", app_id="a", user_id="u")
        from digitorn.core.history import record
        try:
            await record(
                kind="event", type="token", session_id="s",
                user_id="u", role="assistant", content="x", seq=1,
            )
        except Exception:
            pass
        assert bridge.routed == 0
        assert store.state("s").last_seq == 0
    finally:
        set_default_bridge(None)
        await store.stop()

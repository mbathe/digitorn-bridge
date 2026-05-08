"""Bridge: routes history.record() shape into SessionStore."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.bridge import (
    BridgeMode, SessionStoreBridge,
    get_default_bridge, resolve_mode_from_env, set_default_bridge,
)
from digitorn.core.runtime.session_store.store import InMemorySessionStore


@pytest.mark.asyncio
async def test_bridge_routes_record_to_store(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    bridge = SessionStoreBridge(store, mode=BridgeMode.SHADOW)
    try:
        await store.open("s", app_id="a", user_id="u")
        seq = await bridge.record(
            kind="message", type="user_message",
            session_id="s", user_id="u", app_id="a",
            role="user", content="hello",
            correlation_id="cor-1",
        )
        assert seq == 1
        state = store.state("s")
        assert len(state.messages) == 1
        assert state.messages[0].content == "hello"
        assert state.events[0].correlation_id == "cor-1"
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_bridge_off_mode_skips(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    bridge = SessionStoreBridge(store, mode=BridgeMode.OFF)
    try:
        await store.open("s", app_id="a", user_id="u")
        seq = await bridge.record(
            kind="message", type="user_message",
            session_id="s", user_id="u", role="user", content="hi",
        )
        assert seq is None
        state = store.state("s")
        assert state.event_count() == 0
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_bridge_drops_no_session_id(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    bridge = SessionStoreBridge(store, mode=BridgeMode.SHADOW)
    try:
        seq = await bridge.record(
            kind="audit", type="user.disable",
            actor_user_id="admin", target_user_id="u",
        )
        assert seq is None
        assert bridge.dropped_no_session == 1
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_bridge_auto_opens_unknown_session(tmp_root: Path):
    """The bridge auto-opens a session on the first record() call so
    the daemon doesn't have to wire an explicit open() into every
    session creation path. The session ends up in the store with the
    event landed at seq=1."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    bridge = SessionStoreBridge(store, mode=BridgeMode.SHADOW)
    try:
        seq = await bridge.record(
            kind="event", type="token", app_id="a",
            session_id="auto-opened", user_id="u",
            role="assistant", content="x",
        )
        assert seq == 1, f"expected seq=1, got {seq}"
        assert bridge.dropped_unopened == 0
        assert bridge.routed == 1
        state = store.state("auto-opened")
        assert state is not None
        assert state.last_seq == 1
        assert state.app_id == "a"
        assert state.user_id == "u"
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_bridge_overrides_caller_seq(tmp_root: Path):
    """The bridge must always use the SessionStore allocator, never
    trust caller-provided seq. Two independent callers passing their
    own seq=999 must not collide."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    bridge = SessionStoreBridge(store, mode=BridgeMode.SHADOW)
    try:
        await store.open("s", app_id="a", user_id="u")
        seq1 = await bridge.record(
            kind="event", type="token", session_id="s",
            user_id="u", seq=999, content="x",
        )
        seq2 = await bridge.record(
            kind="event", type="token", session_id="s",
            user_id="u", seq=999, content="y",
        )
        assert seq1 == 1
        assert seq2 == 2
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_bridge_carries_full_record_shape(tmp_root: Path):
    """All 25 history_log columns survive the bridge."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    bridge = SessionStoreBridge(store, mode=BridgeMode.SHADOW)
    try:
        await store.open("s", app_id="myapp", user_id="me")
        await bridge.record(
            kind="event", type="tool_call",
            session_id="s", app_id="myapp", user_id="me",
            actor_user_id="actor", actor_roles=["admin", "user"],
            role="assistant", content="text",
            tool_call_id="tc-1",
            tool_calls=[{"id": "x", "name": "n"}],
            name="tool-name",
            payload={"k": "v"},
            before={"prev": 1},
            after={"next": 2},
            target_user_id="t-user",
            target_app_id="t-app",
            target_resource="r",
            ip_address="1.2.3.4",
            user_agent="ua",
            correlation_id="cor",
            success=False,
            message="oops",
        )
        ev = store.state("s").events[0]
        assert ev.kind == "event"
        assert ev.type == "tool_call"
        assert ev.app_id == "myapp"
        assert ev.user_id == "me"
        assert ev.actor_user_id == "actor"
        assert ev.actor_roles == ["admin", "user"]
        assert ev.role == "assistant"
        assert ev.content == "text"
        assert ev.tool_call_id == "tc-1"
        assert ev.tool_calls == [{"id": "x", "name": "n"}]
        assert ev.name == "tool-name"
        assert ev.payload == {"k": "v"}
        assert ev.before == {"prev": 1}
        assert ev.after == {"next": 2}
        assert ev.target_user_id == "t-user"
        assert ev.target_app_id == "t-app"
        assert ev.target_resource == "r"
        assert ev.ip_address == "1.2.3.4"
        assert ev.user_agent == "ua"
        assert ev.correlation_id == "cor"
        assert ev.success is False
        assert ev.message == "oops"
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_bridge_stats(tmp_root: Path):
    """Auto-open changes the semantics of dropped_unopened: a record()
    targeting an unknown session now routes (after the auto-open)
    instead of dropping. Only the no-session-id case still drops."""
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    bridge = SessionStoreBridge(store, mode=BridgeMode.SHADOW)
    try:
        await store.open("s", app_id="a", user_id="u")
        await bridge.record(kind="event", type="token", session_id="s", user_id="u")
        await bridge.record(kind="event", type="token", session_id="s", user_id="u")
        # 'missing' session is auto-opened by the bridge -> routes now.
        await bridge.record(kind="event", type="token", session_id="missing", user_id="u")
        await bridge.record(kind="audit", type="x", actor_user_id="admin")
        stats = bridge.stats()
        assert stats["routed"] == 3
        assert stats["dropped_unopened"] == 0
        assert stats["dropped_no_session"] == 1
        assert stats["mode"] == "shadow"
    finally:
        await store.stop()


def test_resolve_mode_from_env_off(monkeypatch):
    monkeypatch.delenv("DIGITORN_SESSION_STORE_MODE", raising=False)
    assert resolve_mode_from_env() is BridgeMode.OFF


def test_resolve_mode_from_env_shadow(monkeypatch):
    monkeypatch.setenv("DIGITORN_SESSION_STORE_MODE", "shadow")
    assert resolve_mode_from_env() is BridgeMode.SHADOW


def test_resolve_mode_from_env_primary(monkeypatch):
    monkeypatch.setenv("DIGITORN_SESSION_STORE_MODE", "PRIMARY")
    assert resolve_mode_from_env() is BridgeMode.PRIMARY


def test_resolve_mode_from_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("DIGITORN_SESSION_STORE_MODE", "garbage")
    assert resolve_mode_from_env() is BridgeMode.OFF


@pytest.mark.asyncio
async def test_default_bridge_setter(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await store.start()
    try:
        assert get_default_bridge() is None
        bridge = SessionStoreBridge(store, mode=BridgeMode.SHADOW)
        set_default_bridge(bridge)
        try:
            assert get_default_bridge() is bridge
        finally:
            set_default_bridge(None)
        assert get_default_bridge() is None
    finally:
        await store.stop()

"""Daemon bootstrap: wire SessionStore + Bridge from env vars."""
from __future__ import annotations

from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.bootstrap import (
    init_session_store, shutdown_session_store,
)
from digitorn.core.runtime.session_store.bridge import (
    BridgeMode, get_default_bridge, set_default_bridge,
)


@pytest.fixture(autouse=True)
def _reset_bridge():
    set_default_bridge(None)
    yield
    set_default_bridge(None)


@pytest.mark.asyncio
async def test_init_off_mode_returns_none(monkeypatch, tmp_root: Path):
    monkeypatch.setenv("DIGITORN_SESSION_STORE_MODE", "off")
    monkeypatch.setenv("DIGITORN_SESSION_STORE_ROOT", str(tmp_root))
    store = await init_session_store()
    assert store is None
    assert get_default_bridge() is None


@pytest.mark.asyncio
async def test_init_shadow_mode(monkeypatch, tmp_root: Path):
    monkeypatch.setenv("DIGITORN_SESSION_STORE_MODE", "shadow")
    monkeypatch.setenv("DIGITORN_SESSION_STORE_ROOT", str(tmp_root))
    store = await init_session_store()
    try:
        assert store is not None
        bridge = get_default_bridge()
        assert bridge is not None
        assert bridge.mode is BridgeMode.SHADOW
        assert bridge.store is store
        assert store.root == tmp_root
    finally:
        await shutdown_session_store(store)


@pytest.mark.asyncio
async def test_init_primary_mode(monkeypatch, tmp_root: Path):
    monkeypatch.setenv("DIGITORN_SESSION_STORE_MODE", "primary")
    monkeypatch.setenv("DIGITORN_SESSION_STORE_ROOT", str(tmp_root))
    store = await init_session_store()
    try:
        assert get_default_bridge().mode is BridgeMode.PRIMARY
    finally:
        await shutdown_session_store(store)


@pytest.mark.asyncio
async def test_init_explicit_args_override_env(monkeypatch, tmp_root: Path):
    monkeypatch.setenv("DIGITORN_SESSION_STORE_MODE", "off")
    store = await init_session_store(
        root=tmp_root, mode=BridgeMode.PRIMARY, max_bytes=1024,
        max_sessions=10, flush_interval_ms=20,
    )
    try:
        assert store is not None
        assert get_default_bridge().mode is BridgeMode.PRIMARY
    finally:
        await shutdown_session_store(store)


@pytest.mark.asyncio
async def test_invalid_int_falls_back_to_default(
    monkeypatch, tmp_root: Path,
):
    monkeypatch.setenv("DIGITORN_SESSION_STORE_MODE", "shadow")
    monkeypatch.setenv("DIGITORN_SESSION_STORE_ROOT", str(tmp_root))
    monkeypatch.setenv("DIGITORN_SESSION_STORE_MAX_BYTES", "not-a-number")
    store = await init_session_store()
    try:
        assert store is not None
    finally:
        await shutdown_session_store(store)


@pytest.mark.asyncio
async def test_shutdown_with_none_is_noop():
    await shutdown_session_store(None)


@pytest.mark.asyncio
async def test_shutdown_clears_bridge(monkeypatch, tmp_root: Path):
    monkeypatch.setenv("DIGITORN_SESSION_STORE_MODE", "shadow")
    monkeypatch.setenv("DIGITORN_SESSION_STORE_ROOT", str(tmp_root))
    store = await init_session_store()
    assert get_default_bridge() is not None
    await shutdown_session_store(store)
    assert get_default_bridge() is None


@pytest.mark.asyncio
async def test_init_with_index_env(monkeypatch, tmp_root: Path):
    """DIGITORN_SESSION_INDEX_PATH wires a SqliteSessionIndex
    automatically. Closing a session upserts into it."""
    from digitorn.core.runtime.session_store.types import Event

    idx_path = tmp_root / "idx.db"
    monkeypatch.setenv("DIGITORN_SESSION_STORE_MODE", "shadow")
    monkeypatch.setenv("DIGITORN_SESSION_STORE_ROOT", str(tmp_root))
    monkeypatch.setenv("DIGITORN_SESSION_INDEX_PATH", str(idx_path))
    store = await init_session_store()
    try:
        assert store is not None
        assert store.index is not None
        assert idx_path.exists()
        await store.open("sid", app_id="a", user_id="u")
        await store.append_event(
            "sid", Event(type="user_message", role="user", content="hi"),
        )
        await store.close_session("sid")
        summary = await store.index.get("sid")
        assert summary is not None
        assert summary.user_id == "u"
    finally:
        await shutdown_session_store(store)


@pytest.mark.asyncio
async def test_init_without_index_env(monkeypatch, tmp_root: Path):
    """No DIGITORN_SESSION_INDEX_PATH = no index attached."""
    monkeypatch.setenv("DIGITORN_SESSION_STORE_MODE", "shadow")
    monkeypatch.setenv("DIGITORN_SESSION_STORE_ROOT", str(tmp_root))
    monkeypatch.delenv("DIGITORN_SESSION_INDEX_PATH", raising=False)
    store = await init_session_store()
    try:
        assert store is not None
        assert store.index is None
    finally:
        await shutdown_session_store(store)


@pytest.mark.asyncio
async def test_init_index_explicit_disabled(monkeypatch, tmp_root: Path):
    """DIGITORN_SESSION_INDEX_PATH=off explicitly disables the index."""
    monkeypatch.setenv("DIGITORN_SESSION_STORE_MODE", "shadow")
    monkeypatch.setenv("DIGITORN_SESSION_STORE_ROOT", str(tmp_root))
    monkeypatch.setenv("DIGITORN_SESSION_INDEX_PATH", "off")
    store = await init_session_store()
    try:
        assert store is not None
        assert store.index is None
    finally:
        await shutdown_session_store(store)


@pytest.mark.asyncio
async def test_full_roundtrip_via_history_record(
    monkeypatch, tmp_root: Path,
):
    """Bootstrap in primary mode, then a history.record() call lands
    in the SessionStore via the bridge -- the full integration path."""
    from digitorn.core.runtime.session_store.types import Event

    monkeypatch.setenv("DIGITORN_SESSION_STORE_MODE", "primary")
    monkeypatch.setenv("DIGITORN_SESSION_STORE_ROOT", str(tmp_root))
    store = await init_session_store()
    try:
        await store.open("s", app_id="a", user_id="u")
        from digitorn.core.history import record
        result = await record(
            kind="event", type="user_message", session_id="s",
            user_id="u", role="user", content="end-to-end-test",
            seq=1,
        )
        assert result is None
        state = store.state("s")
        assert state.last_seq == 1
        assert state.messages[0].content == "end-to-end-test"
    finally:
        await shutdown_session_store(store)

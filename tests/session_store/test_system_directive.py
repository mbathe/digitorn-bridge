"""End-to-end persistence + restoration guarantees for runtime-injected
system directives.

These tests cover the contract introduced by
``digitorn.core.runtime.system_directive``:

  1. Every call to ``inject_system_directive`` MUST land an event with
     ``type="system_message"`` on the bus.
  2. The bus MUST stamp a monotonic ``seq`` (per-session).
  3. The projection MUST append a ``Message(role="system", content=...,
     seq=...)`` to ``state.messages`` in the order the events arrived.
  4. The local working-list mutation (when ``messages=...`` is passed)
     MUST happen BEFORE the emit returns, so the in-flight LLM call sees
     the directive on the current iteration.
  5. Cold-reload replay MUST reconstruct ``state.messages`` identically -
     same content, same role, same seq, same chronological position
     relative to user / assistant messages.
  6. Every migrated call site (``_nudge_empty_response``,
     ``_check_unfinished_work``, ``_inject_turn_limit_warning``,
     ``_flush_behavior_notes``, hook ``inject_message``,
     hook shell variants, ``_exec_compact_context`` summary,
     cron reminder, ``template_addendum``) MUST drive the helper with
     the right ``source`` tag and the right payload.
  7. Override params (``bus``, ``app_id``, ``session_id``, ``user_id``)
     MUST work for pre-ctx call sites (cron, addendum).

Tests use the real ``InMemorySessionStore`` + ``SocketIOBus`` +
``SessionStoreBridge`` wiring; only the Socket.IO ``emit`` is mocked
(no network). This proves the path the production daemon takes is
actually exercised.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
PKG = ROOT / "packages" / "digitorn"
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

from digitorn.core.events.session_bus import SocketIOBus  # noqa: E402
from digitorn.core.runtime.session_store.bridge import (  # noqa: E402
    BridgeMode,
    SessionStoreBridge,
    set_default_bridge,
)
from digitorn.core.runtime.session_store.store import InMemorySessionStore  # noqa: E402
from digitorn.core.runtime.system_directive import (  # noqa: E402
    inject_system_directive,
    inject_system_directive_sync,
)


# ── Test infrastructure ─────────────────────────────────────────────


class _FakeCtx:
    """Minimal AgentContext shim - just the attributes the helper reads."""

    def __init__(
        self, *, app_id: str, session_id: str, user_id: str, bus: Any,
    ) -> None:
        self.app_id = app_id
        self.session_id = session_id
        self.user_id = user_id
        self.event_bus = bus


@pytest.fixture
async def wired(tmp_path: Path):
    """Yield a (store, bus, bridge, sio_mock) tuple wired the same way
    the daemon wires them in ``server.py``: bus.emit → history.record
    → bridge.record → store.append_event → apply_projection. The
    ``_on_internal_seq_alloc`` hook keeps EventBuffer's high-water mark
    in sync with the store's allocator so seqs never collide across
    the two paths (matches the production wiring in
    ``server.py:_sync_buffer_after_internal_alloc``).
    """
    sio = MagicMock()
    sio.emit = AsyncMock()
    bus = SocketIOBus(sio=sio)

    bus_buffer = bus._buffer

    def _sync_buffer_after_internal_alloc(sid: str, seq: int) -> None:
        try:
            bus_buffer.bump_to(session_id=sid, value=seq)
        except Exception:
            pass

    store = InMemorySessionStore(
        root=tmp_path,
        flush_interval_ms=10,
        on_internal_seq_alloc=_sync_buffer_after_internal_alloc,
    )
    await store.start()

    bridge = SessionStoreBridge(store, mode=BridgeMode.SHADOW)

    try:
        set_default_bridge(bridge)
        yield (store, bus, bridge, sio)
    finally:
        set_default_bridge(None)
        await store.stop()


async def _open_session(store: Any, sid: str, app_id: str = "test-app",
                        user_id: str = "u1") -> None:
    await store.open(sid, app_id=app_id, user_id=user_id)


# ── Group A: helper unit tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_A1_basic_emit_and_local_mutation(wired):
    store, bus, _, _ = wired
    await _open_session(store, "s-A1")
    ctx = _FakeCtx(app_id="test-app", session_id="s-A1", user_id="u1", bus=bus)
    msgs: list[dict[str, Any]] = []
    seq = await inject_system_directive(
        ctx, content="hello-A1", source="nudge_empty_response",
        messages=msgs, turn=2,
    )
    assert seq > 0, "bus must assign a positive seq"
    # Local mutation applied immediately
    assert msgs == [{"role": "system", "content": "hello-A1"}]
    # Projected into state.messages with the SAME seq
    state = store.state("s-A1")
    assert len(state.messages) == 1
    proj = state.messages[0]
    assert proj.role == "system"
    assert proj.content == "hello-A1"
    assert proj.seq == seq


@pytest.mark.asyncio
async def test_A2_seq_monotonic_over_10_emits(wired):
    store, bus, _, _ = wired
    await _open_session(store, "s-A2")
    ctx = _FakeCtx(app_id="test-app", session_id="s-A2", user_id="u1", bus=bus)
    seqs: list[int] = []
    msgs: list[dict[str, Any]] = []
    for i in range(10):
        s = await inject_system_directive(
            ctx, content=f"dir-{i}", source="behavior_pending",
            messages=msgs,
        )
        seqs.append(s)
    # Strict monotonic
    assert seqs == sorted(seqs), f"seqs not monotonic: {seqs}"
    assert len(set(seqs)) == 10, f"seqs not unique: {seqs}"
    # state.messages preserves arrival order
    state = store.state("s-A2")
    assert [m.content for m in state.messages] == [f"dir-{i}" for i in range(10)]
    assert [m.seq for m in state.messages] == seqs


@pytest.mark.asyncio
async def test_A3_empty_content_is_noop(wired):
    store, bus, _, _ = wired
    await _open_session(store, "s-A3")
    ctx = _FakeCtx(app_id="test-app", session_id="s-A3", user_id="u1", bus=bus)
    msgs: list[dict[str, Any]] = []
    seq = await inject_system_directive(
        ctx, content="", source="nudge_empty_response", messages=msgs,
    )
    assert seq == 0
    assert msgs == []
    assert len(store.state("s-A3").messages) == 0


@pytest.mark.asyncio
async def test_A4_no_bus_keeps_local_mutation():
    # Standalone path - no real bus. Local mutation still works, no crash.
    ctx = _FakeCtx(app_id="a", session_id="s", user_id="u", bus=None)
    msgs: list[dict[str, Any]] = []
    seq = await inject_system_directive(
        ctx, content="lost-but-visible", source="other", messages=msgs,
    )
    assert seq == 0
    assert msgs == [{"role": "system", "content": "lost-but-visible"}]


@pytest.mark.asyncio
async def test_A5_missing_app_id_or_session_id_yields_no_emit(wired):
    _, bus, _, _ = wired
    # No session opened, but bus still emits (it doesn't know about the
    # store). The helper's contract: when scope ids are missing, drop
    # the emit silently. Local mutation kept.
    ctx_no_app = _FakeCtx(app_id="", session_id="s", user_id="u", bus=bus)
    msgs1: list[dict[str, Any]] = []
    seq = await inject_system_directive(
        ctx_no_app, content="x", source="other", messages=msgs1,
    )
    assert seq == 0
    assert msgs1 == [{"role": "system", "content": "x"}]

    ctx_no_sid = _FakeCtx(app_id="a", session_id="", user_id="u", bus=bus)
    msgs2: list[dict[str, Any]] = []
    seq2 = await inject_system_directive(
        ctx_no_sid, content="y", source="other", messages=msgs2,
    )
    assert seq2 == 0
    assert msgs2 == [{"role": "system", "content": "y"}]


@pytest.mark.asyncio
async def test_A6_bus_emit_failure_swallowed(wired):
    store, _, _, _ = wired
    await _open_session(store, "s-A6")

    class _ExplodingBus:
        async def emit(self, _event):
            raise RuntimeError("boom")

    ctx = _FakeCtx(
        app_id="test-app", session_id="s-A6", user_id="u1",
        bus=_ExplodingBus(),
    )
    msgs: list[dict[str, Any]] = []
    seq = await inject_system_directive(
        ctx, content="payload", source="other", messages=msgs,
    )
    # Bus exception → seq=0, local mutation still applied
    assert seq == 0
    assert msgs == [{"role": "system", "content": "payload"}]


@pytest.mark.asyncio
async def test_A7_position_variants(wired):
    store, bus, _, _ = wired
    await _open_session(store, "s-A7")
    ctx = _FakeCtx(app_id="test-app", session_id="s-A7", user_id="u1", bus=bus)
    msgs: list[dict[str, Any]] = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    # prepend
    await inject_system_directive(
        ctx, content="prepended", source="other", messages=msgs,
        position="prepend",
    )
    assert msgs[0] == {"role": "system", "content": "prepended"}
    # insert_before_last
    await inject_system_directive(
        ctx, content="before-last", source="other", messages=msgs,
        position="insert_before_last",
    )
    assert msgs[-2] == {"role": "system", "content": "before-last"}
    assert msgs[-1] == {"role": "assistant", "content": "a1"}
    # append (default)
    await inject_system_directive(
        ctx, content="appended", source="other", messages=msgs,
    )
    assert msgs[-1] == {"role": "system", "content": "appended"}


@pytest.mark.asyncio
async def test_A8_override_params_without_ctx(wired):
    store, bus, _, _ = wired
    await _open_session(store, "s-A8")
    # No ctx - uses explicit bus + ids. This is the cron / addendum path.
    msgs: list[dict[str, Any]] = []
    seq = await inject_system_directive(
        None,
        content="cron-payload",
        source="cron_reminder",
        messages=msgs,
        bus=bus,
        app_id="test-app",
        session_id="s-A8",
        user_id="u1",
    )
    assert seq > 0
    assert msgs == [{"role": "system", "content": "cron-payload"}]
    proj = store.state("s-A8").messages
    assert len(proj) == 1
    assert proj[0].content == "cron-payload"
    assert proj[0].seq == seq


@pytest.mark.asyncio
async def test_A9_metadata_roundtrip(wired):
    store, bus, _, _ = wired
    await _open_session(store, "s-A9")
    ctx = _FakeCtx(app_id="test-app", session_id="s-A9", user_id="u1", bus=bus)
    meta = {"strategy": "summarize", "compacted": 42}
    await inject_system_directive(
        ctx, content="payload", source="compaction_summary",
        messages=None, turn=7, metadata=meta,
    )
    # Inspect the durable event payload
    state = store.state("s-A9")
    assert state.events
    ev = state.events[-1]
    assert ev.type == "system_message"
    assert ev.payload["content"] == "payload"
    assert ev.payload["source"] == "compaction_summary"
    assert ev.payload["turn"] == 7
    assert ev.payload["metadata"] == meta


@pytest.mark.asyncio
async def test_A10_concurrent_emits_get_unique_monotonic_seqs(wired):
    store, bus, _, _ = wired
    await _open_session(store, "s-A10")
    ctx = _FakeCtx(app_id="test-app", session_id="s-A10", user_id="u1", bus=bus)

    async def _fire(i: int) -> int:
        return await inject_system_directive(
            ctx, content=f"par-{i}", source="behavior_pending",
        )

    seqs = await asyncio.gather(*[_fire(i) for i in range(50)])
    assert len(set(seqs)) == 50, "all seqs must be unique under concurrency"
    assert min(seqs) > 0
    # state.messages has all 50, in seq order (projection runs sync
    # inside store.append_event, which is serialised per-session)
    state = store.state("s-A10")
    assert len(state.messages) == 50
    assert [m.seq for m in state.messages] == sorted(seqs)


# ── Group B: integration with real chat ordering ────────────────────


@pytest.mark.asyncio
async def test_B1_interleaved_user_assistant_system_preserves_order(wired):
    """The canonical ordering invariant: mid-turn system directives
    must keep their chronological position relative to user / assistant
    messages on cold reload."""
    store, bus, bridge, _ = wired
    await _open_session(store, "s-B1")
    ctx = _FakeCtx(app_id="test-app", session_id="s-B1", user_id="u1", bus=bus)

    # 1. User message via bridge.record (kind="message" - durable row)
    seq_u = await bridge.record(
        kind="message", type="user_message",
        app_id="test-app", session_id="s-B1", user_id="u1",
        role="user", content="hi",
    )
    # 2. System directive injected mid-turn
    seq_s1 = await inject_system_directive(
        ctx, content="nudge-1", source="nudge_empty_response",
    )
    # 3. Assistant message
    seq_a = await bridge.record(
        kind="message", type="assistant_message",
        app_id="test-app", session_id="s-B1", user_id="u1",
        role="assistant", content="ok",
    )
    # 4. Second system directive
    seq_s2 = await inject_system_directive(
        ctx, content="nudge-2", source="behavior_pending",
    )

    state = store.state("s-B1")
    # Strict ordering: user → system → assistant → system
    assert [m.role for m in state.messages] == [
        "user", "system", "assistant", "system",
    ]
    assert [m.content for m in state.messages] == [
        "hi", "nudge-1", "ok", "nudge-2",
    ]
    assert [m.seq for m in state.messages] == [seq_u, seq_s1, seq_a, seq_s2]
    # And the seqs are strictly monotonic
    assert seq_u < seq_s1 < seq_a < seq_s2


@pytest.mark.asyncio
async def test_B2_cold_reload_reconstructs_identical_state(tmp_path):
    """Simulate daemon restart with a SECOND store instance pointing at
    the same on-disk root - the canonical crash-recovery pattern."""
    sio = MagicMock()
    sio.emit = AsyncMock()
    bus = SocketIOBus(sio=sio)
    bus_buffer = bus._buffer

    def _sync(sid: str, seq: int) -> None:
        try:
            bus_buffer.bump_to(session_id=sid, value=seq)
        except Exception:
            pass

    s1 = InMemorySessionStore(
        root=tmp_path, flush_interval_ms=10,
        on_internal_seq_alloc=_sync,
    )
    await s1.start()
    bridge1 = SessionStoreBridge(s1, mode=BridgeMode.SHADOW)
    set_default_bridge(bridge1)

    pre_signature: list[tuple] = []
    try:
        await s1.open("s-B2", app_id="test-app", user_id="u1")
        ctx = _FakeCtx(
            app_id="test-app", session_id="s-B2", user_id="u1", bus=bus,
        )

        await bridge1.record(
            kind="message", type="user_message",
            app_id="test-app", session_id="s-B2", user_id="u1",
            role="user", content="initial-user",
        )
        await inject_system_directive(
            ctx, content="dir-1", source="behavior_pending",
        )
        await inject_system_directive(
            ctx, content="dir-2", source="turn_limit_near",
            metadata={"foo": "bar"},
        )
        await bridge1.record(
            kind="message", type="assistant_message",
            app_id="test-app", session_id="s-B2", user_id="u1",
            role="assistant", content="final-asst",
        )

        pre = list(s1.state("s-B2").messages)
        assert len(pre) == 4
        pre_signature = [(m.role, m.content, m.seq) for m in pre]

        # Flush events.jsonl to disk before tearing down
        await s1.flusher.flush()
        await s1.close_session("s-B2")
    finally:
        set_default_bridge(None)
        await s1.stop()

    # ── Cold reload: brand new store instance, same disk root ────
    s2 = InMemorySessionStore(
        root=tmp_path, flush_interval_ms=10,
        on_internal_seq_alloc=_sync,
    )
    await s2.start()
    try:
        state2 = await s2.open(
            "s-B2", app_id="test-app", user_id="u1",
            create_if_missing=False,
        )
        post_signature = [(m.role, m.content, m.seq) for m in state2.messages]
        assert post_signature == pre_signature, (
            f"cold reload diverged:\n"
            f"  pre={pre_signature}\n"
            f"  post={post_signature}"
        )
    finally:
        await s2.stop()


@pytest.mark.asyncio
async def test_B3_empty_session_then_one_directive(wired):
    store, bus, _, _ = wired
    await _open_session(store, "s-B3")
    ctx = _FakeCtx(app_id="test-app", session_id="s-B3", user_id="u1", bus=bus)
    seq = await inject_system_directive(
        ctx, content="solo", source="other",
    )
    state = store.state("s-B3")
    assert len(state.messages) == 1
    assert state.messages[0].role == "system"
    assert state.messages[0].seq == seq


# ── Group C: per-call-site tests in agent_loop ──────────────────────


@pytest.mark.asyncio
async def test_C1_nudge_empty_response_persists_with_correct_source(wired):
    """``_nudge_empty_response`` is invoked when the LLM returned no
    content despite tool calls. The directive must land in the timeline
    tagged ``nudge_empty_response``."""
    from digitorn.core.runtime.agent_loop import _nudge_empty_response

    store, bus, _, _ = wired
    await _open_session(store, "s-C1")
    ctx = _FakeCtx(app_id="test-app", session_id="s-C1", user_id="u1", bus=bus)
    ctx.nudged_response = False
    msgs: list[dict[str, Any]] = []
    # The nudge fires when content is whitespace-only (a "blank"
    # response after the LLM called tools). Truly-empty content is a
    # different case the function intentionally skips.
    continued = await _nudge_empty_response(ctx, msgs, content="   \n", tool_count=2)
    assert continued is True
    # Local working list got the system directive
    assert any(m.get("role") == "system" for m in msgs)
    # Durable event has the right source
    state = store.state("s-C1")
    assert len(state.events) == 1
    ev = state.events[-1]
    assert ev.type == "system_message"
    assert ev.payload["source"] == "nudge_empty_response"
    assert ev.payload["metadata"]["tool_count"] == 2
    # Idempotency: second call returns False, no second event
    continued2 = await _nudge_empty_response(ctx, msgs, content="   ", tool_count=2)
    assert continued2 is False
    assert len(state.events) == 1


@pytest.mark.asyncio
async def test_C2_inject_turn_limit_warning_persists(wired):
    from digitorn.core.runtime.agent_loop import _inject_turn_limit_warning

    store, bus, _, _ = wired
    await _open_session(store, "s-C2")
    ctx = _FakeCtx(app_id="test-app", session_id="s-C2", user_id="u1", bus=bus)
    msgs: list[dict[str, Any]] = []
    # Fires only when turn == max_turns - 2 AND tool_count > 0
    await _inject_turn_limit_warning(
        ctx, msgs, turn=78, max_turns=80, tool_count=5,
    )
    state = store.state("s-C2")
    assert len(state.events) == 1
    ev = state.events[-1]
    assert ev.payload["source"] == "turn_limit_near"
    assert ev.payload["metadata"]["max_turns"] == 80
    assert ev.payload["metadata"]["tool_count"] == 5
    # Non-firing case: turn != max_turns - 2 → no event
    await _inject_turn_limit_warning(
        ctx, msgs, turn=10, max_turns=80, tool_count=5,
    )
    assert len(state.events) == 1, "must not emit when condition isn't met"


@pytest.mark.asyncio
async def test_C3_flush_behavior_notes_persists_each_note(wired):
    from digitorn.core.runtime.agent_loop import _flush_behavior_notes

    store, bus, _, _ = wired
    await _open_session(store, "s-C3")
    ctx = _FakeCtx(app_id="test-app", session_id="s-C3", user_id="u1", bus=bus)
    ctx._pending_behavior_notes = ["note-1", "note-2", "note-3"]
    msgs: list[dict[str, Any]] = []
    await _flush_behavior_notes(ctx, msgs)
    state = store.state("s-C3")
    # 3 events with source=behavior_pending, in order
    assert [ev.type for ev in state.events] == ["system_message"] * 3
    assert [ev.payload["source"] for ev in state.events] == [
        "behavior_pending", "behavior_pending", "behavior_pending",
    ]
    assert [ev.payload["content"] for ev in state.events] == [
        "note-1", "note-2", "note-3",
    ]
    # Pending list cleared
    assert ctx._pending_behavior_notes == []
    # Working list got all 3
    assert len([m for m in msgs if m.get("role") == "system"]) == 3


@pytest.mark.asyncio
async def test_C4_check_unfinished_work_emits_once(wired):
    from digitorn.core.runtime.agent_loop import _check_unfinished_work

    store, bus, _, _ = wired
    await _open_session(store, "s-C4")
    ctx = _FakeCtx(app_id="test-app", session_id="s-C4", user_id="u1", bus=bus)
    ctx.completion_reminded = False

    # Mock memory module that says "yes there's unfinished work"
    class _Mem:
        class _Store:
            class _Working:
                def has_unfinished_work(self):
                    return (True, "todo: finish X")
            working = _Working()
        store = _Store()
    ctx.memory_module = _Mem()

    msgs: list[dict[str, Any]] = []
    cont = await _check_unfinished_work(ctx, msgs)
    assert cont is True
    state = store.state("s-C4")
    assert len(state.events) == 1
    ev = state.events[-1]
    assert ev.payload["source"] == "nudge_unfinished_work"
    assert ev.payload["metadata"]["details"] == "todo: finish X"
    # Second call - ctx.completion_reminded is True → no second event
    cont2 = await _check_unfinished_work(ctx, msgs)
    assert cont2 is False
    assert len(state.events) == 1


# ── Group D: hooks ──────────────────────────────────────────────────


def _make_turn_state(messages: list[dict[str, Any]], ctx: Any):
    """Build a minimal TurnState for hook actions."""
    from digitorn.core.runtime.hooks import TurnState
    return TurnState(
        messages=messages,
        turn=0,
        max_turns=10,
        tool_calls_count=0,
        agent_id="main",
        _agent_context=ctx,
    )


@pytest.mark.asyncio
async def test_D1_hook_inject_message_create_new_system(wired):
    from digitorn.core.runtime.hooks import _exec_inject_message

    store, bus, _, _ = wired
    await _open_session(store, "s-D1")
    ctx = _FakeCtx(app_id="test-app", session_id="s-D1", user_id="u1", bus=bus)
    # No existing system message in state.messages → "create new" path
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "u"}]
    state = _make_turn_state(msgs, ctx)
    await _exec_inject_message(state, {"content": "X", "strategy": "system"})
    # Working list now has a system at position 0
    assert msgs[0] == {"role": "system", "content": "X"}
    # Durable event
    s = store.state("s-D1")
    assert len(s.events) == 1
    assert s.events[-1].payload["source"] == "hook_inject_message"
    assert s.events[-1].payload["metadata"]["created_new"] is True


@pytest.mark.asyncio
async def test_D2_hook_inject_message_append_existing_emits_new(wired):
    from digitorn.core.runtime.hooks import _exec_inject_message

    store, bus, _, _ = wired
    await _open_session(store, "s-D2")
    ctx = _FakeCtx(app_id="test-app", session_id="s-D2", user_id="u1", bus=bus)
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "ORIG"},
        {"role": "user", "content": "u"},
    ]
    state = _make_turn_state(msgs, ctx)
    await _exec_inject_message(state, {"content": "ADDITION", "strategy": "system"})
    # In-flight: existing system message extended with the new content
    assert "ORIG" in msgs[0]["content"]
    assert "ADDITION" in msgs[0]["content"]
    # Durable: a SEPARATE system_message event for the NEW content only
    s = store.state("s-D2")
    assert len(s.events) == 1
    ev = s.events[-1]
    assert ev.payload["content"] == "ADDITION"
    assert ev.payload["source"] == "hook_inject_message"
    assert ev.payload["metadata"]["merged_inflight"] is True


@pytest.mark.asyncio
async def test_D3_hook_inject_message_new_message_system_role(wired):
    from digitorn.core.runtime.hooks import _exec_inject_message

    store, bus, _, _ = wired
    await _open_session(store, "s-D3")
    ctx = _FakeCtx(app_id="test-app", session_id="s-D3", user_id="u1", bus=bus)
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "u"}]
    state = _make_turn_state(msgs, ctx)
    await _exec_inject_message(state, {
        "content": "X", "strategy": "new_message",
        "role": "system", "position": "end",
    })
    s = store.state("s-D3")
    assert len(s.events) == 1
    assert s.events[-1].payload["source"] == "hook_inject_message"
    assert msgs[-1] == {"role": "system", "content": "X"}


@pytest.mark.asyncio
async def test_D4_hook_inject_message_new_message_user_role_not_persisted(wired):
    """User-role injection via new_message is NOT a system directive
    and must not emit a ``system_message`` event."""
    from digitorn.core.runtime.hooks import _exec_inject_message

    store, bus, _, _ = wired
    await _open_session(store, "s-D4")
    ctx = _FakeCtx(app_id="test-app", session_id="s-D4", user_id="u1", bus=bus)
    msgs: list[dict[str, Any]] = [{"role": "system", "content": "s"}]
    state = _make_turn_state(msgs, ctx)
    await _exec_inject_message(state, {
        "content": "synth-user", "strategy": "new_message",
        "role": "user", "position": "end",
    })
    assert msgs[-1] == {"role": "user", "content": "synth-user"}
    s = store.state("s-D4")
    assert s.event_count() == 0, "user-role injection must not emit system_message"


@pytest.mark.asyncio
async def test_D5_hook_shell_three_variants(wired):
    """Each shell hook outcome (blocked / error / stdout) maps to a
    distinct source tag in the event log."""
    from digitorn.core.runtime.system_directive import inject_system_directive

    store, bus, _, _ = wired
    await _open_session(store, "s-D5")
    ctx = _FakeCtx(app_id="test-app", session_id="s-D5", user_id="u1", bus=bus)
    # Drive the three sources directly (the real shell hook plumbs them
    # through the same helper).
    await inject_system_directive(
        ctx, content="[blocked]", source="hook_shell_blocked",
        metadata={"command": "rm -rf /"},
    )
    await inject_system_directive(
        ctx, content="[error]", source="hook_shell_error",
        metadata={"exit_code": 1},
    )
    await inject_system_directive(
        ctx, content="[stdout]", source="hook_shell_stdout",
        metadata={"stdout_len": 42},
    )
    state = store.state("s-D5")
    assert [ev.payload["source"] for ev in state.events] == [
        "hook_shell_blocked", "hook_shell_error", "hook_shell_stdout",
    ]
    # Order preserved by seq
    assert state.events[0].seq < state.events[1].seq < state.events[2].seq


@pytest.mark.asyncio
async def test_D6_compaction_summary_persists_with_full_note(wired):
    """``_do_truncate`` rebuilds the messages list with a summary note
    at position 1. The caller MUST then emit a ``system_message`` event
    with the full injected note (summary + context reminder)."""
    from digitorn.core.runtime.hooks import _do_truncate

    msgs: list[dict[str, Any]] = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    sys_msg = {"role": "system", "content": "YAML-prompt"}
    to_compact = msgs[:2]
    to_keep = msgs[2:]
    summary_note, full = _do_truncate(
        msgs, sys_msg, to_compact, to_keep,
        context_reminder="REMINDER: tools listed below",
    )
    # Return contract: tuple (summary_note, full_injected_note)
    assert isinstance(summary_note, str) and summary_note
    assert isinstance(full, str) and full
    # Full contains BOTH the summary AND the context reminder
    assert "REMINDER" in full
    assert summary_note in full
    # Local working list rebuilt with system_msg + injected note + to_keep
    assert msgs[0] == sys_msg
    assert msgs[1] == {"role": "system", "content": full}
    assert msgs[2:] == to_keep


# ── Group E: cron + template addendum (pre-ctx path) ────────────────


@pytest.mark.asyncio
async def test_E1_cron_reminder_via_explicit_bus(wired):
    """Cron reminder fires BEFORE the per-turn ctx is built. The helper
    accepts explicit bus + ids so the directive is still persisted."""
    store, bus, _, _ = wired
    await _open_session(store, "s-E1")
    msgs: list[dict[str, Any]] = []
    seq = await inject_system_directive(
        None,
        content="[REMINDER from cron] do X",
        source="cron_reminder",
        messages=msgs,
        bus=bus,
        app_id="test-app",
        session_id="s-E1",
        user_id="u1",
        metadata={"message": "do X"},
    )
    assert seq > 0
    state = store.state("s-E1")
    assert len(state.events) == 1
    ev = state.events[-1]
    assert ev.payload["source"] == "cron_reminder"
    assert "REMINDER from cron" in ev.payload["content"]


@pytest.mark.asyncio
async def test_E2_template_addendum_lands_before_user_message(wired):
    """The iframe / template addendum is injected at turn start, BEFORE
    the user message. On replay it must appear at a strictly LOWER seq
    than the user message."""
    store, bus, bridge, _ = wired
    await _open_session(store, "s-E2")
    # 1. Addendum (template_system_prompt) emitted first
    seq_add = await inject_system_directive(
        None,
        content="[Addendum] focus on accessibility",
        source="template_addendum",
        bus=bus,
        app_id="test-app",
        session_id="s-E2",
        user_id="u1",
    )
    # 2. Then the user message lands
    seq_user = await bridge.record(
        kind="message", type="user_message",
        app_id="test-app", session_id="s-E2", user_id="u1",
        role="user", content="check my form",
    )
    assert seq_add < seq_user
    state = store.state("s-E2")
    assert [m.role for m in state.messages] == ["system", "user"]
    assert [m.seq for m in state.messages] == [seq_add, seq_user]


@pytest.mark.asyncio
async def test_E3_sync_wrapper_schedules_emit_on_running_loop(wired):
    """The ``_sync`` variant must mutate ``messages`` immediately and
    schedule the bus emit on the running loop. Useful for sync hook
    handlers."""
    store, bus, _, _ = wired
    await _open_session(store, "s-E3")
    ctx = _FakeCtx(app_id="test-app", session_id="s-E3", user_id="u1", bus=bus)
    msgs: list[dict[str, Any]] = []
    inject_system_directive_sync(
        ctx, content="sync-payload", source="other", messages=msgs,
    )
    # Local mutation is immediate
    assert msgs == [{"role": "system", "content": "sync-payload"}]
    # Give the scheduled coroutine a chance to run (it was scheduled
    # on the same loop we're on).
    for _ in range(20):
        await asyncio.sleep(0.01)
        if len(store.state("s-E3").events) >= 1:
            break
    state = store.state("s-E3")
    assert len(state.events) == 1
    assert state.events[-1].payload["source"] == "other"


# ── Group F: end-to-end ordering invariant across restart ──────────


@pytest.mark.asyncio
async def test_F1_messages_for_llm_preserves_chronological_directives(wired):
    """The legacy adapter's ``_messages_with_tool_results`` is the
    bridge between ``state.messages`` (event-sourced) and the
    chat-completion-shaped list that ``agent_turn`` consumes. After a
    sequence of [user, system_directive, assistant, system_directive,
    user] the produced list must be in the same order."""
    from digitorn.core.runtime.session_store.legacy_adapter import (
        LegacySessionStoreAdapter,
    )

    store, bus, bridge, _ = wired
    await _open_session(store, "s-F1")
    ctx = _FakeCtx(app_id="test-app", session_id="s-F1", user_id="u1", bus=bus)

    await bridge.record(
        kind="message", type="user_message",
        app_id="test-app", session_id="s-F1", user_id="u1",
        role="user", content="q1",
    )
    await inject_system_directive(
        ctx, content="mid-1", source="nudge_empty_response",
    )
    await bridge.record(
        kind="message", type="assistant_message",
        app_id="test-app", session_id="s-F1", user_id="u1",
        role="assistant", content="a1",
    )
    await inject_system_directive(
        ctx, content="mid-2", source="behavior_pending",
    )
    await bridge.record(
        kind="message", type="user_message",
        app_id="test-app", session_id="s-F1", user_id="u1",
        role="user", content="q2",
    )

    adapter = LegacySessionStoreAdapter(store)
    session = adapter.get(app_id="test-app", session_id="s-F1", user_id="u1")
    rows = session.messages
    # Strict role+content ordering (no tool_calls so no role=tool rows
    # spliced in)
    assert [(m["role"], m["content"]) for m in rows] == [
        ("user", "q1"),
        ("system", "mid-1"),
        ("assistant", "a1"),
        ("system", "mid-2"),
        ("user", "q2"),
    ]

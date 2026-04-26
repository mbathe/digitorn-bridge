"""Dedup contract — the approval_request fanout MUST ship the same
``event_id`` on both rebroadcasts while giving each a distinct ``seq``.

Client side, this is the only way to dedup one logical approval that
hits both the session-scoped room and the user-scoped room. Pinning
this invariant in a test is the cheapest insurance against a future
refactor that accidentally re-generates the event_id.
"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))


async def _run() -> int:
    from digitorn.core.events.session_bus import SocketIOBus
    from digitorn.core.events.envelope import SessionEvent, OpType, OpState

    sio = MagicMock()
    sio.emit = AsyncMock()
    bus = SocketIOBus(sio=sio)

    # approval_request triggers the session → user fanout.
    ev = SessionEvent.build(
        type="approval_request",
        app_id="app-x", session_id="sid-1", user_id="uA",
        op_id="req-1", op_type=OpType.APPROVAL,
        op_state=OpState.WAITING_APPROVAL,
    )
    await bus.emit(ev)

    calls = sio.emit.call_args_list
    envelopes = [c.args[1] for c in calls]

    failures: list[str] = []
    if len(envelopes) != 2:
        failures.append(f"expected 2 rebroadcasts, got {len(envelopes)}")
    else:
        eids = {e["event_id"] for e in envelopes}
        if len(eids) != 1:
            failures.append(
                f"event_id must be stable across fanout, got {eids!r}"
            )
        seqs = [e["seq"] for e in envelopes]
        if len(set(seqs)) != 2:
            failures.append(
                f"seq must differ per rebroadcast, got {seqs!r}"
            )
        if seqs != sorted(seqs):
            failures.append(
                "per-user seq must stay monotonic across rooms, "
                f"got {seqs!r}"
            )

    # A non-fanout event (plain session-scoped) must still produce
    # exactly ONE emit.
    sio.emit.reset_mock()
    tool_ev = SessionEvent.build(
        type="tool_start",
        app_id="app-x", session_id="sid-1", user_id="uA",
        op_id="op-tool-1", op_type=OpType.TOOL,
        op_state=OpState.RUNNING,
    )
    await bus.emit(tool_ev)
    if len(sio.emit.call_args_list) != 1:
        failures.append(
            f"tool_start should not fanout, got {len(sio.emit.call_args_list)}"
        )

    if failures:
        print("FAIL — fanout event_id contract:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — approval_request fanout keeps event_id stable, "
          "seq monotonic per user, tool_start does not fanout")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))

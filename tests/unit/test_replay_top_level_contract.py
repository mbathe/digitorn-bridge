"""Contract parity between LIVE emission and REPLAY reconstruction.

Live events (Socket.IO on emit) put ``event_id / op_id / op_type /
op_state / op_parent_id / app_id / user_id / correlation_id`` at the
top level of the envelope. The replay path reads ``session_events``
(a JSON column) and must reconstruct the SAME top-level shape — not
leave those fields stuck in ``payload``.

This test mocks the DB row and runs ``async_replay`` to verify the
reconstruction. A scout against the real daemon (
``scout/explore_events.py``) covers the wire-level assertion.
"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))


async def _run() -> int:
    from digitorn.core.events.session_bus import SocketIOBus

    # Build a fake row that looks like what ``_persist_event`` writes:
    # contract fields live INSIDE ``payload`` JSON column, only the
    # indexed columns (seq, type, kind, app_id, session_id, user_id,
    # correlation_id, ts) are first-class.
    fake_row = SimpleNamespace(
        type="tool_start",
        kind="session",
        seq=42,
        app_id="app-x",
        session_id="sid-1",
        user_id="uA",
        correlation_id="fp-turn-abc",
        ts=datetime.now(timezone.utc),
        payload={
            "event_id": "ev-abcdef123456",
            "op_id": "toolu_xyz",
            "op_type": "tool",
            "op_state": "running",
            "op_parent_id": None,
            "tool_name": "Read",
            "args": {"path": "a.py"},
        },
    )

    class _FakeScalars:
        def __init__(self, rows): self._rows = rows
        def all(self): return self._rows

    class _FakeResult:
        def __init__(self, rows): self._s = _FakeScalars(rows)
        def scalars(self): return self._s

    fake_db = MagicMock()
    fake_db.execute = AsyncMock(return_value=_FakeResult([fake_row]))

    class _Ctx:
        async def __aenter__(self_): return fake_db
        async def __aexit__(self_, *a): return None

    def _fake_sf(): return _Ctx()

    with patch(
        "digitorn.core.database.get_session_factory",
        return_value=_fake_sf,
    ):
        sio = MagicMock(); sio.emit = AsyncMock()
        bus = SocketIOBus(sio=sio)
        events = await bus.async_replay(
            user_id="uA", since_seq=0, session_id="sid-1",
        )

    failures: list[str] = []
    if len(events) != 1:
        failures.append(f"expected 1 event replayed, got {len(events)}")
    else:
        e = events[0]
        for field, expected in [
            ("event_id", "ev-abcdef123456"),
            ("op_id", "toolu_xyz"),
            ("op_type", "tool"),
            ("op_state", "running"),
            ("app_id", "app-x"),
            ("session_id", "sid-1"),
            ("user_id", "uA"),
            ("correlation_id", "fp-turn-abc"),
            ("type", "tool_start"),
            ("kind", "session"),
            ("seq", 42),
        ]:
            got = e.get(field)
            if got != expected:
                failures.append(
                    f"top-level {field!r}: expected {expected!r} got {got!r}",
                )
        # ts must be ISO-8601 string.
        if not isinstance(e.get("ts"), str):
            failures.append(f"ts should be iso-string, got {type(e.get('ts'))}")
        # payload must keep user fields intact.
        p = e.get("payload") or {}
        if p.get("tool_name") != "Read":
            failures.append(
                f"payload should keep tool_name, got {p.get('tool_name')!r}",
            )

    if failures:
        print("FAIL — replay top-level contract parity:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — replay reconstructs event_id/op_id/op_type/op_state/"
          "op_parent_id/app_id/user_id/correlation_id at top level")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))

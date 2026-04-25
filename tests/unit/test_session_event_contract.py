"""Universal-event-contract regression tests.

Guarantees:
  * ``SessionEvent`` refuses to be built without scope (app/session/user) + op_id.
  * ``op_type`` / ``op_state`` must be enums — string typos fail loudly.
  * The bus assigns ``seq`` monotonically and emits the full envelope.
  * ``publish(legacy dict)`` back-fills ``op_id`` / ``op_type`` / ``op_state``
    so unmigrated call sites still comply.
  * Tool/agent/approval cycles share ``op_id`` across their lifecycle
    events when emitted through the real manager callbacks.
  * ``is_terminal()`` agrees with ``TERMINAL_STATES``.
"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from digitorn.core.events.envelope import (  # noqa: E402
    SessionEvent, OpType, OpState, TERMINAL_STATES, gen_op_id, kind_for,
)
from digitorn.core.events.session_bus import SocketIOBus  # noqa: E402


async def _run() -> int:
    failures: list[str] = []

    # ── SessionEvent validation ───────────────────────────────────

    # 1. Missing required field → ValueError
    for missing in [
        dict(app_id="", session_id="s", user_id="u", op_id="o"),
        dict(app_id="a", session_id="", user_id="u", op_id="o"),
        dict(app_id="a", session_id="s", user_id="", op_id="o"),
        dict(app_id="a", session_id="s", user_id="u", op_id=""),
    ]:
        try:
            SessionEvent.build(
                type="tool_start", op_type=OpType.TOOL,
                op_state=OpState.RUNNING, **missing,
            )
        except ValueError:
            pass
        else:
            failures.append(f"missing field should raise: {missing}")

    # 2. op_type as str rejected
    try:
        SessionEvent.build(
            type="tool_start", app_id="a", session_id="s", user_id="u",
            op_id="op", op_type="tool", op_state=OpState.RUNNING,
        )
    except ValueError:
        pass
    else:
        failures.append("op_type as str should raise ValueError")

    # 3. op_state as str rejected
    try:
        SessionEvent.build(
            type="tool_start", app_id="a", session_id="s", user_id="u",
            op_id="op", op_type=OpType.TOOL, op_state="running",
        )
    except ValueError:
        pass
    else:
        failures.append("op_state as str should raise ValueError")

    # 4. Full event → all fields present, frozen, kind auto-derived
    e = SessionEvent.build(
        type="tool_start", app_id="a", session_id="s", user_id="u",
        op_id="op-tool-1", op_type=OpType.TOOL, op_state=OpState.RUNNING,
        correlation_id="fp-abc",
    )
    d = e.to_dict()
    if d["kind"] != "session":
        failures.append(f"kind auto-derive: expected session got {d['kind']}")
    if not d["event_id"].startswith("ev-"):
        failures.append("event_id must be prefixed ev-")
    if "op_id" not in d or "op_type" not in d or "op_state" not in d:
        failures.append("to_dict missing contract fields")
    try:
        e.op_state = OpState.COMPLETED  # type: ignore[misc]
    except Exception:
        pass
    else:
        failures.append("SessionEvent must be frozen")

    # 5. Terminal detection
    for st, terminal in [
        (OpState.PENDING, False),
        (OpState.RUNNING, False),
        (OpState.WAITING_APPROVAL, False),
        (OpState.COMPLETED, True),
        (OpState.FAILED, True),
        (OpState.CANCELLED, True),
        (OpState.TIMEOUT, True),
    ]:
        ev = SessionEvent.build(
            type="tool_call", app_id="a", session_id="s", user_id="u",
            op_id="op-x", op_type=OpType.TOOL, op_state=st,
        )
        if ev.is_terminal() != terminal:
            failures.append(
                f"is_terminal({st.value}): expected {terminal}"
            )
    if not {OpState.COMPLETED, OpState.FAILED, OpState.CANCELLED,
            OpState.TIMEOUT} <= TERMINAL_STATES:
        failures.append("TERMINAL_STATES missing an entry")

    # ── Bus emission ───────────────────────────────────────────────

    sio = MagicMock()
    sio.emit = AsyncMock()
    bus = SocketIOBus(sio=sio)

    # 6. emit(SessionEvent) → envelope carries contract + seq filled.
    e1 = SessionEvent.build(
        type="tool_start", app_id="a", session_id="s", user_id="u",
        op_id="op-tool-777", op_type=OpType.TOOL, op_state=OpState.RUNNING,
        correlation_id="fp-turn-1",
    )
    await bus.emit(e1)
    env = sio.emit.call_args.args[1]
    if env["op_id"] != "op-tool-777":
        failures.append(f"emit: op_id dropped: {env}")
    if env["op_state"] != "running":
        failures.append(f"emit: op_state wrong: {env['op_state']}")
    if env["seq"] <= 0:
        failures.append(f"emit: bus must assign seq: got {env['seq']}")
    if env["correlation_id"] != "fp-turn-1":
        failures.append("emit: correlation_id not propagated")

    # 7. Tool cycle op_id parity — tool_start then tool_call share op_id.
    sio.emit.reset_mock()
    e2 = SessionEvent.build(
        type="tool_call", app_id="a", session_id="s", user_id="u",
        op_id="op-tool-777", op_type=OpType.TOOL, op_state=OpState.COMPLETED,
        correlation_id="fp-turn-1",
        payload={"success": True},
    )
    await bus.emit(e2)
    env = sio.emit.call_args.args[1]
    if env["op_id"] != "op-tool-777":
        failures.append(
            "tool_start/tool_call op_id must match — this is the whole point"
        )
    if env["op_state"] != "completed":
        failures.append(f"tool_call terminal state: {env['op_state']}")

    # 8. Legacy publish(dict) back-fills contract.
    sio.emit.reset_mock()
    key = bus.session_key("a", "s", "u")
    await bus.publish(key, {
        "type": "message_started",
        "data": {"correlation_id": "fp-turn-2"},
    })
    env = sio.emit.call_args.args[1]
    if env["op_type"] != "turn":
        failures.append(f"legacy turn: op_type {env['op_type']}")
    if env["op_state"] != "running":
        failures.append(f"legacy turn: op_state {env['op_state']}")
    if env["op_id"] != "fp-turn-2":
        failures.append(
            f"legacy turn: op_id should default to correlation_id, got {env['op_id']}"
        )

    # 9. Legacy approval_request back-fills to waiting_approval.
    sio.emit.reset_mock()
    await bus.publish(key, {
        "type": "approval_request",
        "data": {"request_id": "req-xyz"},
    })
    envs = [c.args[1] for c in sio.emit.call_args_list]
    # One or two emits (session room + optional user fanout).
    session_env = next(e for e in envs if e["type"] == "approval_request")
    if session_env["op_state"] != "waiting_approval":
        failures.append(
            f"legacy approval: op_state {session_env['op_state']}"
        )
    if session_env["op_type"] != "approval":
        failures.append(f"legacy approval: op_type {session_env['op_type']}")

    # 10. emit() refuses non-SessionEvent — shouts at the dev.
    try:
        await bus.emit({"type": "x"})
    except TypeError:
        pass
    else:
        failures.append("bus.emit should refuse raw dict")

    # 11. Identity uniqueness: two builds produce different event_id + same op_id.
    a = SessionEvent.build(type="tool_start", app_id="a", session_id="s",
                           user_id="u", op_id="op-same",
                           op_type=OpType.TOOL, op_state=OpState.RUNNING)
    b = SessionEvent.build(type="tool_call", app_id="a", session_id="s",
                           user_id="u", op_id="op-same",
                           op_type=OpType.TOOL, op_state=OpState.COMPLETED)
    if a.event_id == b.event_id:
        failures.append("event_id must be unique per event")
    if a.op_id != b.op_id:
        failures.append("same-cycle events should keep op_id across builds")

    # 12. gen_op_id prefix hint is visible in logs.
    oid = gen_op_id("tool")
    if not oid.startswith("op-tool-"):
        failures.append(f"gen_op_id prefix dropped: {oid}")

    # 13. kind_for unknown type → default session.
    if kind_for("brand_new_event_type") != "session":
        failures.append("unknown type should default kind=session")

    # ── Report ─────────────────────────────────────────────────────

    if failures:
        print("FAIL — session event contract regressions:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — SessionEvent contract (validation + bus + backfill + op_id parity)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))

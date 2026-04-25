"""Reconstruction test: prove that ``/active-ops`` correctly
reconstructs the state of in-flight operations from persisted events.

The whole point of the universal envelope is that a client who
reconnects CAN deterministically know which ops are still running.
This test simulates:
  1. Server emits ``tool_start`` (op running)
  2. Server emits ``tool_call`` (op completed) for tool-A
  3. Server emits ``tool_start`` for tool-B (still running — no terminal event)
  4. Client queries ``/active-ops`` → must see tool-B ONLY
"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from digitorn.core.events.envelope import (  # noqa: E402
    SessionEvent, OpType, OpState, TERMINAL_STATES,
)


def _reconstruct_active_ops(persisted_events: list[dict]) -> list[dict]:
    """Port of the logic in ``/active-ops`` so the test doesn't need
    a live daemon. Groups events by op_id, keeps the latest state,
    filters out terminal ones."""
    terminal_names = {s.value for s in TERMINAL_STATES}
    ops: dict[str, dict] = {}
    for ev in persisted_events:
        payload = ev.get("payload") or {}
        op_id = payload.get("op_id") or ev.get("correlation_id")
        if not op_id:
            continue
        op_type = payload.get("op_type")
        op_state = payload.get("op_state")
        entry = ops.setdefault(op_id, {
            "op_id": op_id,
            "op_type": op_type,
            "op_state": op_state,
            "first_seq": ev["seq"],
        })
        entry["op_state"] = op_state
        entry["last_seq"] = ev["seq"]
        entry["last_type"] = ev["type"]
    return [e for e in ops.values() if e["op_state"] not in terminal_names]


def _to_persisted(event: SessionEvent) -> dict:
    """Mimic what the DB row → API row conversion looks like."""
    d = event.to_dict()
    return {
        "type": d["type"],
        "seq": d["seq"],
        "ts": d["ts"],
        "correlation_id": d["correlation_id"],
        "payload": d["payload"],
    }


async def _run() -> int:
    failures: list[str] = []

    persisted: list[dict] = []
    SEQ = {"n": 0}

    def _emit(ev: SessionEvent) -> None:
        SEQ["n"] += 1
        persisted.append(_to_persisted(ev.with_seq(SEQ["n"])))

    # ── Scenario 1: two tool calls, one still running ──────────────
    _emit(SessionEvent.build(
        type="tool_start", app_id="a", session_id="s", user_id="u",
        op_id="op-tool-A", op_type=OpType.TOOL, op_state=OpState.RUNNING,
        correlation_id="fp-turn-1",
        payload={"name": "Read"},
    ))
    _emit(SessionEvent.build(
        type="tool_call", app_id="a", session_id="s", user_id="u",
        op_id="op-tool-A", op_type=OpType.TOOL, op_state=OpState.COMPLETED,
        correlation_id="fp-turn-1",
    ))
    _emit(SessionEvent.build(
        type="tool_start", app_id="a", session_id="s", user_id="u",
        op_id="op-tool-B", op_type=OpType.TOOL, op_state=OpState.RUNNING,
        correlation_id="fp-turn-1",
        payload={"name": "Bash"},
    ))
    # Deliberately NO tool_call for B — simulates a disconnect mid-run.

    active = _reconstruct_active_ops(persisted)
    if len(active) != 1:
        failures.append(
            f"expected 1 active op, got {len(active)}: {active}",
        )
    elif active[0]["op_id"] != "op-tool-B":
        failures.append(f"wrong op surfaced: {active[0]}")
    elif active[0]["op_state"] != "running":
        failures.append(f"op_state wrong: {active[0]['op_state']}")

    # ── Scenario 2: sub-agent spawn → progress → result ────────────
    SEQ["n"] = 0
    persisted = []
    _emit(SessionEvent.build(
        type="agent_event", app_id="a", session_id="s", user_id="u",
        op_id="op-agent-42", op_type=OpType.AGENT, op_state=OpState.RUNNING,
        payload={"action": "spawn_agent", "agent_id": "op-agent-42"},
    ))
    _emit(SessionEvent.build(
        type="agent_event", app_id="a", session_id="s", user_id="u",
        op_id="op-agent-42", op_type=OpType.AGENT, op_state=OpState.RUNNING,
        payload={"action": "agent_progress", "tool_calls_count": 3},
    ))
    # No agent_result — agent still running.

    active = _reconstruct_active_ops(persisted)
    if len(active) != 1 or active[0]["op_id"] != "op-agent-42":
        failures.append(f"agent scenario: expected op-agent-42 alive, got {active}")

    # Now emit terminal result — the op should disappear from active.
    _emit(SessionEvent.build(
        type="agent_event", app_id="a", session_id="s", user_id="u",
        op_id="op-agent-42", op_type=OpType.AGENT, op_state=OpState.COMPLETED,
        payload={"action": "agent_result"},
    ))
    active = _reconstruct_active_ops(persisted)
    if active:
        failures.append(
            f"after terminal agent_result, active must be empty, got {active}"
        )

    # ── Scenario 3: approval pending + heartbeats + resolution ─────
    SEQ["n"] = 0
    persisted = []
    _emit(SessionEvent.build(
        type="approval_request", app_id="a", session_id="s", user_id="u",
        op_id="req-approval-1", op_type=OpType.APPROVAL,
        op_state=OpState.WAITING_APPROVAL,
        payload={"tool_name": "Write"},
    ))
    _emit(SessionEvent.build(
        type="approval_progress", app_id="a", session_id="s", user_id="u",
        op_id="req-approval-1", op_type=OpType.APPROVAL,
        op_state=OpState.WAITING_APPROVAL,
        payload={"heartbeat": True},
    ))
    active = _reconstruct_active_ops(persisted)
    if len(active) != 1 or active[0]["op_state"] != "waiting_approval":
        failures.append(
            f"approval pending: expected 1 waiting, got {active}"
        )
    _emit(SessionEvent.build(
        type="approval_resolved", app_id="a", session_id="s", user_id="u",
        op_id="req-approval-1", op_type=OpType.APPROVAL,
        op_state=OpState.COMPLETED,
        payload={"approved": True},
    ))
    active = _reconstruct_active_ops(persisted)
    if active:
        failures.append(f"after approval resolved: {active}")

    # ── Scenario 4: crash mid-cycle (op stays running, no terminal).
    # Simulates the DOS case the user described: a sub-agent was
    # spawned, the daemon crashed before emitting agent_result.
    # The sweeper (BUG-054 fix) would eventually mark it failed, but
    # before that, /active-ops must still report it so the client
    # can show "running (crashed?)".
    SEQ["n"] = 0
    persisted = []
    _emit(SessionEvent.build(
        type="agent_event", app_id="a", session_id="s", user_id="u",
        op_id="op-agent-crash", op_type=OpType.AGENT,
        op_state=OpState.RUNNING,
        payload={"action": "spawn_agent"},
    ))
    active = _reconstruct_active_ops(persisted)
    if len(active) != 1 or active[0]["op_state"] != "running":
        failures.append(
            f"crash scenario: expected running op to be reported, got {active}"
        )

    # ── Scenario 5: parent/child nesting — parent terminal, child
    # orphan. The client must know the child ISN'T active once
    # parent is cancelled (real UI depends on this). For now
    # reconstruct_active_ops reports BOTH states — the client is
    # expected to prune children whose parent is terminal.
    SEQ["n"] = 0
    persisted = []
    _emit(SessionEvent.build(
        type="agent_event", app_id="a", session_id="s", user_id="u",
        op_id="parent-agent", op_type=OpType.AGENT, op_state=OpState.RUNNING,
        payload={"action": "spawn_agent"},
    ))
    _emit(SessionEvent.build(
        type="tool_start", app_id="a", session_id="s", user_id="u",
        op_id="child-tool", op_type=OpType.TOOL, op_state=OpState.RUNNING,
        op_parent_id="parent-agent",
        payload={"name": "Read"},
    ))
    _emit(SessionEvent.build(
        type="agent_event", app_id="a", session_id="s", user_id="u",
        op_id="parent-agent", op_type=OpType.AGENT,
        op_state=OpState.CANCELLED,
        payload={"action": "agent_cancel"},
    ))
    active = _reconstruct_active_ops(persisted)
    # Parent is terminal, child still "running" per event log.
    parent_in = any(o["op_id"] == "parent-agent" for o in active)
    child_in = any(o["op_id"] == "child-tool" for o in active)
    if parent_in:
        failures.append("cancelled parent must NOT be in active_ops")
    if not child_in:
        failures.append(
            "orphan child tool should still be reported so client can "
            "prune it based on op_parent_id",
        )

    # ── Report ────────────────────────────────────────────────────

    if failures:
        print("FAIL — active-ops reconstruction:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — /active-ops reconstructs state correctly from persisted events")
    print("       (tool cycle, agent cycle, approval cycle, crash orphans, nesting)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))

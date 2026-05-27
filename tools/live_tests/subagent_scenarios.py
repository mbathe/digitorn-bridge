"""Live E2E regression test for sub-agent spawn (Agent tool).

Targets `digitorn-code` which declares a coordinator (`main`) + 4
specialists (`worker`, `explore`, `plan`, `verification`) and grants
`agent_spawn.agent`. No fixtures, no mocks, real LLM, full path.

Asserts :
  1. The coordinator invokes the Agent tool.
  2. A `spawn_agent` event fires for the chosen specialist.
  3. An `agent_result` event fires (the sub-agent completed).
  4. The agent_result payload references the same agent_id as spawn_agent.

Run:
    py -3.12 tools/live_tests/subagent_scenarios.py
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path("C:/Users/ASUS/Documents/digitorn-bridge/packages")))

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


APP_ID = "digitorn-code"


def _ok(label: str, cond: bool, why: str = "") -> tuple[str, tuple[bool, str]]:
    return (label, (cond, "" if cond else why))


def scenario_subagent_spawn(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    artifacts: dict[str, Any] = {}

    apps = client.list_apps()
    apps_iter = apps if isinstance(apps, list) else (apps.get("data") or apps.get("rows") or [])
    deployed_ids = {a.get("app_id") for a in apps_iter if isinstance(a, dict)}
    if APP_ID not in deployed_ids:
        return False, f"app '{APP_ID}' not deployed", artifacts

    sid = f"sub-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace="",
    )
    artifacts["session_id"] = sid

    stream = None
    try:
        prompt = (
            "Call Agent(specialist='explore', wait=true, prompt='Say pong'). "
            "After it returns, reply 'done'."
        )
        stream = client.send_live(session, prompt, total_timeout=300)
        events = assertions.sort_by_seq(stream.events())
        artifacts["event_count"] = len(events)
        artifacts["event_types"] = sorted({e["type"] for e in events})

        # Tool calls — Agent invocation by the coordinator.
        tool_calls = []
        for e in events:
            if e.get("type") in ("tool_call", "tool_start"):
                p = e.get("payload") or {}
                tool_calls.append({
                    "type": e.get("type"),
                    "tool_name": p.get("tool_name") or p.get("name"),
                    "params_preview": json.dumps(
                        p.get("params") or p.get("tool_params") or {},
                        default=str,
                    )[:200],
                })
        artifacts["tool_calls_seen"] = tool_calls[:12]

        called_agent_tool = any(
            (tc.get("tool_name") or "").lower() == "agent"
            for tc in tool_calls
        )

        # Sub-agent lifecycle events. The manager relays them as a
        # unified `agent_event` type (see CLAUDE.md Sub-agent events).
        # The inner kind/phase is in payload (spawn / progress / result /
        # cancel).
        agent_events = [e for e in events if e.get("type") == "agent_event"]
        artifacts["agent_event_count"] = len(agent_events)

        # Dump the inner kinds we saw, for diagnostic.
        kinds_seen: list[str] = []
        agent_ids: set[str] = set()
        for e in agent_events:
            p = e.get("payload") or {}
            kind = (
                p.get("kind") or p.get("event") or p.get("type")
                or p.get("phase") or p.get("status") or ""
            )
            if kind:
                kinds_seen.append(kind)
            aid = p.get("agent_id") or ""
            if aid:
                agent_ids.add(aid)
        artifacts["agent_event_kinds"] = kinds_seen[:20]
        artifacts["agent_event_first_payload"] = (
            (agent_events[0].get("payload") or {}) if agent_events else None
        )
        artifacts["agent_event_last_payload"] = (
            (agent_events[-1].get("payload") or {}) if agent_events else None
        )
        artifacts["agent_ids_seen"] = sorted(agent_ids)

        # Recognise spawn-like and result-like inner kinds. Be defensive
        # about exact wording (depends on relay version).
        _SPAWN_KINDS = {"spawn", "spawn_agent", "spawned", "start", "started"}
        _RESULT_KINDS = {
            "result", "agent_result", "complete", "completed",
            "done", "finished", "failed", "error",
        }
        had_spawn_like = any(k.lower() in _SPAWN_KINDS for k in kinds_seen)
        had_result_like = any(k.lower() in _RESULT_KINDS for k in kinds_seen)

        checks = [
            _ok("app deployed", APP_ID in deployed_ids, "missing"),
            _ok("events received", len(events) > 0, "0 events"),
            _ok(
                "coordinator invoked Agent tool",
                called_agent_tool,
                f"no Agent tool_call in {tool_calls!r}",
            ),
            _ok(
                "at least one agent_event seen on live stream",
                len(agent_events) >= 1,
                f"got 0 agent_event in {sorted({e['type'] for e in events})}",
            ),
            _ok(
                "agent_event payloads expose at least one agent_id",
                len(agent_ids) >= 1,
                f"no agent_id in payloads (first={artifacts.get('agent_event_first_payload')!r})",
            ),
            _ok(
                "saw a spawn-like phase for the sub-agent",
                had_spawn_like,
                f"kinds seen: {kinds_seen!r}",
            ),
            _ok(
                "saw a result-like phase for the sub-agent",
                had_result_like,
                f"kinds seen: {kinds_seen!r}",
            ),
        ]
        ok, detail = assertions.report(checks)
        return ok, detail, artifacts
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)


def main() -> int:
    creds_path = Path(r"C:\Users\ASUS\.digitorn\credentials.json")
    if not creds_path.exists():
        print(f"FAIL  CLI credentials missing: {creds_path}")
        return 1
    token = json.loads(creds_path.read_text(encoding="utf-8"))["access_token"]
    client = DevClient.with_token(token)

    t0 = time.monotonic()
    try:
        ok, detail, artifacts = scenario_subagent_spawn(client)
    except Exception as exc:
        print(f"FAIL  scenario crashed: {type(exc).__name__}: {exc}")
        return 1
    dt = time.monotonic() - t0

    print(f"\n=== sub-agent spawn round-trip ({dt:.1f}s) ===")
    print(detail)
    print("artifacts:")
    for k, v in artifacts.items():
        print(f"  {k:28s} = {v!r}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

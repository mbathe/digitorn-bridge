"""Production test: digitorn-deepresearch multi-agent one-shot research.

Verifies:
- Coordinator spawns sub-agents (agent_spawn / agent_progress / agent_result events)
- Web search actually runs
- Final synthesis is non-empty
- seq unique, no duplicates
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


def run() -> tuple[bool, list[str], dict]:
    token = os.environ.get("DIGITORN_TEST_TOKEN", "")
    if not token:
        raise RuntimeError("Set DIGITORN_TEST_TOKEN env var")
    client = DevClient.with_token(token)

    app_id = "digitorn-deepresearch"
    sid = f"prod-dr-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=app_id,
        daemon_url=client.daemon_url, workspace="",
    )

    bugs: list[str] = []
    artifacts: dict = {"session_id": sid}
    stream = None

    try:
        post = client.post_message_raw(session,
            "Write a brief (max 5 sentences) report on what Claude Opus 4 is. "
            "Use at most 2 specialists."
        )
        cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
        artifacts["correlation_id"] = cid
        stream = client.open_event_stream(session)

        done = stream.wait_for(
            "message_done", timeout=300,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
        )
        artifacts["done_received"] = done is not None
        if done is None:
            bugs.append("message_done never received within 300s")

        time.sleep(1.0)
        hist = client.get_history(session)
        last_assistant = ""
        for m in reversed(hist):
            if m.get("role") == "assistant" and m.get("content"):
                last_assistant = m["content"]
                break
        artifacts["final_answer"] = last_assistant[:600]
        if len(last_assistant) < 50:
            bugs.append(f"Final answer too short ({len(last_assistant)} chars): {last_assistant!r}")

        events = assertions.sort_by_seq(stream.events())
        types = [e.get("type", "?") for e in events]
        artifacts["total_events"] = len(events)
        artifacts["event_types_set"] = sorted(set(types))
        artifacts["agent_events"] = {
            "spawn": types.count("agent_spawn"),
            "spawn_agent": types.count("spawn_agent"),
            "progress": types.count("agent_progress"),
            "result": types.count("agent_result"),
            "cancel": types.count("agent_cancel"),
        }

        spawn_count = types.count("agent_spawn") + types.count("spawn_agent")
        if spawn_count == 0:
            bugs.append("No agent_spawn/spawn_agent event emitted — coordinator did not spawn sub-agents")
        result_count = types.count("agent_result")
        if result_count == 0 and spawn_count > 0:
            bugs.append(f"{spawn_count} spawns but zero agent_result events")

        tool_calls = [e for e in events if e.get("type") == "tool_call"]
        tool_names = [((e.get("payload") or {}).get("data") or {}).get("name", "") for e in tool_calls]
        artifacts["tool_names_sample"] = tool_names[:12]
        used_web = any("web" in n.lower() or "search" in n.lower() or "fetch" in n.lower() for n in tool_names)
        if not used_web:
            bugs.append(f"No web tool call observed. Tools={tool_names}")

        ok, detail = assertions.seq_unique(events)
        if not ok:
            bugs.append(f"seq_unique FAILED: {detail}")

    except Exception as e:
        bugs.append(f"EXCEPTION: {type(e).__name__}: {e}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    return (len(bugs) == 0), bugs, artifacts


if __name__ == "__main__":
    ok, bugs, art = run()
    print(f"\n{'=' * 60}")
    print(f"RESULT: {'PASS' if ok else 'FAIL'}")
    print(f"{'=' * 60}")
    if bugs:
        print("\nBUGS:")
        for i, b in enumerate(bugs, 1):
            print(f"  {i}. {b}")
    print("\nARTIFACTS:")
    print(json.dumps(art, indent=2, default=str)[:6000])
    sys.exit(0 if ok else 1)

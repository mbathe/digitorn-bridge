"""Real user scenario on digitorn-builder: ask it to build a simple counter app.

We act as a user who walks through the flow:
1. "I want to build an app that counts things"
2. Answer any clarifying question naturally
3. Ask it to validate + show the YAML
4. Check the workspace has app.yaml
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
        raise RuntimeError("No token")
    client = DevClient.with_token(token)

    app_id = "digitorn-builder"
    sid = f"builder-{uuid.uuid4().hex[:6]}"
    session = SessionHandle(session_id=sid, app_id=app_id,
                            daemon_url=client.daemon_url, workspace="")
    bugs: list[str] = []
    art: dict = {"session_id": sid, "turns": []}
    stream = None

    turns_plan = [
        "I want to build a simple conversational app called 'counter-helper' that helps a user keep a counter — the user can say 'increment', 'decrement', 'reset', 'what's the count'. Use memory to store the count. Deepseek model. No web. Just this.",
        "Please go ahead and generate the YAML now. Show it to me and save it as a draft.",
        "Validate the YAML against the daemon (compile it) and tell me if it's valid.",
    ]

    try:
        post = client.post_message_raw(session, turns_plan[0])
        cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
        stream = client.open_event_stream(session)
        done = stream.wait_for("message_done", timeout=240,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid)
        art["turns"].append({"turn": 1, "done": done is not None, "cid": cid})
        if done is None:
            bugs.append("Turn 1: message_done never received (240s)")

        time.sleep(1.0)
        t1_hist = client.get_history(session)
        t1_last = next((m.get("content","") for m in reversed(t1_hist) if m.get("role")=="assistant" and m.get("content")), "")
        art["turns"][-1]["last_assistant"] = t1_last[:500]

        # Turn 2
        post = client.post_message_raw(session, turns_plan[1])
        cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
        done = stream.wait_for("message_done", timeout=300,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid)
        art["turns"].append({"turn": 2, "done": done is not None, "cid": cid})
        if done is None:
            bugs.append("Turn 2: message_done never received (300s)")

        # Turn 3
        post = client.post_message_raw(session, turns_plan[2])
        cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
        done = stream.wait_for("message_done", timeout=300,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid)
        art["turns"].append({"turn": 3, "done": done is not None, "cid": cid})
        if done is None:
            bugs.append("Turn 3: message_done never received (300s)")

        time.sleep(1.5)
        # Inspect workspace — expect app.yaml present
        ws = client.get_workspace(session)
        snap = (ws.get("snapshot") or {}) if isinstance(ws, dict) else {}
        files = ((snap.get("resources") or {}).get("files") or {})
        art["files_in_workspace"] = list(files.keys())[:20]

        app_yaml = files.get("app.yaml", {})
        yaml_content = app_yaml.get("content", "") if isinstance(app_yaml, dict) else ""
        art["app_yaml_len"] = len(yaml_content)
        art["app_yaml_head"] = yaml_content[:400]

        if not yaml_content:
            bugs.append("app.yaml absent or empty in workspace")
        else:
            # Basic sanity: should have app_id, agents, modules
            for marker in ("app_id", "agents", "modules"):
                if marker not in yaml_content:
                    bugs.append(f"app.yaml missing '{marker}' marker")
            # Should mention counter
            if "counter" not in yaml_content.lower():
                bugs.append("app.yaml doesn't mention 'counter' — builder may have lost the requirement")

        # Check drafts list
        drafts = client.list_drafts()
        art["drafts_count"] = len(drafts) if isinstance(drafts, list) else None
        art["drafts_sample"] = [d.get("name", d.get("id","?"))[:60] for d in (drafts or [])][:5]

        # Events summary
        events = assertions.sort_by_seq(stream.events())
        art["total_events"] = len(events)
        art["event_types"] = sorted({e.get("type") for e in events})
        ok, detail = assertions.seq_unique(events)
        if not ok:
            bugs.append(f"seq_unique: {detail}")

        tool_types = [e for e in events if e.get("type") == "tool_start"]
        # Extract tool names
        tool_names: list[str] = []
        for e in tool_types:
            payload = e.get("payload") or {}
            data = payload.get("data") or payload
            name = data.get("name") or data.get("tool_name") or ""
            tool_names.append(name)
        art["tool_calls_seen"] = tool_names[:30]
        art["tool_call_count"] = len(tool_names)

    except Exception as e:
        bugs.append(f"EXCEPTION: {type(e).__name__}: {e}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    return (len(bugs) == 0), bugs, art


if __name__ == "__main__":
    ok, bugs, art = run()
    print(f"\n{'=' * 60}\nBUILDER DEEP: {'PASS' if ok else 'FAIL'}\n{'=' * 60}")
    for i, b in enumerate(bugs, 1):
        print(f"  {i}. {b}")
    print("\nARTIFACTS:", json.dumps(art, indent=2, default=str)[:6000])
    sys.exit(0 if ok else 1)

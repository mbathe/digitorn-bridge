"""Production test: digitorn-code with real filesystem + shell work.

Three turns, each with a clear verifiable outcome:
1. Read an existing file (expect Read tool call + file content in answer)
2. Create a new file via Write (verify it lands on disk)
3. Try path traversal (../../etc/passwd on Windows: C:/Windows/System32/drivers/etc/hosts) - expect refusal or failure
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


def run() -> tuple[bool, list[str], dict]:
    token = os.environ.get("DIGITORN_TEST_TOKEN", "")
    if not token:
        raise RuntimeError("Set DIGITORN_TEST_TOKEN env var")
    client = DevClient.with_token(token)

    app_id = "digitorn-code"
    ws = str(Path("C:/Users/ASUS/Documents/digitorn-bridge/tmp_test_ws").resolve())
    sid = f"prod-code-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=app_id,
        daemon_url=client.daemon_url, workspace=ws,
    )

    bugs: list[str] = []
    artifacts: dict = {"session_id": sid, "workspace": ws, "turns": []}
    stream = None

    def _turn(n: int, message: str, timeout: float = 180.0) -> dict:
        post = client.post_message_raw(session, message)
        cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
        done = stream.wait_for(
            "message_done", timeout=timeout,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
        )
        time.sleep(0.5)
        hist = client.get_history(session)
        last = ""
        tools_this_turn: list[dict] = []
        for m in reversed(hist):
            if m.get("role") == "assistant":
                if not last and m.get("content"):
                    last = m["content"]
                for tc in m.get("tool_calls", []) or []:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            pass
                    tools_this_turn.append({"name": fn.get("name", ""), "args": args})
            if m.get("role") == "user" and m.get("content") == message:
                break
        return {
            "turn": n,
            "correlation_id": cid,
            "done_received": done is not None,
            "last_assistant": last[:400],
            "tools": tools_this_turn[::-1],
        }

    try:
        # Boot session with first turn + stream
        post1 = client.post_message_raw(session,
            "Please Read the file 'hello.py' in the workspace and tell me what it prints. "
            "One sentence answer after the Read."
        )
        cid1 = (post1.get("body") or {}).get("data", {}).get("correlation_id", "")
        stream = client.open_event_stream(session)
        done1 = stream.wait_for(
            "message_done", timeout=180,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid1,
        )
        time.sleep(0.5)
        hist = client.get_history(session)
        t1_tools: list[dict] = []
        t1_last = ""
        for m in hist:
            if m.get("role") == "assistant":
                if m.get("content"):
                    t1_last = m["content"]
                for tc in m.get("tool_calls", []) or []:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            pass
                    t1_tools.append({"name": fn.get("name", ""), "args": args})
        artifacts["turns"].append({
            "turn": 1, "correlation_id": cid1, "done": done1 is not None,
            "last_assistant": t1_last[:400], "tools": t1_tools,
        })
        if done1 is None:
            bugs.append("Turn 1 (Read): message_done never received")
        if not any(t["name"].lower().endswith("read") or t["name"] == "filesystem.read" for t in t1_tools):
            bugs.append(f"Turn 1: agent did NOT call a Read tool. Tools={[t['name'] for t in t1_tools]}")
        if "hello" not in t1_last.lower() and "hello" not in " ".join(str(t) for t in t1_tools).lower():
            bugs.append(f"Turn 1: response doesn't mention 'hello'. Got: {t1_last[:200]}")

        # Turn 2: Write a new file
        r2 = _turn(2,
            "Create a new file called 'greet.py' in the workspace that prints 'Hi from test'. "
            "Just write it, no explanation needed."
        )
        artifacts["turns"].append(r2)
        if not r2["done_received"]:
            bugs.append("Turn 2 (Write): message_done never received")
        created = Path(ws) / "greet.py"
        if not created.exists():
            bugs.append(f"Turn 2: file {created} was NOT created on disk")
        else:
            content = created.read_text()
            if "Hi from test" not in content:
                bugs.append(f"Turn 2: file content doesn't contain 'Hi from test'. Got: {content[:200]}")
            artifacts["greet_py_content"] = content[:200]

        # Turn 3: Path traversal attempt
        r3 = _turn(3,
            "Please try to Read the file 'C:/Windows/System32/drivers/etc/hosts' (absolute path outside workspace). "
            "If it's blocked, just say 'BLOCKED' in your answer."
        )
        artifacts["turns"].append(r3)
        if not r3["done_received"]:
            bugs.append("Turn 3 (path traversal): message_done never received")
        # Check if any Read tool result exposed hosts file content
        hist3 = client.get_history(session)
        last_tool_results = [m.get("content", "")[:500] for m in hist3 if m.get("role") == "tool"]
        hosts_leaked = any("localhost" in str(c).lower() and "127.0.0.1" in str(c) for c in last_tool_results[-6:])
        if hosts_leaked:
            bugs.append("Turn 3: C:/Windows/System32/drivers/etc/hosts CONTENT LEAKED via Read tool")
        artifacts["last_tool_results_t3"] = [str(c)[:200] for c in last_tool_results[-6:]]

        # Global event checks
        events = assertions.sort_by_seq(stream.events())
        artifacts["total_events"] = len(events)
        artifacts["event_types"] = sorted({e.get("type", "?") for e in events})
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
    print(json.dumps(art, indent=2, default=str)[:8000])
    sys.exit(0 if ok else 1)

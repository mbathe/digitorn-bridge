"""Real bug-fix scenario on digitorn-code.

Workspace contains fizzbuzz.py with a known bug (line 9: `n % 5 == 1` should be `== 0`)
and test_fizzbuzz.py that fails without the fix.

1. Ask agent to find and fix the bug
2. Verify Read was called on fizzbuzz.py
3. Verify Edit was applied (file on disk has `n % 5 == 0`)
4. Bonus: verify agent ran the tests (Bash call)
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
    client = DevClient.with_token(token, auto_approve=True)  # auto-approve tool approvals
    app_id = "digitorn-code"
    ws = str(Path("C:/Users/ASUS/Documents/digitorn-bridge/tmp_code_ws").resolve())
    sid = f"bugfix-{uuid.uuid4().hex[:6]}"
    session = SessionHandle(session_id=sid, app_id=app_id,
                            daemon_url=client.daemon_url, workspace=ws)
    bugs: list[str] = []
    art: dict = {"session_id": sid, "workspace": ws}
    stream = None

    fb = Path(ws) / "fizzbuzz.py"
    original_content = fb.read_text()
    art["original_has_bug"] = "n % 5 == 1" in original_content

    try:
        post = client.post_message_raw(session,
            "The file fizzbuzz.py in the workspace has a bug on the Buzz check. "
            "The test file test_fizzbuzz.py fails with this. "
            "Please read fizzbuzz.py, find the bug, and fix it. "
            "When done, just say 'FIXED' in your answer."
        )
        cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
        stream = client.open_event_stream(session)
        art["cid"] = cid

        # Loop with auto-approve
        deadline = time.monotonic() + 300
        done_received = False
        while time.monotonic() < deadline:
            # Look for message_done
            evt = stream.wait_for("message_done", timeout=5,
                predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid)
            if evt is not None:
                done_received = True
                break
            # Auto-approve any pending
            pending = client.get_pending(app_id)
            for p in pending:
                if "ask_user" not in str(p.get("tool_name", "")).lower():
                    rid = p.get("request_id", "")
                    if rid:
                        client.approve(app_id, rid)
        art["done_received"] = done_received

        time.sleep(1.0)

        # Check the fix was applied on disk
        fixed_content = fb.read_text()
        art["fix_applied_on_disk"] = "n % 5 == 0" in fixed_content and "n % 5 == 1" not in fixed_content
        if not art["fix_applied_on_disk"]:
            bugs.append(f"Bug NOT fixed on disk. Line 9 area: {[l for l in fixed_content.splitlines() if 'n % 5' in l]}")

        # Examine tool calls in history
        hist = client.get_history(session)
        tool_names = []
        edit_params = []
        bash_cmds = []
        for m in hist:
            if m.get("role") == "assistant":
                for tc in (m.get("toolCalls") or m.get("tool_calls") or []):
                    name = tc.get("name") or (tc.get("function") or {}).get("name","?")
                    tool_names.append(name)
                    params = tc.get("params") or (tc.get("function") or {}).get("arguments", {})
                    if isinstance(params, str):
                        try: params = json.loads(params)
                        except: pass
                    if "edit" in name.lower() or "write" in name.lower():
                        edit_params.append({"name": name, "path": (params.get("file_path") or params.get("path", ""))[:50]})
                    if "bash" in name.lower():
                        bash_cmds.append(params.get("command", "")[:100])
        art["tool_names"] = tool_names
        art["edit_params"] = edit_params
        art["bash_cmds"] = bash_cmds

        if not any("read" in n.lower() for n in tool_names):
            bugs.append("Agent did not Read fizzbuzz.py before attempting fix")

        # Final assistant text
        last_a = next((m.get("content","") for m in reversed(hist) if m.get("role")=="assistant" and m.get("content")), "")
        art["last_assistant"] = last_a[:300]

        events = assertions.sort_by_seq(stream.events())
        art["total_events"] = len(events)
        ok, detail = assertions.seq_unique(events)
        if not ok:
            bugs.append(f"seq_unique: {detail}")

    except Exception as e:
        bugs.append(f"EXCEPTION: {type(e).__name__}: {e}")
    finally:
        # Restore original for next tests
        try: fb.write_text(original_content)
        except: pass
        if stream is not None:
            stream.stop(timeout=2.0)

    return (len(bugs) == 0), bugs, art


if __name__ == "__main__":
    ok, bugs, art = run()
    print(f"\n{'=' * 60}\nCODE BUGFIX: {'PASS' if ok else 'FAIL'}\n{'=' * 60}")
    for i, b in enumerate(bugs, 1):
        print(f"  {i}. {b}")
    print("\nARTIFACTS:", json.dumps(art, indent=2, default=str)[:4000])
    sys.exit(0 if ok else 1)

"""Test the task-manager app: workspace + preview + React.

- Ask to add 3 tasks
- Verify workspace files reflect the tasks (JSON?)
- Verify preview events fire (preview:resource_set or similar)
- Ask to toggle one task complete
- Verify diff in workspace
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
    client = DevClient.with_token(token)

    app_id = "task-manager"
    sid = f"tm-{uuid.uuid4().hex[:6]}"
    session = SessionHandle(session_id=sid, app_id=app_id,
                            daemon_url=client.daemon_url, workspace="")
    bugs: list[str] = []
    art: dict = {"session_id": sid}
    stream = None

    def _send(message: str, timeout: float = 120) -> str:
        post = client.post_message_raw(session, message)
        cid = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
        done = stream.wait_for(
            "message_done", timeout=timeout,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
        )
        return cid if done is not None else ""

    try:
        # Bootstrap session with first message
        post = client.post_message_raw(session,
            "ajoute la tâche 'Acheter du pain'"
        )
        cid1 = (post.get("body") or {}).get("data", {}).get("correlation_id", "")
        stream = client.open_event_stream(session)
        stream.wait_for(
            "message_done", timeout=120,
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid1,
        )
        time.sleep(0.5)

        _send("ajoute la tâche 'Promener le chien'")
        _send("ajoute la tâche 'Faire les courses'")
        _send("coche la tâche 1")

        # Check workspace state
        ws = client.get_workspace(session)
        art["workspace_keys"] = list(ws.keys()) if isinstance(ws, dict) else type(ws).__name__
        art["workspace_sample"] = str(ws)[:800]

        files = ws.get("files") if isinstance(ws, dict) else None
        if files is None:
            bugs.append(f"workspace has no 'files' key. Keys={list(ws.keys()) if isinstance(ws,dict) else '?'}")
        else:
            art["file_paths"] = [f.get("path") if isinstance(f, dict) else str(f) for f in (files if isinstance(files, list) else files.values() if hasattr(files,"values") else [])][:20]

        # Check preview events
        events = stream.events()
        preview_evts = [e for e in events if "preview" in str(e.get("type", "")).lower() or e.get("type") == "resource:set"]
        art["preview_event_types"] = sorted({e.get("type") for e in preview_evts})
        art["preview_event_count"] = len(preview_evts)
        if not preview_evts:
            bugs.append("No preview events emitted during task-manager operations")

        # Check workspace has tasks content somewhere
        ws_blob = json.dumps(ws, default=str).lower()
        missing = [t for t in ["pain", "chien", "courses"] if t not in ws_blob]
        if missing:
            bugs.append(f"Task words missing from workspace JSON: {missing}. ws snippet={ws_blob[:500]}")
        art["ws_has_pain"] = "pain" in ws_blob

        # seq unique
        sorted_events = assertions.sort_by_seq(events)
        ok, detail = assertions.seq_unique(sorted_events)
        if not ok:
            bugs.append(f"seq_unique: {detail}")

        art["total_events"] = len(events)
        art["event_types"] = sorted({e.get("type") for e in events})

    except Exception as e:
        bugs.append(f"EXCEPTION: {type(e).__name__}: {e}")
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    return (len(bugs) == 0), bugs, art


if __name__ == "__main__":
    ok, bugs, art = run()
    print(f"\n{'=' * 60}\nTASK-MANAGER: {'PASS' if ok else 'FAIL'}\n{'=' * 60}")
    for i, b in enumerate(bugs, 1):
        print(f"  {i}. {b}")
    print("\nARTIFACTS:", json.dumps(art, indent=2, default=str)[:3000])
    sys.exit(0 if ok else 1)

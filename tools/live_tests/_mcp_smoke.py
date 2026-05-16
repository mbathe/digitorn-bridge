"""Quick smoke: prove the testing SDK can drive a live LLM turn at all
on this daemon. Uses the built-in ``digitorn-chat`` app which is auto-
deployed by the daemon and is guaranteed to be available.

If THIS fails, the MCP scenarios were never going to pass — the
problem is auth/brain/wiring, not MCP.
"""
from __future__ import annotations

import json
import sys
import time
import uuid

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


def main() -> int:
    client = DevClient()
    sid = f"smoke-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id="digitorn-chat",
        daemon_url=client.daemon_url, workspace="",
    )
    print(f"app=digitorn-chat session={sid} daemon={client.daemon_url}")
    t0 = time.monotonic()
    try:
        stream = client.send_live(session, "Réponds 'pong' en un seul mot.", total_timeout=45)
    except Exception as exc:  # noqa: BLE001
        print(f"send_live FAILED: {exc!r}")
        return 1
    events = stream.events()
    sorted_events = assertions.sort_by_seq(events)

    # Histogram + sample contents
    by_type: dict[str, int] = {}
    sample_payloads: dict[str, dict] = {}
    for ev in events:
        t = str(ev.get("type"))
        by_type[t] = by_type.get(t, 0) + 1
        if t not in sample_payloads and t in (
            "message_done", "message_started", "user_message", "error",
        ):
            sample_payloads[t] = ev.get("payload") or {}
    stream.stop(timeout=2.0)

    print(f"elapsed={time.monotonic() - t0:0.1f}s events={len(events)}")
    print("type_histogram:", json.dumps(by_type, indent=2))
    print("samples:", json.dumps({k: v for k, v in sample_payloads.items()}, indent=2, default=str)[:600])

    ok = (
        by_type.get("message_done", 0) >= 1
        and by_type.get("message_started", 0) >= 1
    )
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

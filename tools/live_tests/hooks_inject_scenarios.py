"""Live E2E regression test for Hooks V2 inject_message declarative action.

Fixture: `hooks-inject-test` with a single hook on turn_start that
injects a marker string into the user message. If hook fires + action
applies, the LLM sees the directive and includes the marker in its
reply. Verifying the marker in the assistant text proves the full
hook execution path end-to-end.

Run:
    py -3.12 tools/live_tests/hooks_inject_scenarios.py
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


FIXTURE = Path("C:/Users/ASUS/Documents/digitorn-bridge/tools/live_tests/_fixtures/hooks_inject_app.yaml")
APP_ID = "hooks-inject-test"
MARKER = "HOOK-MK-9821"


def _ok(label: str, cond: bool, why: str = "") -> tuple[str, tuple[bool, str]]:
    return (label, (cond, "" if cond else why))


def scenario_hook_inject_message(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    artifacts: dict[str, Any] = {}

    deploy_resp = client.deploy(str(FIXTURE), wait=30.0)
    artifacts["deploy_agents"] = deploy_resp.agents
    artifacts["deploy_total_tools"] = deploy_resp.total_tools

    sid = f"inj-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace="",
    )
    artifacts["session_id"] = sid

    try:
        post_result = client.post_message_raw(session, "Reply with one short sentence about the sea.")
        artifacts["post_status_code"] = post_result.get("status_code")
        time.sleep(0.5)
        stream = client.open_event_stream(session)
        try:
            correlation_id = (
                (post_result.get("body") or {}).get("data", {}).get("correlation_id")
                or ""
            )
            if correlation_id:
                stream.wait_for(
                    "message_done", timeout=120,
                    predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == correlation_id,
                )
            else:
                stream.wait_until_idle(quiet_seconds=3.0, total_timeout=120)
            events = assertions.sort_by_seq(stream.events())
        finally:
            stream.stop(timeout=2.0)
        artifacts["event_count"] = len(events)
        artifacts["event_types"] = sorted({e["type"] for e in events})

        reply_text = ""
        for e in events:
            if e.get("type") in ("out_token", "token"):
                p = e.get("payload") or {}
                d = p.get("delta") or p.get("text") or p.get("content")
                if isinstance(d, str):
                    reply_text += d
        artifacts["reply_length"] = len(reply_text)
        artifacts["reply_preview"] = reply_text[:240]

        history = client.get_history(session)
        if isinstance(history, dict):
            history_msgs = history.get("messages") or history.get("history") or []
        else:
            history_msgs = history or []
        artifacts["history_count"] = len(history_msgs)

        assistant_text = ""
        for m in history_msgs:
            if m.get("role") == "assistant":
                c = m.get("content", "")
                if isinstance(c, str):
                    assistant_text += c

        marker_in_stream = MARKER in reply_text
        marker_in_history = MARKER in assistant_text
        marker_anywhere = marker_in_stream or marker_in_history

        checks = [
            _ok("app deployed", bool(deploy_resp.agents), "no agents"),
            _ok("post returned 200", artifacts["post_status_code"] == 200,
                f"got {artifacts['post_status_code']}"),
            _ok("turn produced events", len(events) > 0, "0 events"),
            _ok(
                "marker injected by hook appears in assistant reply",
                marker_anywhere,
                f"marker={MARKER!r} not found in stream ({reply_text[:80]!r}) "
                f"nor history ({assistant_text[:80]!r})",
            ),
        ]
        ok, detail = assertions.report(checks)
        return ok, detail, artifacts
    finally:
        try:
            client.undeploy(APP_ID)
        except Exception:
            pass


def main() -> int:
    creds_path = Path(r"C:\Users\ASUS\.digitorn\credentials.json")
    if not creds_path.exists():
        print(f"FAIL  CLI credentials missing: {creds_path}")
        return 1
    token = json.loads(creds_path.read_text(encoding="utf-8"))["access_token"]
    client = DevClient.with_token(token)

    t0 = time.monotonic()
    try:
        ok, detail, artifacts = scenario_hook_inject_message(client)
    except Exception as exc:
        print(f"FAIL  scenario crashed: {type(exc).__name__}: {exc}")
        return 1
    dt = time.monotonic() - t0

    print(f"\n=== hooks V2 inject_message ({dt:.1f}s) ===")
    print(detail)
    print("artifacts:")
    for k, v in artifacts.items():
        line = f"  {k:28s} = {v!r}"
        sys.stdout.buffer.write(line.encode("utf-8", "replace") + b"\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""Live E2E regression test for the workspace write/read contract.

Targets `digitorn-code` (grants WsWrite + WsRead). No fixtures, no mocks,
real LLM, full agent path. Acts like a human asking the agent to write
a file then read it back.

Run:
    py -3.12 tools/live_tests/workspace_scenarios.py
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
MARKER = "ws-roundtrip-9821"
FILE_PATH = "ws_test.txt"


def _ok(label: str, cond: bool, why: str = "") -> tuple[str, tuple[bool, str]]:
    return (label, (cond, "" if cond else why))


def scenario_ws_write_read(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    artifacts: dict[str, Any] = {}

    apps = client.list_apps()
    apps_iter = apps if isinstance(apps, list) else (apps.get("data") or apps.get("rows") or [])
    deployed_ids = {a.get("app_id") for a in apps_iter if isinstance(a, dict)}
    if APP_ID not in deployed_ids:
        return False, f"app '{APP_ID}' not deployed", artifacts

    sid = f"ws-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace="",
    )
    artifacts["session_id"] = sid

    stream = None
    try:
        prompt = (
            f"Use the WsWrite tool to create a file named '{FILE_PATH}' "
            f"with content exactly: {MARKER}\n\n"
            f"Then use WsRead to read it back and reply with just 'done'."
        )
        stream = client.send_live(session, prompt, total_timeout=300)
        events = assertions.sort_by_seq(stream.events())
        artifacts["event_count"] = len(events)
        artifacts["event_types"] = sorted({e["type"] for e in events})

        # Collect tool_call events for diagnostics.
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
                    )[:150],
                })
        artifacts["tool_calls_seen"] = tool_calls[:12]

        # Accept any write-tool the agent may pick (Write / WsWrite /
        # workspace.write / filesystem.write). Short-name resolution is
        # documented in CLAUDE.md.
        _WRITE_NAMES = {"write", "wswrite", "workspace.write", "filesystem.write"}
        _READ_NAMES = {"read", "wsread", "workspace.read", "filesystem.read"}
        called_write = any(
            (tc.get("tool_name") or "").lower() in _WRITE_NAMES
            for tc in tool_calls
        )

        # The decisive check: read the file content via the workspace API.
        # This is what the file ENDS UP being, regardless of the agent's
        # final natural-language reply.
        actual_content = ""
        actual_status = None
        try:
            file_resp = client.get_workspace_file_content(session, FILE_PATH)
            if isinstance(file_resp, dict):
                # API shape: {payload: {content: "..."}, ...}
                payload = file_resp.get("payload") or {}
                actual_content = (
                    payload.get("content")
                    or file_resp.get("content")
                    or ""
                )
            elif isinstance(file_resp, str):
                actual_content = file_resp
            actual_status = "ok"
        except Exception as exc:
            actual_status = f"{type(exc).__name__}: {exc}"
        artifacts["read_status"] = actual_status
        artifacts["read_content_preview"] = (actual_content or "")[:120]

        marker_in_content = (
            isinstance(actual_content, str) and MARKER in actual_content
        )

        checks = [
            _ok("app deployed", APP_ID in deployed_ids, "missing"),
            _ok("events received", len(events) > 0, "stream returned 0 events"),
            _ok(
                "agent invoked a write tool",
                called_write,
                f"no write tool in tool_calls: {tool_calls!r}",
            ),
            _ok(
                "file readable via workspace API",
                actual_status == "ok",
                f"read failed: {actual_status}",
            ),
            _ok(
                "file content contains the marker",
                marker_in_content,
                f"marker {MARKER!r} not in content (preview: "
                f"{(actual_content or '')[:80]!r})",
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
        ok, detail, artifacts = scenario_ws_write_read(client)
    except Exception as exc:
        print(f"FAIL  scenario crashed: {type(exc).__name__}: {exc}")
        return 1
    dt = time.monotonic() - t0

    print(f"\n=== workspace WsWrite/WsRead round-trip ({dt:.1f}s) ===")
    print(detail)
    print("artifacts:")
    for k, v in artifacts.items():
        print(f"  {k:28s} = {v!r}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

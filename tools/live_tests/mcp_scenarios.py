"""Live E2E regression test for MCP server tool invocation.

Targets `qtest-mcp-fetch-dict` deployed at boot (mcp_fetch category,
1 tool). Asserts the agent path through an MCP-provided tool.

Run:
    py -3.12 tools/live_tests/mcp_scenarios.py
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


APP_ID = "qtest-mcp-fetch-dict"


def _ok(label: str, cond: bool, why: str = "") -> tuple[str, tuple[bool, str]]:
    return (label, (cond, "" if cond else why))


def scenario_mcp_tool_invocation(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    artifacts: dict[str, Any] = {}

    apps = client.list_apps() or []
    deployed_ids = {a.get("app_id") for a in apps if isinstance(a, dict)}
    if APP_ID not in deployed_ids:
        return False, f"app '{APP_ID}' not deployed", artifacts

    sid = f"mcp-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace="",
    )
    artifacts["session_id"] = sid

    stream = None
    try:
        prompt = (
            "Use the fetch tool to fetch the definition of the word 'serendipity'. "
            "Reply with a one-sentence summary."
        )
        stream = client.send_live(session, prompt, total_timeout=180)
        events = assertions.sort_by_seq(stream.events())
        artifacts["event_count"] = len(events)
        artifacts["event_types"] = sorted({e["type"] for e in events})

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
        artifacts["tool_calls_seen"] = tool_calls[:10]

        mcp_tool_called = any(
            ("mcp_" in (tc.get("tool_name") or "").lower()
             or "fetch" in (tc.get("tool_name") or "").lower())
            for tc in tool_calls
        )

        message_done = any(e.get("type") == "message_done" for e in events)

        checks = [
            _ok("app deployed", APP_ID in deployed_ids, "missing"),
            _ok("events received", len(events) > 0, "0 events"),
            _ok("turn reached message_done", message_done, "no message_done"),
            _ok(
                "agent invoked an MCP tool (mcp_* or fetch)",
                mcp_tool_called,
                f"no MCP-like tool in {tool_calls!r}",
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
        ok, detail, artifacts = scenario_mcp_tool_invocation(client)
    except Exception as exc:
        print(f"FAIL  scenario crashed: {type(exc).__name__}: {exc}")
        return 1
    dt = time.monotonic() - t0

    print(f"\n=== MCP tool invocation ({dt:.1f}s) ===")
    print(detail)
    print("artifacts:")
    for k, v in artifacts.items():
        line = f"  {k:28s} = {v!r}"
        sys.stdout.buffer.write(line.encode("utf-8", "replace") + b"\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

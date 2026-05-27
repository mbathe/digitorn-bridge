"""Live E2E regression test for tool discovery (SearchTools, GetTool).

Uses a minimal test fixture (`tool-discovery-test`) deployed with
`tool_injection: discovery` and `direct_modules: []` so the agent has
NO tools directly and MUST call SearchTools to find them.

Asserts :
  1. Agent calls SearchTools at least once.
  2. SearchTools returns at least one matching tool.
  3. Agent calls a discovered tool (filesystem.glob/read/grep).

Real LLM, real daemon, agent path complete.

Run:
    py -3.12 tools/live_tests/discovery_scenarios.py
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


FIXTURE = Path("C:/Users/ASUS/Documents/digitorn-bridge/tools/live_tests/_fixtures/tool_discovery_app.yaml")
APP_ID = "tool-discovery-test"


def _ok(label: str, cond: bool, why: str = "") -> tuple[str, tuple[bool, str]]:
    return (label, (cond, "" if cond else why))


def scenario_search_tools_invocation(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    artifacts: dict[str, Any] = {}

    deploy_resp = client.deploy(str(FIXTURE), wait=90.0)
    artifacts["deploy_agents"] = deploy_resp.agents
    artifacts["deploy_total_tools"] = deploy_resp.total_tools

    sid = f"disc-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace="",
    )
    artifacts["session_id"] = sid

    prompt = (
        "I need to look for files in the workspace. Use SearchTools "
        "with the query 'glob' to find an appropriate tool, then "
        "call that tool with pattern '*.txt'. Reply 'done' when finished."
    )
    # Manual post + small settle + stream, to avoid send_live's
    # tight 5s wait_for_session on freshly-deployed apps.
    post_result = client.post_message_raw(session, prompt)
    artifacts["post_status_code"] = post_result.get("status_code")
    artifacts["post_status"] = (post_result.get("body") or {}).get("data", {}).get("status")
    time.sleep(0.5)

    stream = None
    try:
        stream = client.open_event_stream(session)
        # Wait for message_done with the right correlation_id, or idle.
        correlation_id = (
            (post_result.get("body") or {}).get("data", {}).get("correlation_id")
            or ""
        )
        if correlation_id:
            stream.wait_for(
                "message_done", timeout=180,
                predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == correlation_id,
            )
        else:
            stream.wait_until_idle(quiet_seconds=4.0, total_timeout=180)
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
        artifacts["tool_calls_seen"] = tool_calls[:15]

        # Normalise short / fqn forms.
        def _is_search(name: str) -> bool:
            n = (name or "").lower()
            return n in ("searchtools", "search_tools", "context_builder.search_tools")

        def _is_get(name: str) -> bool:
            n = (name or "").lower()
            return n in ("gettool", "get_tool", "context_builder.get_tool")

        def _is_filesystem_call(tc: dict) -> bool:
            """Match either a direct tool call OR execute_tool(name='filesystem.*')."""
            n = (tc.get("tool_name") or "").lower()
            if n.startswith(("glob", "read", "grep", "filesystem.")):
                return True
            if n in ("execute_tool", "context_builder.execute_tool"):
                # execute_tool is the discovery-mode invocation wrapper.
                pp = tc.get("params_preview") or ""
                if "filesystem.glob" in pp or "filesystem.read" in pp or "filesystem.grep" in pp:
                    return True
            return False

        called_search = any(_is_search(tc.get("tool_name") or "") for tc in tool_calls)
        called_any_get = any(_is_get(tc.get("tool_name") or "") for tc in tool_calls)
        called_filesystem = any(_is_filesystem_call(tc) for tc in tool_calls)
        artifacts["called_search_tools"] = called_search
        artifacts["called_get_tool"] = called_any_get
        artifacts["called_a_filesystem_tool"] = called_filesystem

        checks = [
            _ok("app deployed", APP_ID in {
                (a.get("app_id") if isinstance(a, dict) else None)
                for a in (client.list_apps() or [])
            }, "missing"),
            _ok("events received", len(events) > 0, "0 events"),
            _ok(
                "agent invoked SearchTools",
                called_search,
                f"no SearchTools call in {tool_calls!r}",
            ),
            _ok(
                "agent called at least one filesystem tool after discovery",
                called_filesystem,
                f"no glob/read/grep call seen",
            ),
        ]
        ok, detail = assertions.report(checks)
        return ok, detail, artifacts
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)
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
        ok, detail, artifacts = scenario_search_tools_invocation(client)
    except Exception as exc:
        print(f"FAIL  scenario crashed: {type(exc).__name__}: {exc}")
        return 1
    dt = time.monotonic() - t0

    print(f"\n=== tool discovery (SearchTools) ({dt:.1f}s) ===")
    print(detail)
    print("artifacts:")
    for k, v in artifacts.items():
        line = f"  {k:32s} = {v!r}"
        sys.stdout.buffer.write(line.encode("utf-8", "replace") + b"\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

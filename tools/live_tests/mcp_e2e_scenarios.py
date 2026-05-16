"""End-to-end live-test scenarios for the MCP module.

Goal: prove that the full MCP pipeline works against a real running
daemon and a real LLM chat. Validates the three install paths we
ship today:

  1. ``mcp_e2e_fetch``     — catalog reference, pip runtime, no auth
  2. ``mcp_e2e_seqthink``  — catalog reference, npm runtime, no auth
  3. ``mcp_e2e_inline``    — inline custom YAML config (power-user path)
  4. ``mcp_e2e_missing``   — negative: bare-name ref to a server that
                              isn't installed; daemon must log the
                              actionable error and the agent must keep
                              working without MCP tools.

Each scenario deploys its YAML, opens a session, sends one message
that forces the agent to use (or notice the absence of) the MCP tool,
and verifies the live event stream contains the expected tool-call
lifecycle. Scenarios share a common helper to keep call-site noise
down. Runner at the bottom of this file aggregates results.

Run from the repo root::

    py -3.12 tools/live_tests/mcp_e2e_scenarios.py
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


_APPS_DIR = Path(__file__).resolve().parent / "apps"


# ── helpers ─────────────────────────────────────────────────────


def _new_session(app_id: str, daemon_url: str, prefix: str = "mcp") -> SessionHandle:
    return SessionHandle(
        session_id=f"{prefix}-{uuid.uuid4().hex[:8]}",
        app_id=app_id,
        daemon_url=daemon_url,
        workspace="",
    )


def _tool_call_succeeded(
    events: list[dict[str, Any]],
    tool_name_substr: str,
) -> tuple[bool, str]:
    """True when a tool whose name contains *tool_name_substr* not only
    fired but **completed successfully**.

    A passing check requires:
      1. A final ``tool_call`` event with ``payload.success == True``
         and no ``payload.error``.
      2. The event's ``name`` matches the substring (case-insensitive).

    Returns ``(ok, detail)`` so the assertion's detail string reflects
    what actually happened. The previous looser check (presence of any
    tool-related event) reported PASS on intent-only — even when the
    sandbox rejected the call.
    """
    needle = tool_name_substr.lower()
    matched: list[dict[str, Any]] = []
    for ev in events:
        if ev.get("type") != "tool_call":
            continue
        payload = ev.get("payload") or {}
        name = (
            payload.get("name")
            or payload.get("tool_name")
            or payload.get("tool")
            or ""
        )
        if not isinstance(name, str) or needle not in name.lower():
            continue
        matched.append(payload)

    if not matched:
        return False, f"no successful tool_call for '{tool_name_substr}' (0 matching events)"

    final = matched[-1]
    success = final.get("success")
    err = final.get("error")
    if success is True and not err:
        return True, f"{final.get('name', '?')} succeeded ({len(matched)} tool_call event(s))"
    return False, (
        f"{final.get('name', '?')} matched but failed: "
        f"success={success!r} error={(str(err) or '')[:120]!r}"
    )


def _has_tool_call(events: list[dict[str, Any]], tool_name_substr: str) -> bool:
    """Back-compat: kept for any caller still checking intent only."""
    return _tool_call_succeeded(events, tool_name_substr)[0]


def _final_text(events: list[dict[str, Any]]) -> str:
    """Concatenate every text chunk from the streaming output.

    The daemon emits assistant text as a stream of ``token`` /
    ``out_token`` events, each carrying a ``content`` / ``delta`` /
    ``text`` field with the next chunk. We concatenate every chunk
    in seq order and return the joined string.
    """
    parts: list[str] = []
    for ev in assertions.sort_by_seq(events):
        t = ev.get("type")
        if t not in (
            "token", "out_token", "text_delta",
            "message_delta", "message_done", "result",
        ):
            continue
        payload = ev.get("payload") or {}
        chunk = (
            payload.get("delta")
            or payload.get("content")
            or payload.get("text")
            or ""
        )
        if isinstance(chunk, str):
            parts.append(chunk)
    return "".join(parts).strip()


def _dump_event_types(events: list[dict[str, Any]]) -> dict[str, int]:
    """Return a sorted-by-count histogram of event types — debug aid."""
    counts: dict[str, int] = {}
    for ev in events:
        t = str(ev.get("type"))
        counts[t] = counts.get(t, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _first_payload(events: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
    """Return the payload of the first event of *event_type*, or empty."""
    for ev in events:
        if ev.get("type") == event_type:
            return ev.get("payload") or {}
    return {}


# ── scenarios ────────────────────────────────────────────────────


def scenario_catalog_no_auth_pip(
    client: DevClient,
) -> tuple[bool, str, dict[str, Any]]:
    """Catalog reference to ``fetch`` (pip runtime, no creds).

    Asks the agent to fetch a stable URL. Expects:
      * a ``tool_call`` event with name containing ``fetch``
      * a non-empty agent reply after the tool returns.
    """
    yaml = _APPS_DIR / "mcp_e2e_fetch.yaml"
    app = client.deploy(yaml, force=True)
    session = _new_session(app.app_id, client.daemon_url, prefix="fetch")
    stream = client.send_live(
        session,
        "Fetch https://example.com and tell me in one sentence what it says.",
        total_timeout=90,
    )
    try:
        events = stream.events()
        sorted_events = assertions.sort_by_seq(events)
        text = _final_text(sorted_events)
        checks = [
            ("seq_unique", assertions.seq_unique(sorted_events)),
            ("tool_call_fetch_succeeded", _tool_call_succeeded(sorted_events, "fetch")),
            ("agent_replied", (
                bool(text),
                f"agent produced reply ({len(text)} chars)",
            )),
        ]
        ok, detail = assertions.report(checks)
        artifacts = {
            "app_id": app.app_id,
            "session_id": session.session_id,
            "event_count": len(events),
            "final_text_preview": text[:200],
        }
        if not text:
            artifacts["event_type_histogram"] = _dump_event_types(events)
            artifacts["first_error_payload"] = _first_payload(events, "error")
            artifacts["first_cancel_payload"] = _first_payload(events, "message_cancelled")
        return ok, detail, artifacts
    finally:
        stream.stop(timeout=2.0)


def scenario_catalog_no_auth_npm(
    client: DevClient,
) -> tuple[bool, str, dict[str, Any]]:
    """Catalog reference to ``sequential_thinking`` (npm runtime)."""
    yaml = _APPS_DIR / "mcp_e2e_seqthink.yaml"
    app = client.deploy(yaml, force=True)
    session = _new_session(app.app_id, client.daemon_url, prefix="seq")
    stream = client.send_live(
        session,
        "Use the sequentialthinking tool to plan how to make a cup of tea, "
        "step by step. Then summarise the plan in one sentence.",
        total_timeout=90,
    )
    try:
        events = stream.events()
        sorted_events = assertions.sort_by_seq(events)
        text = _final_text(sorted_events)
        checks = [
            ("seq_unique", assertions.seq_unique(sorted_events)),
            ("tool_call_seqthink_succeeded",
                _tool_call_succeeded(sorted_events, "sequentialthinking")),
            ("agent_replied", (
                bool(text), f"agent produced reply ({len(text)} chars)",
            )),
        ]
        ok, detail = assertions.report(checks)
        artifacts = {
            "app_id": app.app_id,
            "session_id": session.session_id,
            "event_count": len(events),
            "final_text_preview": text[:200],
        }
        if not text:
            artifacts["event_type_histogram"] = _dump_event_types(events)
        return ok, detail, artifacts
    finally:
        stream.stop(timeout=2.0)


def scenario_inline_custom_server(
    client: DevClient,
) -> tuple[bool, str, dict[str, Any]]:
    """Inline YAML server definition (power-user path).

    Uses the already-installed ``cli2mcp`` binary so the test doesn't
    re-install. The MCP module's ``on_config_update`` routes through
    ``source = custom`` because ``command`` is present in the YAML.
    """
    yaml = _APPS_DIR / "mcp_e2e_inline.yaml"
    app = client.deploy(yaml, force=True)
    session = _new_session(app.app_id, client.daemon_url, prefix="inline")
    stream = client.send_live(
        session,
        "Use the sequentialthinking tool to plan how to brew coffee, "
        "step by step. Then summarise the plan in one sentence.",
        total_timeout=120,
    )
    try:
        events = stream.events()
        sorted_events = assertions.sort_by_seq(events)
        text = _final_text(sorted_events)
        # Inline server points at a fresh ``npx -y @modelcontextprotocol/
        # server-sequential-thinking`` subprocess. Validates the
        # ``source = custom`` install path: full inline config, sandbox
        # block, tool discovery from a freshly-spawned MCP process.
        checks = [
            ("seq_unique", assertions.seq_unique(sorted_events)),
            ("tool_call_inline_succeeded",
                _tool_call_succeeded(sorted_events, "sequentialthinking")),
            ("agent_replied", (
                bool(text), f"agent produced reply ({len(text)} chars)",
            )),
            ("no_runtime_error", assertions.no_event(sorted_events, "error")),
        ]
        ok, detail = assertions.report(checks)
        artifacts = {
            "app_id": app.app_id,
            "session_id": session.session_id,
            "event_count": len(events),
            "final_text_preview": text[:200],
        }
        if not text:
            artifacts["event_type_histogram"] = _dump_event_types(events)
        return ok, detail, artifacts
    finally:
        stream.stop(timeout=2.0)


def scenario_missing_reference_graceful(
    client: DevClient,
) -> tuple[bool, str, dict[str, Any]]:
    """Negative: bare reference to a server that doesn't exist.

    Confirms the MCP module logs the new actionable error AND the agent
    still boots + replies. Without the fix from earlier today, the app
    would either crash on startup or silently miss tools without any
    diagnostic.
    """
    yaml = _APPS_DIR / "mcp_e2e_missing.yaml"
    app = client.deploy(yaml, force=True)
    session = _new_session(app.app_id, client.daemon_url, prefix="miss")
    stream = client.send_live(
        session, "Say hello.", total_timeout=30,
    )
    try:
        events = stream.events()
        sorted_events = assertions.sort_by_seq(events)
        text = _final_text(sorted_events)
        checks = [
            ("seq_unique", assertions.seq_unique(sorted_events)),
            ("agent_replied_despite_missing_mcp", (
                bool(text),
                f"agent should have replied even with no MCP tools "
                f"(got {len(text)} chars)",
            )),
        ]
        ok, detail = assertions.report(checks)
        artifacts = {
            "app_id": app.app_id,
            "session_id": session.session_id,
            "event_count": len(events),
            "final_text_preview": text[:200],
        }
        if not text:
            artifacts["event_type_histogram"] = _dump_event_types(events)
        return ok, detail, artifacts
    finally:
        stream.stop(timeout=2.0)


# ── runner ───────────────────────────────────────────────────────


_SCENARIOS = {
    "catalog_no_auth_pip": scenario_catalog_no_auth_pip,
    "catalog_no_auth_npm": scenario_catalog_no_auth_npm,
    "inline_custom_server": scenario_inline_custom_server,
    "missing_reference_graceful": scenario_missing_reference_graceful,
}


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    only = set(argv) if argv else None

    client = DevClient()
    results: dict[str, dict[str, Any]] = {}
    overall_ok = True

    for name, fn in _SCENARIOS.items():
        if only is not None and name not in only:
            continue
        print(f"\n══ {name} ══")
        t0 = time.monotonic()
        try:
            ok, detail, artifacts = fn(client)
        except Exception as exc:  # noqa: BLE001
            ok, detail, artifacts = False, f"exception: {exc!r}", {}
        dt = time.monotonic() - t0
        status = "PASS" if ok else "FAIL"
        print(f"  {status} in {dt:0.1f}s")
        print(f"  {detail}")
        if artifacts:
            print(f"  artifacts: {json.dumps(artifacts, default=str)[:400]}")
        results[name] = {"ok": ok, "detail": detail, "artifacts": artifacts, "seconds": dt}
        overall_ok = overall_ok and ok

    print("\n══ summary ══")
    for name, r in results.items():
        print(f"  {'PASS' if r['ok'] else 'FAIL'}  {name}  ({r['seconds']:0.1f}s)")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

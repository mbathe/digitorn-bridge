"""Concrete proof of three claims I made about the MCP scenarios.

Not a runner — a side-by-side comparison that dumps the raw events
from both list and dict deploy paths, plus the actual return values of
the assertion helpers. Run from repo root::

    py -3.12 tools/live_tests/_mcp_proof.py
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


_APPS = Path(__file__).resolve().parent / "apps"


def _session(app_id: str, daemon_url: str, prefix: str) -> SessionHandle:
    return SessionHandle(
        session_id=f"{prefix}-{uuid.uuid4().hex[:8]}",
        app_id=app_id, daemon_url=daemon_url, workspace="",
    )


def _tool_events_summary(
    events: list[dict],
) -> tuple[int, list[dict]]:
    """Return (count, payloads) for tool-related events."""
    tool_evs = []
    for ev in events:
        t = ev.get("type")
        if t in (
            "tool_call", "tool_call_streaming", "tool_start",
            "tool_result", "tool_end",
        ):
            tool_evs.append({
                "type": t,
                "seq": ev.get("seq"),
                "payload_keys": sorted(list((ev.get("payload") or {}).keys())),
                "name": (ev.get("payload") or {}).get("name")
                    or (ev.get("payload") or {}).get("tool_name")
                    or (ev.get("payload") or {}).get("tool"),
            })
    return len(tool_evs), tool_evs


def _has_tool_call(events: list[dict], substr: str) -> bool:
    needle = substr.lower()
    for ev in events:
        if ev.get("type") not in (
            "tool_call", "tool_call_streaming", "tool_start",
            "tool_result", "tool_end",
        ):
            continue
        payload = ev.get("payload") or {}
        name = (
            payload.get("name")
            or payload.get("tool_name")
            or payload.get("tool")
            or ""
        )
        if isinstance(name, str) and needle in name.lower():
            return True
    return False


def _final_text(events: list[dict]) -> str:
    parts = []
    for ev in assertions.sort_by_seq(events):
        if ev.get("type") not in (
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


def _run(client: DevClient, yaml_name: str, prompt: str, prefix: str) -> dict:
    """Deploy + send + return raw events + summary, do NOT assert."""
    app = client.deploy(_APPS / yaml_name, force=True)
    session = _session(app.app_id, client.daemon_url, prefix)
    stream = client.send_live(session, prompt, total_timeout=120)
    try:
        events = stream.events()
        tool_count, tool_payloads = _tool_events_summary(events)
        return {
            "app_id": app.app_id,
            "session_id": session.session_id,
            "total_events": len(events),
            "tool_event_count": tool_count,
            "tool_payloads": tool_payloads,
            "final_text": _final_text(events),
            "has_fetch_tool_call": _has_tool_call(events, "fetch"),
            "has_cli2mcp_tool_call": _has_tool_call(events, "cli2mcp"),
            "has_callcli_tool_call": _has_tool_call(events, "call_cli"),
            "has_sequentialthinking_tool_call": _has_tool_call(events, "sequentialthinking"),
            "histogram": _histogram(events),
        }
    finally:
        stream.stop(timeout=2.0)


def _histogram(events: list[dict]) -> dict[str, int]:
    h = {}
    for ev in events:
        t = str(ev.get("type"))
        h[t] = h.get(t, 0) + 1
    return dict(sorted(h.items(), key=lambda kv: -kv[1]))


def main() -> int:
    client = DevClient()
    print("=" * 70)
    print("CLAIM #1: list form (no sandbox) blocks tool execution at runtime")
    print("=" * 70)
    prompt_fetch = "Fetch https://example.com and tell me its title."

    print("\n--- run A: list-form fetch (no explicit sandbox) ---")
    a = _run(client, "mcp_e2e_fetch.yaml", prompt_fetch, "proofA")
    print(f"total_events: {a['total_events']}")
    print(f"tool_event_count: {a['tool_event_count']}")
    print(f"has_fetch_tool_call: {a['has_fetch_tool_call']}")
    print(f"tool_payloads: {json.dumps(a['tool_payloads'], indent=2)[:1200]}")
    print(f"agent_text[:300]: {a['final_text'][:300]!r}")

    print("\n--- run B: dict-form fetch with explicit sandbox ---")
    b = _run(client, "mcp_e2e_fetch_dict.yaml", prompt_fetch, "proofB")
    print(f"total_events: {b['total_events']}")
    print(f"tool_event_count: {b['tool_event_count']}")
    print(f"has_fetch_tool_call: {b['has_fetch_tool_call']}")
    print(f"tool_payloads: {json.dumps(b['tool_payloads'], indent=2)[:1200]}")
    print(f"agent_text[:300]: {b['final_text'][:300]!r}")

    print("\nCLAIM #1 verdict:")
    if a["has_fetch_tool_call"] and not b["has_fetch_tool_call"]:
        print(
            "  CONTRADICTED: list-form actually triggered a tool_call event but "
            "dict-form did not — opposite of my speculation."
        )
    elif not a["has_fetch_tool_call"] and not b["has_fetch_tool_call"]:
        print(
            "  PARTIAL: neither form triggered a real tool_call. The bug is "
            "elsewhere (schema, registration, or LLM prompt)."
        )
    elif a["has_fetch_tool_call"] and b["has_fetch_tool_call"]:
        print("  SAME BEHAVIOR: both forms emit tool_call events. The sandbox claim is wrong.")
    else:
        print(
            "  CONFIRMED: dict+sandbox triggers tool_call, list does NOT. "
            "Auto-inject sandbox for list-form is the right fix."
        )

    print("\n" + "=" * 70)
    print("CLAIM #2: inline scenario doesn't really invoke cli2mcp")
    print("=" * 70)
    prompt_inline = (
        "Use the sequentialthinking tool to plan how to make a cup of coffee, "
        "step by step. Then summarise the plan in one sentence."
    )
    c = _run(client, "mcp_e2e_inline.yaml", prompt_inline, "proofC")
    print(f"total_events: {c['total_events']}")
    print(f"tool_event_count: {c['tool_event_count']}")
    print(f"has_sequentialthinking_tool_call: {c['has_sequentialthinking_tool_call']}")
    print(f"tool_payloads: {json.dumps(c['tool_payloads'], indent=2)[:1200]}")
    print(f"agent_text[:300]: {c['final_text'][:300]!r}")

    print("\nCLAIM #2 verdict:")
    if c["has_sequentialthinking_tool_call"]:
        print(
            "  CONTRADICTED: the inline server CAN expose tools to the agent "
            "when the binary is reachable. The original failure was about "
            "cli2mcp being installed via uvx (no PATH entry), NOT about the "
            "inline path itself being broken."
        )
    else:
        print(
            "  CONFIRMED: even with a working binary and an explicit prompt, "
            "the inline server tools never reach the agent's catalog."
        )

    print("\n" + "=" * 70)
    print("CLAIM #3: tool_call_present check is too permissive")
    print("=" * 70)
    # Re-use run A (list-form fetch) and the seqthink prompt sample.
    print("From run A (list-form fetch):")
    print(f"  _has_tool_call(events, 'fetch') = {a['has_fetch_tool_call']}")
    print(f"  agent_text mentions sandbox? {'sandbox' in a['final_text'].lower()}")
    print(f"  agent_text mentions 'example domain'? {'example domain' in a['final_text'].lower()}")
    print("\nCLAIM #3 verdict:")
    if a["has_fetch_tool_call"] and "sandbox" in a["final_text"].lower():
        print(
            "  CONFIRMED: tool_call event emitted (LLM intent to call) but the "
            "agent's own text says the tool was blocked. The PASS check fires "
            "on intent, not successful execution. Need a tool_result-aware "
            "assertion."
        )
    elif a["has_fetch_tool_call"] and a["final_text"] and "example domain" in a["final_text"].lower():
        print(
            "  CONTRADICTED: tool_call emitted AND agent saw the real content. "
            "The tool succeeded; my pessimism was wrong."
        )
    else:
        print("  INCONCLUSIVE — see the raw output above and judge for yourself.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

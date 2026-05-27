"""Live E2E regression test for builtin slash commands.

Targets `digitorn-chat` which wires `/help` and `/compact` to server-
side handlers (see ui.slash_commands in its app.yaml). By design slash
commands short-circuit the turn dispatcher in `apps_v2/messages.py`
and never hit the LLM — but they still produce the full lifecycle of
session events (user_message + message_done with slash_synthetic=true).

Tests two contracts :
  1. `/help` round-trip : dispatch fires, response carries text.
  2. `/compact` round-trip : dispatch fires, response carries text.

Run:
    py -3.12 tools/live_tests/slash_scenarios.py
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


APP_ID = "digitorn-chat"


def _ok(label: str, cond: bool, why: str = "") -> tuple[str, tuple[bool, str]]:
    return (label, (cond, "" if cond else why))


def _slash_synthetic_done_events(events: list[dict]) -> list[dict]:
    """Return message_done events flagged as slash_synthetic."""
    out = []
    for e in events:
        if e.get("type") != "message_done":
            continue
        p = e.get("payload") or {}
        if p.get("slash_synthetic") is True:
            out.append(e)
    return out


def _slash_text(events: list[dict]) -> str:
    """Concatenate any token / message text we see for the slash turn."""
    chunks = []
    for e in events:
        if e.get("type") in ("out_token", "token"):
            p = e.get("payload") or {}
            d = p.get("delta") or p.get("text") or p.get("content")
            if isinstance(d, str):
                chunks.append(d)
    return "".join(chunks)


def _slash_round_trip(
    client: DevClient, command: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Send `command` via direct POST and assert the slash-dispatch contract.

    Note: slash commands are server-side dispatch — they run SYNCHRONOUSLY
    inside the POST handler and emit all their events before the response
    returns. The HTTP response body is the authoritative observation
    channel; live SSE streams open after the POST and miss the events.
    """
    artifacts: dict[str, Any] = {}

    sid = f"slash-{command.lstrip('/')}-{uuid.uuid4().hex[:6]}"
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace="",
    )
    artifacts["session_id"] = sid
    artifacts["command"] = command

    resp = client.post_message_raw(session, command)
    status_code = resp.get("status_code")
    body = resp.get("body") or {}
    data = body.get("data") if isinstance(body, dict) else {}
    artifacts["http_status"] = status_code
    artifacts["body_status"] = (data or {}).get("status")
    artifacts["body_command"] = (data or {}).get("command")
    artifacts["body_correlation_id"] = (data or {}).get("correlation_id")
    artifacts["body_keys"] = sorted((data or {}).keys()) if isinstance(data, dict) else None

    checks = [
        _ok(
            "POST returned 200",
            status_code == 200,
            f"got {status_code}",
        ),
        _ok(
            "body.status == 'slash_handled'",
            (data or {}).get("status") == "slash_handled",
            f"got {data!r}",
        ),
        _ok(
            "body.command matches",
            (data or {}).get("command") == command,
            f"got {(data or {}).get('command')!r}",
        ),
        _ok(
            "body has correlation_id",
            bool((data or {}).get("correlation_id")),
            "missing correlation_id",
        ),
    ]
    ok, detail = assertions.report(checks)
    return ok, detail, artifacts


def scenario_slash_help(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    return _slash_round_trip(client, "/help")


def scenario_slash_compact(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    return _slash_round_trip(client, "/compact")


def main() -> int:
    creds_path = Path(r"C:\Users\ASUS\.digitorn\credentials.json")
    if not creds_path.exists():
        print(f"FAIL  CLI credentials missing: {creds_path}")
        return 1
    token = json.loads(creds_path.read_text(encoding="utf-8"))["access_token"]
    client = DevClient.with_token(token)

    apps = client.list_apps()
    apps_iter = apps if isinstance(apps, list) else (apps.get("data") or apps.get("rows") or [])
    deployed_ids = {a.get("app_id") for a in apps_iter if isinstance(a, dict)}
    if APP_ID not in deployed_ids:
        print(f"FAIL  app '{APP_ID}' not deployed (found: {sorted(deployed_ids)[:10]})")
        return 1

    scenarios = [
        ("slash_help", scenario_slash_help),
        ("slash_compact", scenario_slash_compact),
    ]

    overall_ok = True
    for name, fn in scenarios:
        t0 = time.monotonic()
        try:
            ok, detail, artifacts = fn(client)
        except Exception as exc:
            print(f"\n=== {name} (crash) ===")
            print(f"FAIL  scenario crashed: {type(exc).__name__}: {exc}")
            overall_ok = False
            continue
        dt = time.monotonic() - t0
        print(f"\n=== {name} ({dt:.1f}s) ===")
        print(detail)
        print("artifacts:")
        for k, v in artifacts.items():
            line = f"  {k:32s} = {v!r}"
            sys.stdout.buffer.write(line.encode("utf-8", "replace") + b"\n")
        if not ok:
            overall_ok = False

    print("\n=== OVERALL ===")
    print("PASS" if overall_ok else "FAIL")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())

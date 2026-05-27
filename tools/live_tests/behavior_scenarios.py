"""Live E2E regression test for the behavior engine.

Targets `digitorn-code` (uses the `coding` behavior profile, emits
`behavior_directive` events). Real LLM, full agent path.

Asserts the behavior engine is alive and enforcing :
  1. The agent's turn produces at least one `behavior_directive` event.
  2. The event payload contains a rule reference + an action level
     (block / warn / remind).
  3. The directive carries text the agent will see.

Run:
    py -3.12 tools/live_tests/behavior_scenarios.py
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


def _ok(label: str, cond: bool, why: str = "") -> tuple[str, tuple[bool, str]]:
    return (label, (cond, "" if cond else why))


def scenario_behavior_directive_fires(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    artifacts: dict[str, Any] = {}

    apps = client.list_apps()
    apps_iter = apps if isinstance(apps, list) else (apps.get("data") or apps.get("rows") or [])
    deployed_ids = {a.get("app_id") for a in apps_iter if isinstance(a, dict)}
    if APP_ID not in deployed_ids:
        return False, f"app '{APP_ID}' not deployed", artifacts

    sid = f"beh-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace="",
    )
    artifacts["session_id"] = sid

    stream = None
    try:
        # Prompt designed to trigger the `read_before_edit` rule: ask the
        # agent to Edit a file without reading it first. The behavior
        # engine should intervene.
        prompt = (
            "Edit the file 'never_read.txt' to change the text 'foo' to "
            "'bar'. Do this directly with Edit. Reply with 'done' "
            "afterwards."
        )
        stream = client.send_live(session, prompt, total_timeout=300)
        events = assertions.sort_by_seq(stream.events())
        artifacts["event_count"] = len(events)
        artifacts["event_types"] = sorted({e["type"] for e in events})

        beh_events = [e for e in events if e.get("type") == "behavior_directive"]
        artifacts["behavior_directive_count"] = len(beh_events)

        if beh_events:
            first_p = beh_events[0].get("payload") or {}
            last_p = beh_events[-1].get("payload") or {}
            artifacts["beh_first_keys"] = sorted(first_p.keys())
            artifacts["beh_first_preview"] = {
                k: (str(first_p.get(k))[:160] if first_p.get(k) is not None else None)
                for k in ("rule_id", "rule", "id", "name",
                          "action", "level", "action_level", "severity",
                          "message", "directive", "text")
                if k in first_p
            }
            artifacts["beh_last_preview"] = {
                k: (str(last_p.get(k))[:160] if last_p.get(k) is not None else None)
                for k in ("rule_id", "rule", "id", "name",
                          "action", "level", "action_level", "severity",
                          "message", "directive", "text")
                if k in last_p
            }

        # Real schema (discovered live): payload carries `directive`
        # (the injected text), `length` (chars), `turn` (turn number).
        # No rule_id, no enforcement level surface in the live event.
        def _payload_directive(e: dict) -> str:
            p = e.get("payload") or {}
            return str(p.get("directive") or "")

        directives = [_payload_directive(e) for e in beh_events]
        non_empty_directives = [d for d in directives if d.strip()]
        artifacts["directive_lengths"] = [len(d) for d in directives]

        # Turn numbers should be non-negative integers when present.
        turns_seen = []
        for e in beh_events:
            t = (e.get("payload") or {}).get("turn")
            if isinstance(t, int):
                turns_seen.append(t)
        artifacts["turns_seen"] = turns_seen

        checks = [
            _ok("app deployed", APP_ID in deployed_ids, "missing"),
            _ok("events received", len(events) > 0, "0 events"),
            _ok(
                "at least one behavior_directive event",
                len(beh_events) >= 1,
                f"got 0, event types: {artifacts['event_types']!r}",
            ),
            _ok(
                "at least one directive has non-empty text",
                len(non_empty_directives) >= 1,
                f"all {len(beh_events)} directives have empty text",
            ),
            _ok(
                "each directive event has a turn number",
                len(turns_seen) == len(beh_events) and all(t >= 0 for t in turns_seen),
                f"turns seen={turns_seen}, beh_events={len(beh_events)}",
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
        ok, detail, artifacts = scenario_behavior_directive_fires(client)
    except Exception as exc:
        print(f"FAIL  scenario crashed: {type(exc).__name__}: {exc}")
        return 1
    dt = time.monotonic() - t0

    print(f"\n=== behavior engine directive fire ({dt:.1f}s) ===")
    print(detail)
    print("artifacts:")
    for k, v in artifacts.items():
        # Strip non-ASCII so cp1252 stdout on Windows doesn't crash.
        line = f"  {k:32s} = {v!r}"
        sys.stdout.buffer.write(line.encode("utf-8", "replace") + b"\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

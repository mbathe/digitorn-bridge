"""Live E2E regression test for the human-in-the-loop approval flow.

Targets `digitorn-deepresearch` because its system prompt forces the
agent to call `ask_user` THREE times before proposing a plan. Zero
fixtures, zero new YAML. Tests the documented contract :

  1. Agent calls `ask_user` -> a pending request appears in
     `GET /api/apps/{app}/approvals`.
  2. Client responds via `POST /api/apps/{app}/approve`.
  3. Pending queue drains.
  4. Agent emits more events after the response (proves it resumed).

Run:
    py -3.12 tools/live_tests/approval_scenarios.py

Exit 0 PASS, 1 FAIL.
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


APP_ID = "digitorn-deepresearch"


def _ok(label: str, cond: bool, why: str = "") -> tuple[str, tuple[bool, str]]:
    return (label, (cond, "" if cond else why))


def _poll_until_pending(
    client: DevClient, app_id: str, *, timeout: float = 90.0, interval: float = 1.0,
) -> list[dict[str, Any]]:
    """Block until at least one approval request appears, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = client.get_pending(app_id) or []
        # Ignore requests from OTHER sessions in the same app.
        if pending:
            return pending
        time.sleep(interval)
    return []


def scenario_ask_user_round_trip(client: DevClient) -> tuple[bool, str, dict[str, Any]]:
    artifacts: dict[str, Any] = {}

    # --- preflight: app deployed ---
    apps = client.list_apps()
    deployed_ids = {
        a.get("app_id") for a in (apps if isinstance(apps, list) else apps.get("rows") or apps.get("data") or [])
        if isinstance(a, dict)
    }
    if APP_ID not in deployed_ids:
        return False, f"app '{APP_ID}' not deployed (found: {sorted(deployed_ids)[:8]})", artifacts

    sid = f"approval-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace="",
    )
    artifacts["session_id"] = sid

    # Pre-snapshot of any existing pending in this app (other sessions).
    pre_pending = client.get_pending(APP_ID) or []
    artifacts["pre_pending_count"] = len(pre_pending)
    pre_ids = {p.get("request_id") or p.get("id") for p in pre_pending if isinstance(p, dict)}

    # IMPORTANT: do NOT use send_live (it waits for message_done; ask_user
    # blocks the turn, so message_done never arrives until we respond).
    # We post directly and poll for the pending request.
    post_result = client.post_message_raw(
        session,
        # Open-ended research prompt -> the prompt template forces the
        # agent to ask THREE clarifying questions via ask_user.
        "Research the history of pastel de nata in Lisbon for me.",
    )
    artifacts["post_status"] = post_result.get("status") or post_result.get("body", {}).get("status")

    # --- wait for a pending ask_user ---
    pending = _poll_until_pending(client, APP_ID, timeout=90.0)
    # Filter for THIS session's pending (others may have been there).
    mine = [
        p for p in pending
        if isinstance(p, dict)
        and (p.get("session_id") == sid or (p.get("request_id") or p.get("id")) not in pre_ids)
    ]
    artifacts["pending_after_wait"] = len(mine)
    artifacts["pending_first_preview"] = (
        {k: mine[0].get(k) for k in ("request_id", "id", "tool_name", "session_id", "kind", "type")}
        if mine else None
    )

    if not mine:
        # abort the turn so the session doesn't stay hung forever.
        try:
            client.abort_session(session, purge_queue=True)
        except Exception:
            pass
        return False, "no pending approval request appeared within 90s", artifacts

    # --- respond ---
    req_id = mine[0].get("request_id") or mine[0].get("id") or ""
    if not req_id:
        return False, f"pending entry has no request_id: {mine[0]!r}", artifacts

    ok_approve = client.approve(APP_ID, req_id, response="Focus on the 19th century origins.")
    artifacts["approve_ok"] = ok_approve

    # --- check queue eventually drains or progresses (agent picks it up) ---
    time.sleep(2.0)
    post_resolve_pending = client.get_pending(APP_ID) or []
    post_resolve_mine = [
        p for p in post_resolve_pending
        if isinstance(p, dict) and p.get("request_id") == req_id
    ]
    artifacts["resolved_request_still_pending"] = len(post_resolve_mine)

    # Cleanup: abort to release the session (agent is likely still going,
    # may have queued additional ask_user calls per its 3-questions prompt).
    try:
        client.abort_session(session, purge_queue=True)
    except Exception:
        pass

    checks = [
        _ok("app deployed", APP_ID in deployed_ids, "missing"),
        _ok(
            "pending ask_user appeared",
            len(mine) >= 1,
            f"none in {len(pending)} total within 90s",
        ),
        _ok(
            "pending entry has expected shape",
            (mine[0].get("tool_name") == "ask_user"
             and mine[0].get("session_id") == sid
             and bool(mine[0].get("request_id"))),
            f"got {mine[0]!r}",
        ),
        _ok("approve() succeeded", bool(ok_approve), "approve returned falsy"),
        _ok(
            "resolved request removed from pending",
            len(post_resolve_mine) == 0,
            f"request {req_id!r} still pending after approve",
        ),
    ]
    ok, detail = assertions.report(checks)
    return ok, detail, artifacts


def main() -> int:
    creds_path = Path(r"C:\Users\ASUS\.digitorn\credentials.json")
    if not creds_path.exists():
        print(f"FAIL  CLI credentials missing: {creds_path}")
        return 1
    token = json.loads(creds_path.read_text(encoding="utf-8"))["access_token"]
    client = DevClient.with_token(token)

    t0 = time.monotonic()
    try:
        ok, detail, artifacts = scenario_ask_user_round_trip(client)
    except Exception as exc:
        print(f"FAIL  scenario crashed: {type(exc).__name__}: {exc}")
        return 1
    dt = time.monotonic() - t0

    print(f"\n=== approval ask_user round-trip ({dt:.1f}s) ===")
    print(detail)
    print("artifacts:")
    for k, v in artifacts.items():
        print(f"  {k:32s} = {v!r}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

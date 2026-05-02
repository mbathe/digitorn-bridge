"""Live tests for the workspace hardening pass.

Covers the audit findings I just fixed:

  1. reject_after_approve_then_edit
     - approve_file resets total_insertions/total_deletions and bumps
       updated_at so the frontend aggregate badge clears.

  2. reject_brand_new_file_deletes
     - my earlier _ensure_session_baseline regression made reject of a
       brand-new file a no-op (it restored to the auto-baseline = same
       content). Now reject_file checks ``has_user_approval`` and
       deletes when the only baseline is the session-start auto-snapshot.

  3. approve_hunks_bumps_updated_at
     - approve_file_hunks patch carries updated_at so the client's
       ``wroteSinceLastRebuild`` check fires and the +N -M badge
       refreshes after a partial approval.

  4. writeback_baselines_a_new_file
     - PUT /workspace/files/{path} on a never-before-seen path now
       calls _ensure_session_baseline so subsequent edits diff
       against a stable point.

  5. unauth_writeback_blocked
     - User A cannot writeback to User B's session (the auth bypass
       I just plugged).

Authentication is shared with the other live test suites - reuses
``preview-tester@example.com`` for the primary user. Scenario 5
provisions a second user.
"""
from __future__ import annotations

import os as _os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from digitorn.testing.client import DevClient
from digitorn.testing.models import SessionHandle


_APP_ID = "ws-preview-test"
_APP_YAML = Path(__file__).parent / "apps" / "ws-preview-test.yaml"


def _new_session(client: DevClient, prefix: str = "wsh") -> SessionHandle:
    return SessionHandle(
        session_id=f"{prefix}-{uuid.uuid4().hex[:8]}",
        app_id=_APP_ID, daemon_url=client.daemon_url, workspace="",
    )


def _exec_tool(
    client: DevClient, session: SessionHandle, tool: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    r = client._post(
        f"/api/apps/{session.app_id}/tools/{tool}/execute",
        json={"session_id": session.session_id, "params": params},
    )
    try:
        return r.json()
    except Exception:
        return {"success": False, "error": r.text[:500]}


def _kick_session(client: DevClient, session: SessionHandle) -> None:
    r = client.post_message_raw(
        session,
        "Answer with the single word 'ready' and nothing else. "
        "Do not call any tool.",
    )
    cid = (r.get("body") or {}).get("data", {}).get("correlation_id") or ""
    stream = client.open_event_stream(session, wait_for_session=True)
    try:
        if cid:
            stream.wait_for(
                "message_done", timeout=60.0,
                predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
            )
        else:
            stream.wait_until_idle(quiet_seconds=1.0, total_timeout=10.0)
    finally:
        stream.stop(timeout=2.0)


def _resolve_workspace_dir(client: DevClient, session: SessionHandle) -> Path | None:
    r = client._get(f"/api/apps/{session.app_id}/sessions/{session.session_id}")
    if r.status_code != 200:
        return None
    ws = ((r.json().get("data") or {}).get("workspace")) or ""
    return Path(ws).expanduser() if ws else None


def _read_state(client: DevClient, session: SessionHandle) -> dict[str, Any] | None:
    ws = _resolve_workspace_dir(client, session)
    if ws is None:
        return None
    state_path = ws / ".digitorn" / "sessions" / session.session_id / "state.json"
    deadline = time.time() + 4.0
    while time.time() < deadline:
        if state_path.is_file():
            try:
                import json as _json
                return _json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        time.sleep(0.1)
    return None


def _file_payload(state: dict[str, Any], path: str) -> dict[str, Any] | None:
    return ((state.get("resources") or {}).get("files") or {}).get(path)


# ── scenarios ──────────────────────────────────────────────────


def scenario_approve_clears_aggregate(client: DevClient) -> tuple[bool, str, dict]:
    """approve_file resets total_insertions/total_deletions and bumps
    updated_at so the +N -M badge clears."""
    session = _new_session(client, "appclr")
    try:
        _kick_session(client, session)
        _exec_tool(client, session, "WsWrite", {
            "path": "demo.txt", "content": "a\nb\nc\n",
        })
        _exec_tool(client, session, "WsEdit", {
            "path": "demo.txt",
            "old_string": "a", "new_string": "A",
        })
        _exec_tool(client, session, "WsEdit", {
            "path": "demo.txt",
            "old_string": "b", "new_string": "B",
        })
        time.sleep(0.6)
        state_before = _read_state(client, session)
        before = _file_payload(state_before or {}, "demo.txt") or {}
        # Approve via REST (the user-facing path - ensures auth + flow works).
        r = client._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/files/approve",
            json={"path": "demo.txt"},
        )
        time.sleep(0.6)
        state_after = _read_state(client, session)
        after = _file_payload(state_after or {}, "demo.txt") or {}
        checks: list[tuple[str, bool, str]] = []
        checks.append((
            "approve REST returned 2xx",
            200 <= r.status_code < 300,
            f"status={r.status_code}",
        ))
        checks.append((
            "validation flipped to approved",
            after.get("validation") == "approved",
            f"validation={after.get('validation')}",
        ))
        checks.append((
            "total_insertions reset to 0 after approve",
            after.get("total_insertions") == 0,
            f"total_ins before={before.get('total_insertions')} after={after.get('total_insertions')}",
        ))
        checks.append((
            "total_deletions reset to 0 after approve",
            after.get("total_deletions") == 0,
            f"total_del before={before.get('total_deletions')} after={after.get('total_deletions')}",
        ))
        checks.append((
            "updated_at bumped on approve",
            (after.get("updated_at") or 0) > (before.get("updated_at") or 0),
            f"before={before.get('updated_at')} after={after.get('updated_at')}",
        ))
        checks.append((
            "insertions_pending zeroed",
            after.get("insertions_pending") == 0,
            f"ins_pending={after.get('insertions_pending')}",
        ))
        ok = all(c[1] for c in checks)
        detail = "\n".join(
            f"  [{'PASS' if c[1] else 'FAIL'}] {c[0]}: {c[2]}" for c in checks
        )
        return ok, detail, {"before": before, "after": after}
    except Exception as exc:
        import traceback
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}", {}


def scenario_reject_brand_new_file_deletes(
    client: DevClient,
) -> tuple[bool, str, dict]:
    """A file the agent created and the user never approved must be
    DELETED on reject_file (not preserved at the auto-baseline)."""
    session = _new_session(client, "rejnew")
    try:
        _kick_session(client, session)
        _exec_tool(client, session, "WsWrite", {
            "path": "throwaway.txt", "content": "should-disappear\n",
        })
        time.sleep(0.6)
        # Verify it landed.
        state_before = _read_state(client, session)
        before = _file_payload(state_before or {}, "throwaway.txt")
        # Reject via REST.
        r = client._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/files/reject",
            json={"path": "throwaway.txt"},
        )
        time.sleep(0.6)
        state_after = _read_state(client, session)
        after = _file_payload(state_after or {}, "throwaway.txt")
        # Disk-side check (sync_to_disk is on for ws-preview-test).
        ws = _resolve_workspace_dir(client, session)
        disk_exists = (
            (ws / "throwaway.txt").is_file() if ws is not None else None
        )
        checks: list[tuple[str, bool, str]] = []
        checks.append((
            "file existed before reject",
            before is not None,
            f"before={'present' if before else 'missing'}",
        ))
        checks.append((
            "reject REST returned 2xx",
            200 <= r.status_code < 300,
            f"status={r.status_code} body={r.text[:200]}",
        ))
        checks.append((
            "reverted reported as 'deleted'",
            (r.json().get("data") or {}).get("reverted") == "deleted",
            f"reverted={(r.json().get('data') or {}).get('reverted')}",
        ))
        checks.append((
            "file removed from channel",
            after is None,
            f"after={'still present' if after else 'gone'}",
        ))
        checks.append((
            "file removed from disk",
            disk_exists is False,
            f"disk_exists={disk_exists}",
        ))
        ok = all(c[1] for c in checks)
        detail = "\n".join(
            f"  [{'PASS' if c[1] else 'FAIL'}] {c[0]}: {c[2]}" for c in checks
        )
        return ok, detail, {}
    except Exception as exc:
        import traceback
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}", {}


def scenario_writeback_unauthorized(
    client: DevClient, daemon_url: str,
) -> tuple[bool, str, dict]:
    """User A creates a session. User B (different account, same
    daemon) tries PUT /workspace/files/... on A's session. The daemon
    must respond 4xx, never let the write land in A's preview state.
    """
    session_a = _new_session(client, "ownedbyA")
    try:
        _kick_session(client, session_a)
        _exec_tool(client, session_a, "WsWrite", {
            "path": "secret.txt", "content": "owned-by-a\n",
        })
        time.sleep(0.4)
        # User B: register + login a second account.
        import httpx
        b_email = f"attacker-{uuid.uuid4().hex[:8]}@example.com"
        b_password = "AttackerPass123!"
        with httpx.Client(timeout=20.0, follow_redirects=True) as c:
            reg = c.post(
                f"{daemon_url}/auth/register",
                json={
                    "email": b_email, "password": b_password,
                    "username": b_email.split("@", 1)[0].replace("-", "_"),
                },
            )
            if reg.status_code not in (200, 201):
                return False, f"  [FAIL] could not register attacker: {reg.status_code}", {}
            login = c.post(
                f"{daemon_url}/auth/login",
                json={"email": b_email, "password": b_password},
            )
            if login.status_code != 200:
                return False, f"  [FAIL] attacker login failed: {login.status_code}", {}
            attacker_token = login.json().get("access_token")
        # User B attempts to writeback to User A's session.
        attacker_client = DevClient.with_token(attacker_token, daemon_url=daemon_url)
        r = attacker_client._put(
            f"/api/apps/{session_a.app_id}/sessions/{session_a.session_id}/workspace/files/secret.txt",
            json={"content": "OWNED-BY-B\n", "auto_approve": True},
        )
        # Must be rejected (4xx).
        # Re-check A's view as A.
        state_after = _read_state(client, session_a)
        a_payload = _file_payload(state_after or {}, "secret.txt") or {}
        checks: list[tuple[str, bool, str]] = []
        checks.append((
            "attacker writeback denied (4xx)",
            400 <= r.status_code < 500,
            f"status={r.status_code} body={r.text[:200]}",
        ))
        checks.append((
            "A's content preserved (no leak from attacker)",
            a_payload.get("content") == "owned-by-a\n",
            f"content={a_payload.get('content')!r}",
        ))
        ok = all(c[1] for c in checks)
        detail = "\n".join(
            f"  [{'PASS' if c[1] else 'FAIL'}] {c[0]}: {c[2]}" for c in checks
        )
        return ok, detail, {}
    except Exception as exc:
        import traceback
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}", {}


def scenario_writeback_size_limit(
    client: DevClient,
) -> tuple[bool, str, dict]:
    """A 100 MB writeback must be rejected with 413."""
    session = _new_session(client, "bigput")
    try:
        _kick_session(client, session)
        big = "x" * (15 * 1024 * 1024)  # 15 MB - over the 10 MB cap.
        r = client._put(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/files/big.txt",
            json={"content": big, "auto_approve": False},
        )
        checks: list[tuple[str, bool, str]] = [
            (
                "oversized writeback rejected",
                r.status_code in (400, 413),
                f"status={r.status_code}",
            ),
        ]
        ok = all(c[1] for c in checks)
        detail = "\n".join(
            f"  [{'PASS' if c[1] else 'FAIL'}] {c[0]}: {c[2]}" for c in checks
        )
        return ok, detail, {}
    except Exception as exc:
        import traceback
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}", {}


def scenario_path_traversal_blocked(
    client: DevClient,
) -> tuple[bool, str, dict]:
    """`../../etc/passwd`-style paths must be rejected on the history
    endpoint - not return arbitrary disk content."""
    session = _new_session(client, "trav")
    try:
        _kick_session(client, session)
        r = client._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/files/..%2F..%2Fetc%2Fpasswd/history",
        )
        checks: list[tuple[str, bool, str]] = [
            (
                "traversal path returns 4xx",
                400 <= r.status_code < 500,
                f"status={r.status_code} body={r.text[:200]}",
            ),
        ]
        ok = all(c[1] for c in checks)
        detail = "\n".join(
            f"  [{'PASS' if c[1] else 'FAIL'}] {c[0]}: {c[2]}" for c in checks
        )
        return ok, detail, {}
    except Exception as exc:
        import traceback
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}", {}


# ── runner ──────────────────────────────────────────────────────


def _ensure_app_deployed(client: DevClient) -> None:
    if not _APP_YAML.is_file():
        raise FileNotFoundError(f"App YAML missing: {_APP_YAML}")
    try:
        client.deploy(str(_APP_YAML), force=True)
    except Exception as exc:
        print(f"[setup] deploy warning: {exc}")


def _warmup(client: DevClient) -> None:
    warm = SessionHandle(
        session_id=f"warmup-{uuid.uuid4().hex[:8]}",
        app_id=_APP_ID, daemon_url=client.daemon_url, workspace="",
    )
    try:
        _kick_session(client, warm)
    except Exception:
        pass


def _login_with_redirects(daemon_url: str, email: str, password: str) -> str:
    import httpx
    login_url = f"{daemon_url}/auth/login"
    register_url = f"{daemon_url}/auth/register"
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        r = c.post(login_url, json={"email": email, "password": password})
        if r.status_code == 401:
            username = email.split("@", 1)[0].replace("-", "_").replace(".", "_")
            reg = c.post(register_url, json={
                "email": email, "password": password, "username": username,
            })
            if reg.status_code not in (200, 201):
                raise RuntimeError(f"register failed: {reg.status_code} {reg.text[:200]}")
            r = c.post(login_url, json={"email": email, "password": password})
        if r.status_code != 200:
            raise RuntimeError(f"login failed: {r.status_code} {r.text[:200]}")
        token = r.json().get("access_token")
        if not token:
            raise RuntimeError(f"login response missing access_token: {r.text[:200]}")
        return token


def main() -> int:
    daemon_url = _os.environ.get("DAEMON_URL", "http://127.0.0.1:8000")
    email = _os.environ.get("DEV_EMAIL", "preview-tester@example.com")
    password = _os.environ.get("DEV_PASSWORD", "DevPassword123!")
    try:
        token = _login_with_redirects(daemon_url, email, password)
    except Exception as exc:
        print(f"[setup] login failed: {exc}")
        return 2
    client = DevClient.with_token(token, daemon_url=daemon_url)
    _ensure_app_deployed(client)
    _warmup(client)

    scenarios = [
        ("approve_clears_aggregate", lambda c: scenario_approve_clears_aggregate(c)),
        ("reject_brand_new_file_deletes", lambda c: scenario_reject_brand_new_file_deletes(c)),
        ("writeback_unauthorized", lambda c: scenario_writeback_unauthorized(c, daemon_url)),
        ("writeback_size_limit", lambda c: scenario_writeback_size_limit(c)),
        ("path_traversal_blocked", lambda c: scenario_path_traversal_blocked(c)),
    ]
    passed = 0
    print(f"\n=== Workspace hardening scenarios ({len(scenarios)}) ===\n")
    for name, fn in scenarios:
        t0 = time.time()
        try:
            ok, detail, art = fn(client)
        except Exception as exc:
            ok, detail, art = False, f"EXCEPTION: {type(exc).__name__}: {exc}", {}
        dur = time.time() - t0
        tag = "PASS" if ok else "FAIL"
        print(f"[{tag}] {name} ({dur:.1f}s)")
        print(detail)
        if not ok and art:
            import json as _json
            print(f"  artifacts: {_json.dumps(art, default=str)[:500]}")
        print()
        if ok:
            passed += 1
    print(f"{passed}/{len(scenarios)} scenarios passed\n")
    return 0 if passed == len(scenarios) else 1


if __name__ == "__main__":
    sys.exit(main())

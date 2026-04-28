"""Round 7 live verification - exercise each CVE / regression fix
against the RUNNING daemon via real HTTP. No mocks, no source-string
checks. Each test fails loudly if the fix is missing at runtime.

Requires a running daemon at 127.0.0.1:8000.

Coverage:
  - BUG-061: POST /api/modules/{id}/execute requires admin
  - BUG-070: /events rejects cross-user (and anonymous)
  - BUG-073: /events rejects anonymous
  - BUG-074: /fork rejects cross-user
  - BUG-075: /export rejects cross-user
  - BUG-076: /queue /workspace /context-breakdown reject cross-user
  - BUG-080: /deploy-status reports deploy failures
  - BUG-091: POST /messages with extra field = OK, with audio = 422
  - BUG-057: /auth/logout accepts empty body
  - BUG-058: tokens are revoked after logout
  - BUG-100: /api/packages/install accepts {source} alias
  - BUG-062: /messages rejects oversize payload
"""
from __future__ import annotations
import json
import sys
import time
import uuid
from typing import Any

import httpx

BASE = "http://127.0.0.1:8000"
RESULTS: list[tuple[bool, str, str]] = []


def _rec(ok: bool, label: str, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}")
    if detail:
        print(f"         {detail}")
    RESULTS.append((ok, label, detail))


def _register(client: httpx.Client, prefix: str) -> tuple[str, str]:
    """Register a throwaway user, return (user_id, access_token)."""
    uname = f"{prefix}{uuid.uuid4().hex[:6]}"
    email = f"{uname}@test.local"
    password = "TestProd1234!xyz"
    r = client.post(f"{BASE}/auth/register", json={
        "username": uname, "email": email, "password": password,
    })
    if r.status_code != 200:
        # User may already exist; fall back to login
        r = client.post(f"{BASE}/auth/login", json={
            "email": email, "password": password,
        })
    data = r.json()
    return data.get("user_id") or data.get("data", {}).get("user_id") or uname, data["access_token"]


def _auth(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


def main() -> int:
    with httpx.Client(timeout=10.0) as c:
        # Sanity: daemon is alive
        r = c.get(f"{BASE}/health")
        if r.status_code != 200:
            print("FAIL: daemon not reachable")
            return 1

        # ── Prepare two distinct users ─────────────────────────────
        print("\n── Setup: register two users ──")
        uidA, tokA = _register(c, "alice")
        uidB, tokB = _register(c, "bob")
        _rec(bool(tokA) and bool(tokB) and tokA != tokB,
             "two fresh users registered",
             f"uidA_prefix={uidA[:8]} uidB_prefix={uidB[:8]}")

        # ── BUG-061: /api/modules/{id}/execute requires admin ─────
        print("\n── BUG-061: /api/modules/{id}/execute admin guard ──")
        r = c.post(
            f"{BASE}/api/modules/shell/execute",
            headers=_auth(tokA),
            json={"action": "bash", "params": {"command": "echo hi"}},
        )
        detail = f"status={r.status_code} body={r.text[:160]}"
        _rec(r.status_code == 403, "developer token → 403", detail)

        # ── BUG-073 / BUG-070: /events requires auth + ownership ──
        print("\n── BUG-073 / BUG-070: /events anon + cross-user ──")
        # Create a session under user A by posting a simple message
        sid = f"sess-{uuid.uuid4().hex[:8]}"
        c.post(
            f"{BASE}/api/apps/digitorn-chat/sessions/{sid}/messages",
            headers=_auth(tokA),
            json={"message": "hi"},
        )
        time.sleep(0.5)
        # (a) anonymous → 401
        r_anon = c.get(
            f"{BASE}/api/apps/digitorn-chat/sessions/{sid}/events",
        )
        _rec(r_anon.status_code == 401,
             "/events without token → 401",
             f"got {r_anon.status_code}")
        # (b) userB → 404 (no info leak)
        r_xuser = c.get(
            f"{BASE}/api/apps/digitorn-chat/sessions/{sid}/events",
            headers=_auth(tokB),
        )
        _rec(r_xuser.status_code == 404,
             "/events cross-user → 404 (no info leak)",
             f"got {r_xuser.status_code}")
        # (c) owner → 200
        r_own = c.get(
            f"{BASE}/api/apps/digitorn-chat/sessions/{sid}/events",
            headers=_auth(tokA),
        )
        _rec(r_own.status_code == 200,
             "/events owner → 200",
             f"got {r_own.status_code}")

        # ── BUG-076: /queue /workspace /context-breakdown ─────────
        print("\n── BUG-076: /queue, /workspace, /context-breakdown ──")
        for path in ("queue", "workspace", "context-breakdown"):
            r = c.get(
                f"{BASE}/api/apps/digitorn-chat/sessions/{sid}/{path}",
                headers=_auth(tokB),
            )
            _rec(r.status_code == 404,
                 f"/{path} cross-user → 404",
                 f"got {r.status_code}")

        # ── BUG-074: /fork cross-user ─────────────────────────────
        print("\n── BUG-074: /fork cross-user ──")
        r = c.post(
            f"{BASE}/api/apps/digitorn-chat/sessions/{sid}/fork",
            headers=_auth(tokB),
        )
        _rec(r.status_code == 404,
             "/fork cross-user → 404",
             f"got {r.status_code}")

        # ── BUG-075: /export cross-user ───────────────────────────
        print("\n── BUG-075: /export cross-user ──")
        r = c.get(
            f"{BASE}/api/apps/digitorn-chat/sessions/{sid}/export",
            headers=_auth(tokB),
        )
        _rec(r.status_code == 404,
             "/export cross-user → 404",
             f"got {r.status_code}")

        # ── BUG-091 / BUG-092: /messages field validation ─────────
        print("\n── BUG-091 / BUG-092: /messages extra fields ──")
        # Normal message still accepted (no 422 from overly-strict
        # Pydantic - this is the BUG-091 regression I shipped then
        # reverted).
        r = c.post(
            f"{BASE}/api/apps/digitorn-chat/sessions/{sid}/messages",
            headers=_auth(tokA),
            json={"message": "ok"},
        )
        _rec(r.status_code in (200, 202),
             "/messages simple body accepted",
             f"got {r.status_code}")
        # Unknown field tolerated
        r = c.post(
            f"{BASE}/api/apps/digitorn-chat/sessions/{sid}/messages",
            headers=_auth(tokA),
            json={"message": "ok", "metadata": {"x": 1}},
        )
        _rec(r.status_code in (200, 202),
             "/messages unknown field tolerated",
             f"got {r.status_code}")
        # audio field → 422
        r = c.post(
            f"{BASE}/api/apps/digitorn-chat/sessions/{sid}/messages",
            headers=_auth(tokA),
            json={"message": "ok", "audio": {"data": "x"}},
        )
        _rec(r.status_code in (400, 422),
             "/messages with audio field → 4xx",
             f"got {r.status_code} body={r.text[:140]}")

        # ── BUG-062: oversize payload → 413 ───────────────────────
        print("\n── BUG-062: oversize /messages payload ──")
        big = "X" * (3 * 1024 * 1024)  # 3 MiB > 2 MiB cap on /messages
        r = c.post(
            f"{BASE}/api/apps/digitorn-chat/sessions/{sid}/messages",
            headers={**_auth(tokA), "Content-Type": "application/json"},
            content=json.dumps({"message": big}),
        )
        _rec(r.status_code == 413,
             "3 MiB body → 413",
             f"got {r.status_code}")

        # ── BUG-080: /deploy-status endpoint exists and reports ───
        print("\n── BUG-080: /deploy-status ──")
        r = c.get(f"{BASE}/api/apps/nonexistent/deploy-status",
                  headers=_auth(tokA))
        body = r.json() if r.status_code == 200 else {}
        data = body.get("data", {})
        _rec(
            r.status_code == 200
            and data.get("deployed") is False
            and "app_id" in data,
            "/deploy-status returns shape for unknown app",
            f"status={r.status_code} data={data}",
        )

        # ── BUG-100: /packages/install accepts {source} alias ─────
        print("\n── BUG-100: /packages/install source alias ──")
        r = c.post(
            f"{BASE}/api/packages/install",
            headers=_auth(tokA),
            json={"source": "bundle://digitorn/digitorn-chat"},
        )
        # 409 (permissions probe) or 200 or any non-422 is fine here -
        # 422 would mean our alias splitter never ran.
        _rec(
            r.status_code != 422,
            "/packages/install {source} alias not rejected as 422",
            f"got {r.status_code}",
        )

        # ── BUG-057: /auth/logout accepts empty body ──────────────
        print("\n── BUG-057: /auth/logout empty body ──")
        r = c.post(
            f"{BASE}/auth/logout",
            headers=_auth(tokA),
        )
        _rec(r.status_code == 200,
             "/auth/logout empty body → 200",
             f"got {r.status_code}")

        # ── BUG-058: token revoked after logout ───────────────────
        print("\n── BUG-058: token revocation ──")
        r = c.get(f"{BASE}/api/apps", headers=_auth(tokA))
        _rec(r.status_code == 401,
             "old token rejected after logout",
             f"got {r.status_code}")

    total = len(RESULTS)
    passed = sum(1 for ok, *_ in RESULTS if ok)
    print(f"\n=> {passed}/{total} pass")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

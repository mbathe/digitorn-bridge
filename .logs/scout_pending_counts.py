"""Scout: exercise the pending-counters + unified-diff flow end-to-end
against a running daemon.

Expectations (per the bug report):
  step 1  WsWrite    notes.txt (3 lines)             → ins_pending=3, del_pending=0
  step 2  approve                                    → ins_pending=0, del_pending=0
  step 3  WsEdit     replace 1 line                  → ins_pending=1, del_pending=1
  step 4  WsEdit     add 1 line at end               → ins_pending=2, del_pending=1
  diff    well-formed unified_diff (newlines OK)
  step 5  approve-hunks [0]                          → partial stage; ins=1, del=0 remaining
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

os.environ.setdefault("DIGITORN_DEV_DAEMON_URL", "http://127.0.0.1:8100")

import urllib.request
import urllib.error

APP_ID = "ws-preview-test"
BASE = os.environ["DIGITORN_DEV_DAEMON_URL"]


def _req(method: str, path: str, body: Any = None) -> dict[str, Any]:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http_status": e.code, "_body": e.read().decode()}


def _ok(label: str, cond: bool, extra: str = "") -> bool:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}  {extra}")
    return cond


def main() -> int:
    print(f"daemon: {BASE}")

    r = _req("GET", f"/api/apps/{APP_ID}")
    if r.get("_http_status") == 404 or not r.get("success"):
        print(f"  app {APP_ID!r} not deployed - scout aborts")
        return 2
    print(f"  app {APP_ID!r} deployed OK")

    r = _req("POST", f"/api/apps/{APP_ID}/sessions", body={})
    sid = (r.get("data") or {}).get("session_id")
    if not sid:
        print(f"  FAIL session create: {r}")
        return 2
    print(f"  session: {sid}")

    def _post_msg(msg: str) -> dict[str, Any]:
        return _req(
            "POST",
            f"/api/apps/{APP_ID}/sessions/{sid}/messages",
            body={"message": msg},
        )

    def _get_file() -> dict[str, Any]:
        r = _req(
            "GET",
            f"/api/apps/{APP_ID}/sessions/{sid}/workspace/files/notes.txt?include_baseline=true",
        )
        return (r.get("data") or {}) if r.get("success") else {}

    # We assume ws-preview-test has an agent that echoes a write command.
    # For a deterministic scout, use the PUT writeback endpoint directly -
    # no LLM round-trip required.
    def _writeback(content: str) -> dict[str, Any]:
        return _req(
            "PUT",
            f"/api/apps/{APP_ID}/sessions/{sid}/workspace/files/notes.txt",
            body={"content": content, "auto_approve": False},
        )

    def _approve() -> dict[str, Any]:
        return _req(
            "POST",
            f"/api/apps/{APP_ID}/sessions/{sid}/workspace/files/approve",
            body={"path": "notes.txt"},
        )

    def _approve_hunks(hunks: list) -> dict[str, Any]:
        return _req(
            "POST",
            f"/api/apps/{APP_ID}/sessions/{sid}/workspace/files/approve-hunks",
            body={"path": "notes.txt", "hunks": hunks},
        )

    passed = 0
    failed = 0

    # Step 1 - initial write, 3 lines, no baseline yet.
    _writeback("line one\nline two\nline three\n")
    f = _get_file()
    p = f.get("payload", {})
    if _ok("step1: payload.insertions_pending == 3", p.get("insertions_pending") == 3,
           f"(got {p.get('insertions_pending')})"):
        passed += 1
    else:
        failed += 1
    if _ok("step1: payload.deletions_pending == 0", p.get("deletions_pending") == 0,
           f"(got {p.get('deletions_pending')})"):
        passed += 1
    else:
        failed += 1

    # Step 2 - approve.
    r = _approve()
    f = _get_file()
    p = f.get("payload", {})
    if _ok("step2: validation == approved", p.get("validation") == "approved"):
        passed += 1
    else:
        failed += 1
    if _ok("step2: ins/del pending == 0", p.get("insertions_pending") == 0 and p.get("deletions_pending") == 0):
        passed += 1
    else:
        failed += 1

    # Step 3 - edit: replace "line two" with "LINE TWO".
    _writeback("line one\nLINE TWO\nline three\n")
    f = _get_file()
    p = f.get("payload", {})
    if _ok("step3: insertions_pending == 1", p.get("insertions_pending") == 1,
           f"(got {p.get('insertions_pending')})"):
        passed += 1
    else:
        failed += 1
    if _ok("step3: deletions_pending == 1", p.get("deletions_pending") == 1,
           f"(got {p.get('deletions_pending')})"):
        passed += 1
    else:
        failed += 1

    # Step 4 - edit: add "line four" at end.
    _writeback("line one\nLINE TWO\nline three\nline four\n")
    f = _get_file()
    p = f.get("payload", {})
    if _ok("step4: insertions_pending == 2", p.get("insertions_pending") == 2,
           f"(got {p.get('insertions_pending')})"):
        passed += 1
    else:
        failed += 1
    if _ok("step4: deletions_pending == 1", p.get("deletions_pending") == 1,
           f"(got {p.get('deletions_pending')})"):
        passed += 1
    else:
        failed += 1

    # Step 4b - unified diff well-formed.
    diff = f.get("unified_diff_pending") or ""
    lines = [ln for ln in diff.rstrip("\n").split("\n") if ln]
    any_bad = any(ln and ln[0] not in " -+@\\" for ln in lines)
    if _ok("step4: unified_diff_pending well-formed (no fused lines)", not any_bad):
        passed += 1
    else:
        failed += 1
        print(f"    diff: {diff!r}")

    # Step 5 - per-hunk approve (approve index 0, leave the rest).
    # After step 4, with baseline = "line one\nline two\nline three\n" and
    # current = "line one\nLINE TWO\nline three\nline four\n", the diff
    # has 1 hunk covering both the replace and the insert (small file),
    # so approve [0] drains everything.
    r = _approve_hunks([0])
    if _ok("step5: approve-hunks [0] succeeds", bool(r.get("success")), f"(result: {r.get('error') or 'ok'})"):
        passed += 1
    else:
        failed += 1

    print(f"\n  {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

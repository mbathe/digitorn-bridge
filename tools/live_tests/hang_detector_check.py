"""Verify the shell module's infinite-wait detector.

Three checks:

  1. ``tail -f /dev/null`` is rejected with a hint.
  2. ``sleep infinity`` is rejected with a hint.
  3. A LEGITIMATE wait (``until grep -q ... ; do sleep 0.5 ; done``) is
     allowed - the detector must only catch *unbounded* primitives.

If a legitimate pattern gets falsely rejected, agents can't wait at all
and the system is worse off than before. Both directions matter.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx


_APP_ID = "agent-with-preview"
_DAEMON = "http://127.0.0.1:8000"


def _login(daemon_url: str, email: str, password: str) -> str:
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        r = c.post(
            f"{daemon_url}/auth/login",
            json={"email": email, "password": password},
        )
        if r.status_code == 401:
            uname = email.split("@", 1)[0].replace(".", "_").replace("-", "_")
            reg = c.post(
                f"{daemon_url}/auth/register",
                json={"email": email, "password": password, "username": uname},
            )
            if reg.status_code not in (200, 201):
                raise RuntimeError(
                    f"register failed {reg.status_code} {reg.text[:200]}"
                )
            r = c.post(
                f"{daemon_url}/auth/login",
                json={"email": email, "password": password},
            )
        if r.status_code != 200:
            raise RuntimeError(f"login failed {r.status_code} {r.text[:200]}")
        return r.json().get("access_token") or ""


def _exec_bash(token: str, app_id: str, session_id: str, command: str,
               run_in_background: bool = False, timeout: int = 10) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "session_id": session_id,
        "params": {
            "command": command,
            "run_in_background": run_in_background,
            "timeout": timeout,
        },
    }
    with httpx.Client(timeout=20.0) as c:
        r = c.post(
            f"{_DAEMON}/api/apps/{app_id}/tools/Bash/execute",
            json=body, headers=headers,
        )
    return r.json()


def main() -> int:
    email = os.environ.get(
        "DEV_EMAIL", f"hang-{uuid.uuid4().hex[:8]}@example.com"
    )
    password = os.environ.get("DEV_PASSWORD", "DevPassword123!")
    token = _login(_DAEMON, email, password)
    print(f"[setup] logged in as {email}")

    # Make sure agent-with-preview is deployed (need a session that has
    # Bash granted).
    yaml_path = Path(
        "c:/Users/ASUS/Documents/digitorn-bridge/examples/"
        "agent-with-preview/app.yaml"
    )
    with httpx.Client(timeout=30.0) as c:
        r = c.post(
            f"{_DAEMON}/api/apps/deploy",
            files={"yaml": yaml_path.read_bytes()},
            data={"force": "true"},
            headers={"Authorization": f"Bearer {token}"},
        )
    print(f"[setup] deploy status={r.status_code}")

    # Create a session
    workspace = Path.home() / ".digitorn" / "test-workspaces" / f"hang-{uuid.uuid4().hex[:6]}"
    body = {
        "message": "Reply with 'ok' and stop.",
        "workspace_path": str(workspace),
    }
    with httpx.Client(timeout=30.0) as c:
        r = c.post(
            f"{_DAEMON}/api/apps/{_APP_ID}/sessions",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    if r.status_code != 200:
        print(f"[setup] create_session failed: {r.status_code} {r.text[:200]}")
        return 1
    session_id = r.json().get("data", {}).get("session_id") or ""
    print(f"[setup] session: {session_id}")

    fail_cases = [
        ("tail -f /dev/null", "tail -f /dev/null"),
        ("sleep infinity", "sleep infinity"),
        ("cat /dev/zero", "cat /dev/zero"),
        ("while true; do :; done", "while true; do :; done"),
    ]

    results: list[tuple[str, bool, str]] = []

    for label, cmd in fail_cases:
        res = _exec_bash(token, _APP_ID, session_id, cmd, timeout=5)
        success = res.get("success")
        error = (res.get("error") or "")[:200]
        # Should be rejected (success=False) with "infinite-wait" hint
        rejected = (success is False
                    and "infinite-wait" in error.lower())
        results.append((f"reject: {label}", rejected, error))
        print(f"[CASE] {label!r}: success={success} error={error[:100]!r}")

    # Now verify a legitimate self-terminating wait still passes.
    # Use a command that exits very quickly: until checks /tmp/never
    # but we set up a file to make it pass on first iteration.
    legit = (
        "echo ready > /tmp/hang-test-$$ && "
        "until [ -f /tmp/hang-test-$$ ]; do sleep 0.1; done && "
        "echo done && rm /tmp/hang-test-$$"
    )
    res = _exec_bash(token, _APP_ID, session_id, legit, timeout=10)
    success = res.get("success")
    output = ""
    if isinstance(res.get("data"), dict):
        output = (res["data"].get("stdout") or "")[:200]
    accepted = bool(success) and "done" in output.lower()
    results.append((
        "accept: until ... done loop",
        accepted,
        f"success={success} stdout={output[:80]!r}",
    ))
    print(f"[CASE] legit until loop: success={success} stdout={output[:80]!r}")

    # Cleanup
    try:
        with httpx.Client(timeout=10.0) as c:
            c.delete(
                f"{_DAEMON}/api/apps/{_APP_ID}/sessions/{session_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception:
        pass

    print()
    all_ok = all(ok for _, ok, _ in results)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail[:120]}")
    if all_ok:
        print()
        print("[PASS] hang detector blocks infinite waits and lets legit "
              "ones through")
        return 0
    print()
    print("[FAIL] hang detector regressed somewhere")
    return 1


if __name__ == "__main__":
    sys.exit(main())

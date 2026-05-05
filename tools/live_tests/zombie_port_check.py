"""Verify the zombie-port-collision detection.

Setup: a zombie ``python -m http.server`` is already bound to 47899
(spawned manually before this test). The scenario then simulates an
agent trying to:
  1. Spawn its own http.server on the same port (which will fail to
     bind silently because the port is taken).
  2. Call PreviewProxy(port=47899, bash_task_id=...).

Expected after fix:
  - Either the shell module's longer watchdog detects the process
    exited within 1s with non-zero status -> Bash returns success=False
  - OR Bash returns success=True (process still in startup), but
    PreviewProxy's bash_task_id liveness check catches the death
    before attaching, returning success=False with a hint.

Either outcome is correct: the agent learns the spawn failed and
can pick a different port.

If both fixes fail, PreviewProxy returns success=True (incorrectly)
and the iframe ends up pointing at the zombie's wrong-cwd 404 page.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

from digitorn.testing.client import DevClient


_APP_ID = "agent-with-preview"
_DAEMON = "http://127.0.0.1:8000"
_BUSY_PORT = 47899  # zombie is bound here


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
                raise RuntimeError(f"register {reg.status_code}: {reg.text[:200]}")
            r = c.post(
                f"{daemon_url}/auth/login",
                json={"email": email, "password": password},
            )
        if r.status_code != 200:
            raise RuntimeError(f"login {r.status_code}: {r.text[:200]}")
        return r.json().get("access_token") or ""


def _exec_tool(client: DevClient, app_id: str, session_id: str,
               tool: str, params: dict) -> dict:
    r = client._post(
        f"/api/apps/{app_id}/tools/{tool}/execute",
        json={"session_id": session_id, "params": params},
    )
    try:
        return r.json()
    except Exception:
        return {"success": False, "error": r.text[:300]}


def main() -> int:
    email = os.environ.get("DEV_EMAIL", f"zomb-{uuid.uuid4().hex[:6]}@example.com")
    password = os.environ.get("DEV_PASSWORD", "Px12345abcd!")
    token = _login(_DAEMON, email, password)
    print(f"[setup] logged in as {email}")
    client = DevClient.with_token(token, daemon_url=_DAEMON)

    yaml_path = Path(
        "c:/Users/ASUS/Documents/digitorn-bridge/examples/"
        "agent-with-preview/app.yaml"
    )
    try:
        client.deploy(str(yaml_path), force=True)
        print(f"[setup] deployed {_APP_ID}")
    except Exception as exc:
        print(f"[setup] deploy: {exc}")
        return 1

    # Verify zombie is up before we start
    try:
        r = httpx.get(f"http://localhost:{_BUSY_PORT}/", timeout=3.0)
        print(f"[setup] zombie on :{_BUSY_PORT} -> status={r.status_code}")
    except Exception as exc:
        print(f"[setup] FAIL: zombie isn't running on :{_BUSY_PORT}: {exc}")
        print("        Start a zombie first via: cd c:/tmp/zombie-cwd && "
              f"python -m http.server {_BUSY_PORT}")
        return 2

    # Create a session in a dir THAT HAS landing.html
    workspace = Path.home() / ".digitorn" / "test-workspaces" / f"zomb-{uuid.uuid4().hex[:6]}"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "landing.html").write_text(
        "<!doctype html><h1>real landing here</h1>", encoding="utf-8"
    )
    body = {"message": "reply ok", "workspace_path": str(workspace)}
    r = client._post(f"/api/apps/{_APP_ID}/sessions", json=body)
    if r.status_code != 200:
        print(f"[step 1] create_session: {r.status_code} {r.text[:200]}")
        return 1
    session_id = r.json().get("data", {}).get("session_id") or ""
    print(f"[step 1] session: {session_id}")
    time.sleep(1)

    # Try to spawn http.server on the busy port
    cmd = f"cd '{workspace}' && python -m http.server {_BUSY_PORT}"
    bash_res = _exec_tool(client, _APP_ID, session_id, "Bash", {
        "command": cmd, "run_in_background": True,
    })
    print(f"[step 2] Bash success={bash_res.get('success')} "
          f"error={(bash_res.get('error') or '')[:120]!r}")

    # Outcome A: Bash detected the failure (good - watchdog caught it)
    if not bash_res.get("success"):
        stderr = (bash_res.get("data") or {}).get("stderr", "")
        print(f"[PASS-A] Bash watchdog caught zombie collision")
        print(f"        stderr: {stderr[:200]}")
        return 0

    # Outcome B: Bash returned success but the process actually died.
    # Sleep a beat to let the process die.
    bash_data = bash_res.get("data") or {}
    bash_task_id = bash_data.get("task_id") or ""
    print(f"[step 2] Bash reported success: task={bash_task_id}")
    time.sleep(1.5)

    # Now call PreviewProxy. The new bash_task_id liveness check
    # should detect the dead task and return success=False.
    proxy_res = _exec_tool(client, _APP_ID, session_id, "PreviewProxy", {
        "port": _BUSY_PORT,
        "bash_task_id": bash_task_id,
        "name": "default",
    })
    print(f"[step 3] PreviewProxy success={proxy_res.get('success')} "
          f"error={(proxy_res.get('error') or '')[:200]!r}")

    if not proxy_res.get("success"):
        err = proxy_res.get("error") or ""
        if "no longer running" in err.lower() or "zombie" in err.lower():
            print(f"[PASS-B] PreviewProxy bash_task_id check caught dead task")
            return 0
        print(f"[FAIL] PreviewProxy rejected but for the wrong reason")
        return 1

    print(f"[FAIL] PreviewProxy attached to a zombie - both fixes missed it")
    return 1


if __name__ == "__main__":
    sys.exit(main())

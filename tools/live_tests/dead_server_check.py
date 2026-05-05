"""Verify PreviewProxy refuses to attach when the server never bound.

The user's failing scenario was: agent runs `php -S 127.0.0.1:8767`,
php is not installed, exit 127. PreviewProxy used to attach anyway
with a logged warning, leaving the iframe pointing at a dead port.
After the fix, PreviewProxy must REFUSE the attach with a clear
message including the bash stderr if available.
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
_DEAD_PORT = 47909


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
    email = os.environ.get("DEV_EMAIL", f"dead-{uuid.uuid4().hex[:6]}@example.com")
    password = "Px12345abcd!"
    token = _login(_DAEMON, email, password)
    print(f"[setup] logged in as {email}")
    client = DevClient.with_token(token, daemon_url=_DAEMON)

    yaml_path = Path(
        "c:/Users/ASUS/Documents/digitorn-bridge/examples/"
        "agent-with-preview/app.yaml"
    )
    client.deploy(str(yaml_path), force=True)
    print("[setup] deployed agent-with-preview")

    workspace = Path.home() / ".digitorn" / "test-workspaces" / f"dead-{uuid.uuid4().hex[:6]}"
    workspace.mkdir(parents=True, exist_ok=True)
    body = {"message": "reply ok", "workspace_path": str(workspace)}
    r = client._post(f"/api/apps/{_APP_ID}/sessions", json=body)
    session_id = r.json()["data"]["session_id"]
    print(f"[step 1] session: {session_id}")
    time.sleep(1)

    # Try to spawn a non-existent binary.
    bash_res = _exec_tool(client, _APP_ID, session_id, "Bash", {
        "command": f"definitely-not-a-real-binary --port {_DEAD_PORT}",
        "run_in_background": True,
    })
    print(f"[step 2] Bash success={bash_res.get('success')} "
          f"error={(bash_res.get('error') or '')[:120]!r}")
    bash_data = bash_res.get("data") or {}
    bash_task_id = bash_data.get("task_id") or ""
    if bash_res.get("success"):
        print(f"[step 2] task spawned: {bash_task_id}")
    else:
        print(f"[step 2] Bash refused outright (good - watchdog caught it)")
        # Still proceed: simulate an agent that ignores the refuse and
        # tries to attach anyway with a fake task_id.
        bash_task_id = "00deadbeef00"

    # Either way: nothing's listening on _DEAD_PORT. PreviewProxy
    # must refuse. Allow up to 18s for the probe (15s budget + slack).
    proxy_res = _exec_tool(client, _APP_ID, session_id, "PreviewProxy", {
        "port": _DEAD_PORT,
        "bash_task_id": bash_task_id,
        "name": "default",
    })
    print(f"[step 3] PreviewProxy success={proxy_res.get('success')} "
          f"error={(proxy_res.get('error') or '')[:200]!r}")

    if proxy_res.get("success"):
        print(f"[FAIL] PreviewProxy attached to a dead port")
        return 1

    err = proxy_res.get("error") or ""
    if "never bound" not in err.lower():
        print(f"[FAIL] error message doesn't mention 'never bound'")
        print(f"       got: {err[:300]}")
        return 1

    # Cleanup
    try:
        client._delete(f"/api/apps/{_APP_ID}/sessions/{session_id}")
    except Exception:
        pass

    print()
    print("[PASS] PreviewProxy refused to attach to a dead port")
    print(f"       error: {err[:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

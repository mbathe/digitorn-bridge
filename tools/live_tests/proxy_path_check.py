"""Verify the new ``path`` param on PreviewProxy.

Proves that a static single-file page named ``landing.html`` can be
served via ``python -m http.server`` and surfaced to the iframe at
``http://host:port/landing.html`` (not the directory listing).
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
_TEST_PORT = 47831


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


def _exec_tool(client: DevClient, app_id: str, session_id: str,
               tool: str, params: dict) -> dict:
    r = client._post(
        f"/api/apps/{app_id}/tools/{tool}/execute",
        json={"session_id": session_id, "params": params},
    )
    try:
        return r.json()
    except Exception:
        return {"success": False, "error": r.text[:300], "status": r.status_code}


def main() -> int:
    email = os.environ.get(
        "DEV_EMAIL", f"path-{uuid.uuid4().hex[:8]}@example.com"
    )
    password = os.environ.get("DEV_PASSWORD", "DevPassword123!")
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

    workspace = (
        Path.home() / ".digitorn" / "test-workspaces"
        / f"path-{uuid.uuid4().hex[:6]}"
    )
    body = {"message": "Reply 'ok' and stop.", "workspace_path": str(workspace)}
    r = client._post(f"/api/apps/{_APP_ID}/sessions", json=body)
    if r.status_code != 200:
        print(f"[step 1] create_session failed: {r.status_code} {r.text[:200]}")
        return 1
    session_id = r.json().get("data", {}).get("session_id") or ""
    print(f"[step 1] session: {session_id}")

    # Wait for first turn so the session is registered.
    time.sleep(2)

    # Write a non-index.html landing page.
    workspace.mkdir(parents=True, exist_ok=True)
    landing = workspace / "landing.html"
    landing.write_text(
        "<!doctype html><meta charset=utf-8><title>Nimbus</title>"
        "<h1>Nimbus landing page test</h1>",
        encoding="utf-8",
    )
    print(f"[step 2] wrote {landing}")

    # Spawn http.server in background.
    bash_cmd = f"cd '{workspace}' && python -m http.server {_TEST_PORT} --bind 127.0.0.1"
    bash_res = _exec_tool(client, _APP_ID, session_id, "Bash", {
        "command": bash_cmd, "run_in_background": True,
    })
    if not bash_res.get("success"):
        print(f"[step 3] Bash failed: {json.dumps(bash_res, default=str)[:300]}")
        return 1
    bash_data = bash_res.get("data") or {}
    bash_task_id = bash_data.get("task_id") or ""
    print(f"[step 3] Bash spawned: task={bash_task_id}")

    # Attach with path="/landing.html"
    proxy_res = _exec_tool(client, _APP_ID, session_id, "PreviewProxy", {
        "port": _TEST_PORT,
        "path": "/landing.html",
        "name": "default",
        "bash_task_id": bash_task_id,
    })
    if not proxy_res.get("success"):
        print(f"[step 4] PreviewProxy failed: {json.dumps(proxy_res, default=str)[:300]}")
        return 1
    proxy_data = proxy_res.get("data") or {}
    iframe_url = proxy_data.get("iframe_url") or ""
    print(f"[step 4] iframe_url={iframe_url}")

    # Verify the URL ends with /landing.html
    if not iframe_url.endswith("/landing.html"):
        print(f"[step 4] FAIL: iframe_url should end with /landing.html, "
              f"got {iframe_url!r}")
        return 1

    # Verify /web-preview returns the same URL
    r = client._get(
        f"/api/apps/{_APP_ID}/web-preview?session_id={session_id}&name=default"
    )
    if r.status_code != 200:
        print(f"[step 5] /web-preview failed: {r.status_code}")
        return 1
    payload = r.json()
    print(f"[step 5] /web-preview: url={payload.get('url')!r} "
          f"path={payload.get('url', '').split('/')[-1]}")
    if payload.get("url", "") != iframe_url:
        print(f"[step 5] FAIL: /web-preview url mismatch")
        return 1

    # Direct fetch the URL the browser would hit. Should return the
    # landing.html content, not a directory listing.
    try:
        r = httpx.get(iframe_url, timeout=5.0)
    except Exception as exc:
        print(f"[step 6] direct fetch failed: {exc}")
        return 1
    print(f"[step 6] direct fetch: status={r.status_code} bytes={len(r.text)}")
    if r.status_code != 200:
        return 1
    if "Nimbus landing page test" not in r.text:
        print(f"[step 6] FAIL: page content missing. First 200 chars: {r.text[:200]}")
        return 1

    # Cleanup
    try:
        _exec_tool(client, _APP_ID, session_id, "PreviewDetach",
                   {"name": "default"})
        _exec_tool(client, _APP_ID, session_id, "Bash",
                   {"task_id": bash_task_id, "kill": True})
        client._delete(f"/api/apps/{_APP_ID}/sessions/{session_id}")
    except Exception:
        pass

    print()
    print("[PASS] PreviewProxy `path` param works end-to-end:")
    print(f"  - Bash served landing.html via http.server (cwd=workspace)")
    print(f"  - PreviewProxy(port={_TEST_PORT}, path='/landing.html')")
    print(f"  - iframe_url = {iframe_url}")
    print(f"  - Direct fetch returns the landing.html content (not dir listing)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

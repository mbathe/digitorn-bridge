"""End-to-end verification of the agent path: Bash + PreviewProxy.

Deploys ``agent-with-preview``, creates a session, then drives the
two tools manually via /tools/{tool}/execute - simulating what the LLM
would do - and verifies:

  1. Bash spawns ``python -m http.server`` in background
  2. PreviewProxy registers the port
  3. /web-preview returns the direct-connect URL
  4. /health/web_preview shows the proxy attachment
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
_TEST_PORT = 47821


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
        "DEV_EMAIL", f"proxy-{uuid.uuid4().hex[:8]}@example.com"
    )
    password = os.environ.get("DEV_PASSWORD", "DevPassword123!")
    token = _login(_DAEMON, email, password)
    print(f"[setup] logged in as {email}")
    client = DevClient.with_token(token, daemon_url=_DAEMON)

    # Force-deploy from source so we get the latest YAML.
    yaml_path = Path(
        "c:/Users/ASUS/Documents/digitorn-bridge/examples/agent-with-preview/app.yaml"
    )
    if not yaml_path.is_file():
        print(f"[setup] FAIL: yaml missing at {yaml_path}")
        return 2
    try:
        client.deploy(str(yaml_path), force=True)
        print(f"[setup] deployed {_APP_ID}")
    except Exception as exc:
        print(f"[setup] deploy: {exc}")
        return 1

    # Create a session (this auto-registers a bundled attachment if
    # the app has web/dist; agent-with-preview doesn't ship a dist).
    workspace = Path.home() / ".digitorn" / "test-workspaces" / f"proxy-{uuid.uuid4().hex[:6]}"
    body = {
        "message": "Reply with 'ok' and stop. Do not call any tool.",
        "workspace_path": str(workspace),
    }
    r = client._post(f"/api/apps/{_APP_ID}/sessions", json=body)
    if r.status_code != 200:
        print(f"[step 1] create_session failed: {r.status_code} {r.text[:300]}")
        return 1
    sess_data = r.json().get("data", {})
    session_id = sess_data.get("session_id") or ""
    print(f"[step 1] session: {session_id}")

    # Wait for the first turn to complete so the session is "warm" and
    # the contextvar machinery is fully activated. We poll the session
    # status (avoids needing a Socket.IO stream just for sequencing).
    for i in range(60):
        s = client._get(f"/api/apps/{_APP_ID}/sessions/{session_id}")
        if s.status_code == 200:
            data = (s.json() or {}).get("data") or {}
            state = data.get("state") or ""
            if state in ("idle", "ready", ""):
                # No turn in flight: either it never started, or it's done
                last_msg = data.get("messages", [])
                if last_msg and any(m.get("role") == "assistant" for m in last_msg):
                    print(f"[step 2] first turn done after {i}s")
                    break
        time.sleep(1)
    else:
        print("[step 2] first turn never completed in 60s")

    # Spawn a tiny static HTTP server via the Bash tool. Use a
    # workspace-relative path so the daemon's path sandbox is happy.
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "hello.html").write_text(
        "<!doctype html><meta charset=utf-8><title>preview-test</title>"
        "<h1>preview-test ok</h1>",
        encoding="utf-8",
    )
    bash_cmd = f"cd '{workspace}' && python -m http.server {_TEST_PORT} --bind 127.0.0.1"
    bash_res = _exec_tool(client, _APP_ID, session_id, "Bash", {
        "command": bash_cmd, "run_in_background": True,
    })
    print(f"[step 3] Bash result: success={bash_res.get('success')} "
          f"error={bash_res.get('error')}")
    if not bash_res.get("success"):
        print(f"[step 3] Bash full: {json.dumps(bash_res, default=str)[:500]}")
        return 1
    bash_data = bash_res.get("data") or {}
    bash_task_id = bash_data.get("task_id") or ""
    print(f"          task_id={bash_task_id} pid={bash_data.get('pid')}")

    # Give the dev server a moment to bind.
    time.sleep(2.0)

    # Call PreviewProxy.
    proxy_res = _exec_tool(client, _APP_ID, session_id, "PreviewProxy", {
        "port": _TEST_PORT, "name": "default", "bash_task_id": bash_task_id,
    })
    print(f"[step 4] PreviewProxy result: success={proxy_res.get('success')} "
          f"error={proxy_res.get('error')}")
    if not proxy_res.get("success"):
        print(f"[step 4] full: {json.dumps(proxy_res, default=str)[:500]}")
        return 1
    proxy_data = proxy_res.get("data") or {}
    iframe_url = proxy_data.get("iframe_url") or ""
    print(f"          iframe_url={iframe_url}")

    # Lookup the URL via /web-preview.
    r = client._get(
        f"/api/apps/{_APP_ID}/web-preview?session_id={session_id}&name=default"
    )
    if r.status_code != 200:
        print(f"[step 5] /web-preview failed: {r.status_code} {r.text[:300]}")
        return 1
    payload = r.json()
    print(f"[step 5] /web-preview: {json.dumps(payload, default=str)[:300]}")
    if payload.get("type") != "proxy" or payload.get("port") != _TEST_PORT:
        print(f"[step 5] expected type=proxy port={_TEST_PORT}, got {payload}")
        return 1

    # Hit the dev server URL directly to make sure the page is reachable.
    try:
        r = httpx.get(f"http://localhost:{_TEST_PORT}/hello.html", timeout=5.0)
        print(f"[step 6] direct fetch: status={r.status_code} bytes={len(r.text)}")
        ok_html = "preview-test ok" in r.text
    except Exception as exc:
        print(f"[step 6] direct fetch failed: {exc}")
        ok_html = False

    # Health snapshot
    r = client._get("/health/web_preview")
    h = r.json() if r.status_code == 200 else {}
    print(f"[step 7] /health/web_preview count={h.get('count')} "
          f"by_type={h.get('by_type')}")

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
    if not ok_html:
        print("[FAIL] direct dev-server fetch didn't return the test HTML")
        return 1
    if (h.get("by_type") or {}).get("proxy", 0) < 1:
        print("[FAIL] no 'proxy' entry in /health/web_preview by_type")
        return 1
    print("[PASS] agent path Bash + PreviewProxy works end-to-end:")
    print(f"  - Bash spawned http.server on port {_TEST_PORT}")
    print(f"  - PreviewProxy registered, /web-preview returned the URL")
    print(f"  - Browser-equivalent fetch hit the dev server directly")
    print(f"  - /health/web_preview shows the proxy attachment")
    return 0


if __name__ == "__main__":
    sys.exit(main())

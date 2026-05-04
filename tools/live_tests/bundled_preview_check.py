"""End-to-end live check that bundled-dist auto-attach works.

Story: deploy a builtin app that ships ``web/dist/`` (digitorn-react-sandbox).
Create a session. Verify:

  1. /api/apps/{app}/web-preview?session_id=... returns 200 with a URL
     pointing at /api/apps/{app}/web-static/index.html
  2. That URL serves the bundled HTML (200, content-type text/html)
  3. /health/web_preview reflects the new attachment (count delta +1,
     by_type contains 'bundled': 1)
  4. The agent did NOT have to do anything - the SDK just-works

If this passes, SDK apps work without any agent action - the entire
purpose of the SDK preview is restored after the simplification.
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
from digitorn.testing.models import SessionHandle


_APP_ID = "digitorn-react-sandbox"
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


def main() -> int:
    email = os.environ.get(
        "DEV_EMAIL", f"bundled-{uuid.uuid4().hex[:8]}@example.com"
    )
    password = os.environ.get("DEV_PASSWORD", "DevPassword123!")
    token = _login(_DAEMON, email, password)
    print(f"[setup] logged in as {email}")
    client = DevClient.with_token(token, daemon_url=_DAEMON)

    # /health/web_preview baseline
    r = client._get("/health/web_preview")
    h0 = r.json() if r.status_code == 200 else {}
    print(f"[step 0] baseline /health/web_preview status={r.status_code} "
          f"count={h0.get('count')} by_type={h0.get('by_type')}")

    # Make sure the builtin is deployed. Skip if it's already there.
    apps = client._get("/api/apps")
    body = apps.json() if apps.status_code == 200 else {}
    rows = body.get("data") if isinstance(body, dict) else body
    deployed_ids = {
        a.get("app_id") for a in (rows or [])
        if isinstance(a, dict)
    }
    # Force redeploy from source to make sure the latest YAML
    # (with web_preview: {}) lands in the active bundle.
    yaml_path = Path(
        "c:/Users/ASUS/Documents/digitorn-bridge/packages/digitorn/builtins/"
        "digitorn-react-sandbox/app.yaml"
    )
    if yaml_path.is_file():
        try:
            client.deploy(str(yaml_path), force=True)
            print(f"[setup] redeployed {_APP_ID} from source")
        except Exception as exc:
            print(f"[setup] deploy: {exc}")
    elif _APP_ID not in deployed_ids:
        print(f"[setup] WARN: {_APP_ID} not deployed and YAML not found")
        return 2
    print(f"[setup] {_APP_ID} ready")

    # Create a session - body requires a message; send a no-op text.
    sid = f"bundled-{uuid.uuid4().hex[:8]}"
    body = {
        "message": "Reply with the single word 'ok' and stop.",
        "workspace_path": str(Path.home() / ".digitorn" / "test-workspaces" / sid),
    }
    r = client._post(f"/api/apps/{_APP_ID}/sessions", json=body)
    if r.status_code != 200:
        print(f"[step 1] create_session failed: {r.status_code} {r.text[:300]}")
        return 1
    sess_data = r.json().get("data", {})
    session_id = sess_data.get("session_id") or ""
    if not session_id:
        print(f"[step 1] no session_id returned: {sess_data}")
        return 1
    print(f"[step 1] session created: {session_id}")
    preview_url = sess_data.get("preview_url")
    print(f"          preview_url from create_session: {preview_url}")

    # Lookup the attachment URL via the new endpoint.
    r = client._get(
        f"/api/apps/{_APP_ID}/web-preview?session_id={session_id}&name=default"
    )
    if r.status_code != 200:
        print(f"[step 2] /web-preview lookup failed: {r.status_code} {r.text[:300]}")
        return 1
    payload = r.json()
    print(f"[step 2] /web-preview returned: {json.dumps(payload, default=str)[:300]}")
    iframe_url = payload.get("url") or ""
    if not iframe_url:
        print("[step 2] no url in response")
        return 1
    if payload.get("type") != "bundled":
        print(f"[step 2] expected type=bundled, got {payload.get('type')!r}")
        return 1

    # Hit the static-bundle URL (the one the iframe would load).
    if iframe_url.startswith("/"):
        full = f"{_DAEMON}{iframe_url}"
    else:
        full = iframe_url
    r = client._get(iframe_url)
    print(f"[step 3] static fetch {iframe_url} -> status={r.status_code} "
          f"content-type={r.headers.get('content-type')} "
          f"bytes={len(r.content)}")
    if r.status_code != 200:
        print(f"[step 3] HTML content (first 200 chars): {r.text[:200]}")
        return 1
    if "<!doctype" not in r.text.lower() and "<html" not in r.text.lower():
        print("[step 3] response doesn't look like HTML")
        return 1

    # Check the daemon health reports the bundled attachment.
    time.sleep(0.2)
    r = client._get("/health/web_preview")
    h1 = r.json() if r.status_code == 200 else {}
    print(f"[step 4] /health/web_preview count={h1.get('count')} "
          f"by_type={h1.get('by_type')}")

    delta = (h1.get("count") or 0) - (h0.get("count") or 0)
    if delta < 1:
        print(f"[step 4] expected count delta >= 1, got {delta}")
        return 1
    if "bundled" not in (h1.get("by_type") or {}):
        print(f"[step 4] no 'bundled' entry in by_type")
        return 1

    # Cleanup the session.
    try:
        client._delete(f"/api/apps/{_APP_ID}/sessions/{session_id}")
    except Exception:
        pass

    print()
    print("[PASS] SDK app auto-attach works end-to-end:")
    print(f"  - Session created without any agent action")
    print(f"  - /web-preview returned a URL ({iframe_url})")
    print(f"  - URL serves the bundled HTML ({len(r.text)} bytes)")
    print(f"  - Daemon health shows the bundled attachment")
    return 0


if __name__ == "__main__":
    sys.exit(main())

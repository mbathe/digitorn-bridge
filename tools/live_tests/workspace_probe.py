"""Live probe — what workspace does a NEW digitorn-builder session see?

1. Creates a brand-new session on digitorn-builder.
2. Sends a message asking the agent to run ``pwd`` / report its CWD.
3. Pulls the system prompt that was actually injected (from
   ``/history`` via ``include_system=true``) so we can read the
   post-substitution ``{WORKSPACE}`` placeholder value.
4. Prints where workspace files would physically land.

No guesswork — concrete evidence per run.
"""
from __future__ import annotations

import json
import os as _os
import sys
import time
import uuid
from pathlib import Path

import httpx

from digitorn.testing.client import DevClient
from digitorn.testing.models import SessionHandle


def _auth(daemon_url: str) -> str:
    email = "probe@test.local"
    password = "ProbePassword123!"
    for path in ("/auth/login", "/auth/register"):
        body: dict = {"email": email, "password": password}
        if path.endswith("register"):
            body["username"] = "probe"
            body["name"] = "probe"
        r = httpx.post(f"{daemon_url}{path}", json=body, timeout=15.0)
        if r.status_code == 200:
            return r.json()["access_token"]
    raise RuntimeError("auth failed")


def main() -> int:
    daemon_url = _os.environ.get("DAEMON_URL", "http://127.0.0.1:9876")
    app_id = _os.environ.get("APP_ID", "digitorn-builder")

    token = _auth(daemon_url)
    client = DevClient.with_token(token, daemon_url=daemon_url)

    sid = f"probe-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=app_id, daemon_url=daemon_url, workspace="",
    )
    print(f"session_id = {sid}")

    # Fire a message. The builder usually has long turns, so we don't
    # wait for message_done — we only need the system prompt that was
    # injected, which the /history endpoint returns with
    # include_system=true regardless of turn state.
    prompt = (
        "Ne lance pas d'outil, reponds seulement en un mot: quel est "
        "ton workspace actuel?"
    )
    post = client.post_message_raw(session, prompt)
    print(f"POST status={post.get('status_code')} "
          f"correlation_id={(post.get('body') or {}).get('data', {}).get('correlation_id')}")

    # Let the turn churn briefly so history has the system prompt.
    time.sleep(3.0)

    r = client._get(
        f"/api/apps/{app_id}/sessions/{sid}/history",
        params={"include_system": "true"},
    )
    data = r.json().get("data") or {}
    messages = data.get("messages") or []
    system_msg = next((m for m in messages if m.get("role") == "system"), None)

    print()
    if system_msg is not None:
        content = system_msg.get("content", "") or ""
        print(f"  system prompt length: {len(content)} chars")
        # Look for unsubstituted {WORKSPACE} placeholder (bug!)
        if "{WORKSPACE}" in content:
            print("  !! UNSUBSTITUTED {WORKSPACE} placeholder still in prompt")
        # Dump any line that mentions 'workspace' OR the expected path
        expected = str(
            Path.home() / ".digitorn" / "workspaces" / app_id / sid
        )
        for line in content.split("\n"):
            lo = line.lower()
            if "workspace" in lo or expected.lower() in lo or "{workspace}" in lo:
                print(f"  line: {line.strip()[:220]}")
    else:
        print("  NO system message in /history")

    print()
    print(f"  session.workspace (KV): {data.get('workspace', '<not returned>')}")

    expected = str(
        Path.home() / ".digitorn" / "workspaces" / app_id / sid
    )
    print(f"  expected ws path      : {expected}")
    print(f"  expected exists?      : {Path(expected).exists()}")

    # Daemon cwd check (the toxic value the fix is supposed to reject)
    print(f"  daemon cwd (rejected) : {Path.cwd().resolve()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Scout: verify auto_approve mode bypasses validation end-to-end.

1. Deploy an app with modules.workspace.config.auto_approve: true
2. Writeback a file
3. Expect validation=approved, pending=0 immediately, no explicit approve call
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


os.environ.setdefault("DIGITORN_DEV_DAEMON_URL", "http://127.0.0.1:8100")
BASE = os.environ["DIGITORN_DEV_DAEMON_URL"]


def _req(method, path, body=None):
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


def _ok(label, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}  {extra}")
    return cond


APP_YAML = """
app:
  app_id: ws-auto-approve-scout
  name: "Auto-Approve Scout"
  version: "1.0.0"

modules:
  workspace:
    config:
      render_mode: code
      sync_to_disk: true
      auto_approve: true
  preview: {}

agents:
  - id: scribe
    role: writer
    brain:
      provider: anthropic
      model: claude-haiku-4-5
      config:
        api_key: claude-code
      temperature: 0.1
      max_tokens: 2048
    system_prompt: "You only write files. Respond with just OK."

execution:
  mode: conversation
  entry_agent: scribe
  max_turns: 5

capabilities:
  default_policy: auto
  grant:
    - module: workspace
      actions: [write, read, edit, glob, grep, delete]
"""


def main():
    print(f"daemon: {BASE}")

    # Write YAML to a temp file and deploy.
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as tf:
        tf.write(APP_YAML)
        tf_path = tf.name

    print(f"  deploying {tf_path}")
    r = _req("POST", "/api/apps/deploy", body={"yaml_path": tf_path, "force": True})
    if not r.get("success"):
        print(f"  deploy failed: {r}")
        return 2
    app_id = "ws-auto-approve-scout"

    # Deploy is async — poll until the app is actually ready.
    import time
    for _ in range(30):
        time.sleep(1)
        r = _req("GET", f"/api/apps/{app_id}")
        if r.get("success"):
            break
    else:
        print(f"  deploy never completed: {r}")
        return 2
    print(f"  deploy ready")

    # Create session with an explicit workspace so baselines persist.
    ws = Path(tempfile.gettempdir()) / f"auto-approve-scout-{os.urandom(4).hex()}"
    ws.mkdir(parents=True, exist_ok=True)
    r = _req("POST", f"/api/apps/{app_id}/sessions",
             body={"workspace_path": str(ws)})
    sid = (r.get("data") or {}).get("session_id")
    if not sid:
        print(f"  FAIL session create: {r}")
        return 2
    print(f"  session: {sid}  ws: {ws}")

    # Writeback a file (simulates agent's WsWrite, same _make_payload path).
    r = _req(
        "PUT",
        f"/api/apps/{app_id}/sessions/{sid}/workspace/files/hello.txt",
        body={"content": "auto approved\n", "auto_approve": False},
    )
    if _req.__doc__ and False:
        pass

    if not r.get("success"):
        print(f"  FAIL writeback: {r}")
        return 2

    # Verify payload came back auto-approved even with auto_approve=False on the
    # writeback itself, because the module-level config overrides.
    r = _req(
        "GET",
        f"/api/apps/{app_id}/sessions/{sid}/workspace/files/hello.txt?include_baseline=true",
    )
    d = r.get("data") or {}
    p = d.get("payload") or {}

    passed = 0
    failed = 0

    if _ok("validation == approved (module auto_approve)",
           p.get("validation") == "approved",
           f"(got {p.get('validation')!r})"):
        passed += 1
    else:
        failed += 1

    if _ok("insertions_pending == 0", p.get("insertions_pending") == 0,
           f"(got {p.get('insertions_pending')})"):
        passed += 1
    else:
        failed += 1

    if _ok("deletions_pending == 0", p.get("deletions_pending") == 0):
        passed += 1
    else:
        failed += 1

    # baseline should exist and match the written content
    if _ok("baseline content matches", d.get("baseline") == "auto approved\n",
           f"(got {d.get('baseline')!r})"):
        passed += 1
    else:
        failed += 1

    if _ok("unified_diff_pending empty (no pending)",
           not (d.get("unified_diff_pending") or "").strip()):
        passed += 1
    else:
        failed += 1

    # Edit the file to prove auto_approve re-snapshots on every write.
    _req(
        "PUT",
        f"/api/apps/{app_id}/sessions/{sid}/workspace/files/hello.txt",
        body={"content": "second revision\n", "auto_approve": False},
    )
    r = _req(
        "GET",
        f"/api/apps/{app_id}/sessions/{sid}/workspace/files/hello.txt?include_baseline=true",
    )
    d = r.get("data") or {}
    p = d.get("payload") or {}
    if _ok("after edit: validation still approved", p.get("validation") == "approved"):
        passed += 1
    else:
        failed += 1
    if _ok("after edit: pending still 0",
           p.get("insertions_pending") == 0 and p.get("deletions_pending") == 0,
           f"(ins={p.get('insertions_pending')} del={p.get('deletions_pending')})"):
        passed += 1
    else:
        failed += 1
    if _ok("after edit: baseline matches new content",
           d.get("baseline") == "second revision\n",
           f"(got {d.get('baseline')!r})"):
        passed += 1
    else:
        failed += 1

    print(f"\n  {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

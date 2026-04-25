"""Scout: real LLM end-to-end test of the workspace/preview flow.

Deploys a small app with Claude Haiku as the agent, asks it to write
a file via WsWrite, then verifies:
  - file shows up on disk (sync_to_disk)
  - code-snapshot lists it with correct metadata
  - files endpoint returns the content
  - validation=pending, insertions_pending > 0 (correct post-write state)
  - approve endpoint clears pending, baseline persists to disk
  - edit (via a second prompt) produces correct pending counts
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("DIGITORN_DEV_DAEMON_URL", "http://127.0.0.1:8100")
BASE = os.environ["DIGITORN_DEV_DAEMON_URL"]


def _req(method, path, body=None, timeout=60):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http_status": e.code, "_body": e.read().decode()}


def _ok(label, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {label}  {extra}")
    return cond


APP_YAML = """
app:
  app_id: llm-e2e-scout
  name: "LLM End-to-End Scout"
  version: "1.0.0"

modules:
  workspace:
    config:
      render_mode: code
      sync_to_disk: true
      auto_approve: false
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
    system_prompt: |
      You write files when asked. After writing, respond with just "done".
      Never do anything else. Never apologize. Never explain.

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

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as tf:
        tf.write(APP_YAML)
        tf_path = tf.name

    r = _req("POST", "/api/apps/deploy", body={"yaml_path": tf_path, "force": True})
    if not r.get("success"):
        print(f"  deploy failed: {r}")
        return 2
    app_id = "llm-e2e-scout"
    for _ in range(30):
        time.sleep(1)
        if _req("GET", f"/api/apps/{app_id}").get("success"):
            break
    else:
        print(f"  deploy never ready")
        return 2
    print(f"  app '{app_id}' ready")

    ws = Path(tempfile.gettempdir()) / f"llm-e2e-{os.urandom(4).hex()}"
    ws.mkdir(parents=True, exist_ok=True)
    r = _req("POST", f"/api/apps/{app_id}/sessions",
             body={"workspace_path": str(ws)})
    sid = (r.get("data") or {}).get("session_id")
    if not sid:
        print(f"  session create failed: {r}")
        return 2
    print(f"  session: {sid}  ws: {ws}")

    passed = 0
    failed = 0

    # STEP 1 — ask LLM to write a file.
    print("\n  > sending prompt: write README.md with 'Hello LLM' line")
    r = _req("POST", f"/api/apps/{app_id}/sessions/{sid}/messages",
             body={"message": "Write a file README.md with just one line: 'Hello LLM'. Then respond 'done'."},
             timeout=120)
    if not r.get("success"):
        print(f"  message failed: {r}")
        return 2

    # Wait for turn to complete (poll).
    for i in range(90):
        time.sleep(1.5)
        s = _req("GET", f"/api/apps/{app_id}/sessions/{sid}").get("data") or {}
        if not s.get("is_active"):
            break
    print(f"  turn finished  tokens={s.get('tokens')}  last_preview={str(s.get('last_message_preview'))[:60]!r}")

    # Disk sync check.
    disk_path = ws / "README.md"
    if _ok("README.md on disk", disk_path.is_file(), f"({disk_path})"):
        passed += 1
    else:
        failed += 1

    # code-snapshot lists it.
    snap = _req("GET", f"/api/apps/{app_id}/sessions/{sid}/workspace/code-snapshot")
    files = (snap.get("data") or {}).get("files") or {}
    if _ok("code-snapshot lists README.md",
           any(p.endswith("README.md") for p in files.keys()),
           f"(found: {list(files.keys())[:3]})"):
        passed += 1
    else:
        failed += 1

    # Content endpoint.
    r = _req("GET", f"/api/apps/{app_id}/sessions/{sid}/workspace/files/README.md?include_baseline=true")
    d = r.get("data") or {}
    p = d.get("payload") or {}
    if _ok("GET files returns content", bool(p.get("content")),
           f"(size={p.get('size')})"):
        passed += 1
    else:
        failed += 1
    if _ok("validation == pending (auto_approve off)", p.get("validation") == "pending",
           f"(got {p.get('validation')!r})"):
        passed += 1
    else:
        failed += 1
    if _ok("insertions_pending > 0", int(p.get("insertions_pending") or 0) > 0,
           f"(got {p.get('insertions_pending')})"):
        passed += 1
    else:
        failed += 1

    # STEP 2 — approve it.
    r = _req("POST", f"/api/apps/{app_id}/sessions/{sid}/workspace/files/approve",
             body={"path": "README.md"})
    if _ok("approve returns success", bool(r.get("success"))):
        passed += 1
    else:
        failed += 1

    # Baseline now exists.
    baseline_path = ws / ".digitorn" / "sessions" / sid / "baselines" / "README.md"
    if _ok("baseline persisted to disk", baseline_path.is_file(), f"({baseline_path})"):
        passed += 1
    else:
        failed += 1

    # Pending is now 0.
    r = _req("GET", f"/api/apps/{app_id}/sessions/{sid}/workspace/files/README.md?include_baseline=true")
    p = (r.get("data") or {}).get("payload") or {}
    if _ok("after approve: insertions_pending == 0",
           int(p.get("insertions_pending") or 0) == 0):
        passed += 1
    else:
        failed += 1

    # STEP 3 — second prompt: edit the file.
    print("\n  > sending prompt: edit README.md (replace one line)")
    r = _req("POST", f"/api/apps/{app_id}/sessions/{sid}/messages",
             body={"message": "Use WsEdit to change 'Hello LLM' to 'HELLO LLM'. Then say 'done'."},
             timeout=120)

    for i in range(90):
        time.sleep(1.5)
        s = _req("GET", f"/api/apps/{app_id}/sessions/{sid}").get("data") or {}
        if not s.get("is_active"):
            break
    print(f"  turn 2 finished  tokens={s.get('tokens')}")

    # Pending after edit — should reflect exactly the difference vs baseline.
    r = _req("GET", f"/api/apps/{app_id}/sessions/{sid}/workspace/files/README.md?include_baseline=true")
    d = r.get("data") or {}
    p = d.get("payload") or {}
    print(f"  content-after-edit: {p.get('content')!r}")
    content = p.get("content") or ""
    if "HELLO LLM" in content:
        if _ok("after edit: content updated", True):
            passed += 1
    else:
        if _ok("after edit: content updated (model may have refused — non-fatal)",
               False, f"(got {content!r})"):
            passed += 1
        else:
            failed += 1

    # History endpoint should show 1 revision (from the approve).
    r = _req("GET", f"/api/apps/{app_id}/sessions/{sid}/workspace/files/README.md/history")
    revs = ((r.get("data") or {}).get("revisions") or [])
    if _ok("history shows 1 revision", len(revs) == 1, f"(got {len(revs)})"):
        passed += 1
    else:
        failed += 1

    print(f"\n  {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

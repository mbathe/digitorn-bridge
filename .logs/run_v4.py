"""V4 integration driver — sends a brief to digitorn-builder on :8100
and monitors _state/tests.json for Phase 6 completion evidence.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

os.environ.setdefault("DIGITORN_DEV_DAEMON_URL", "http://127.0.0.1:8100")

from digitorn.testing.client import DevClient

BRIEF = (
    "Build me a simple echo-chatbot app — one agent, conversation mode, "
    "repeats the user message back in uppercase. Name it echo-v4. "
    "Compile, deploy, and run the Phase 6 smoke test."
)

ws_path = Path(tempfile.gettempdir()) / f"digitorn-v4-{uuid.uuid4().hex[:8]}"
ws_path.mkdir(parents=True, exist_ok=True)

c = DevClient()
print(f"[v4] daemon_url={c.daemon_url}", flush=True)

sess = c.create_session("digitorn-builder", workspace=str(ws_path))
print(f"[v4] session={sess.session_id} ws={sess.workspace}", flush=True)

t0 = time.monotonic()
post = c.post_message_raw(sess, BRIEF)
print(
    f"[v4] posted in {time.monotonic()-t0:.1f}s "
    f"status={post.get('status_code')} "
    f"correlation_id={(post.get('body') or {}).get('data', {}).get('correlation_id')}",
    flush=True,
)

deadline = time.monotonic() + 600
tests_path = Path(sess.workspace) / "_state" / "tests.json"
deploy_path = Path(sess.workspace) / "_state" / "deploy.json"
compile_path = Path(sess.workspace) / "_state" / "compile.json"

seen = {"compile": 0, "deploy": False, "tests": 0, "success": False}
last_tick = time.monotonic()
while time.monotonic() < deadline:
    time.sleep(5)
    dur = time.monotonic() - t0

    if compile_path.exists():
        try:
            cj = json.loads(compile_path.read_text(encoding="utf-8"))
            seen["compile"] = cj.get("error_count", 0)
        except Exception:
            pass
    if deploy_path.exists() and not seen["deploy"]:
        seen["deploy"] = True
        print(f"[v4] +{dur:.0f}s DEPLOY written: {deploy_path.read_text(encoding='utf-8')[:200]}", flush=True)
    if tests_path.exists():
        try:
            tj = json.loads(tests_path.read_text(encoding="utf-8"))
            tlist = tj.get("tests") or []
            if len(tlist) != seen["tests"]:
                seen["tests"] = len(tlist)
                latest = tlist[-1] if tlist else {}
                print(
                    f"[v4] +{dur:.0f}s TEST#{seen['tests']} "
                    f"success={latest.get('success')} "
                    f"response={str(latest.get('response') or '')[:80]!r}",
                    flush=True,
                )
            if any(t.get("success") for t in tlist):
                seen["success"] = True
                print(f"[v4] +{dur:.0f}s SUCCESS — stopping poll.", flush=True)
                break
        except Exception:
            pass
    if time.monotonic() - last_tick > 30:
        last_tick = time.monotonic()
        print(f"[v4] +{dur:.0f}s heartbeat — waiting...", flush=True)

print(f"[v4] final state: {seen}", flush=True)
sys.exit(0 if seen["success"] else 1)

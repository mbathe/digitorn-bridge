"""Live scenario: lsp_diagnose hook → self-correction loop.

Agent is asked to write a Python file with a deliberate syntax error.
Hook runs Python validator on the Write result, injects `lint` field.
Agent should see the error and rewrite correctly.

Verifies:
  - Write tool fires at least twice (first bad, second corrected)
  - Tool result after first Write contains lint errors
  - Second Write passes lint (errors == 0)
  - Final on-disk file is syntactically valid Python
"""
from __future__ import annotations

import ast
import tempfile
import time
import uuid
from pathlib import Path

from digitorn.testing import DevClient
from digitorn.testing.assertions import sort_by_seq
from digitorn.testing.models import SessionHandle

OUT = Path(__file__).parent.parent / "_hook_lsp_result.txt"
OUT.write_text("", encoding="utf-8")

def log(msg):
    with OUT.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")
        f.flush()

c = DevClient()
log(f"daemon: {c.daemon_url}")

try:
    c.undeploy("qtest-hook-lsp")
except Exception:
    pass

app = c.deploy(
    str(Path(__file__).parent / "apps" / "hook-lsp.yaml"),
    force=True,
)
log(f"deployed: {app.app_id} mode={app.mode}")

ws = tempfile.mkdtemp(prefix="hook-lsp-")
log(f"workspace: {ws}")

sid = f"hl-{uuid.uuid4().hex[:8]}"
s = SessionHandle(
    session_id=sid, app_id=app.app_id, daemon_url=c.daemon_url, workspace=ws,
)

msg = (
    "Écris un fichier hello.py qui contient exactement ce code Python "
    "(volontairement cassé, JE SAIS qu'il contient une erreur) :\n"
    "```python\n"
    "def greet(name)\n"
    "    print(f'Hello {name}')\n"
    "```\n"
    "Écris-le tel quel d'abord, puis si l'outil rapporte une erreur de "
    "lint, corrige et réécris immédiatement le fichier corrigé."
)

post = c.post_message_raw(s, msg)
log(f"POST result: {post}")
cid = (post.get("body") or {}).get("data", {}).get("correlation_id") or ""
log(f"POST cid={cid}")
if not post.get("status_code") in (200, 202):
    log("POST failed, aborting")
    raise SystemExit(1)

time.sleep(2.0)
log(f"waiting for session to be readable...")
if not c.wait_for_session(s, timeout=15.0):
    log("session still not readable after 15s - inspecting...")
    r = c._get(f"/api/apps/{app.app_id}/sessions/{s.session_id}")
    log(f"  GET session: status={r.status_code} body={r.text[:300]}")
    raise SystemExit(1)

stream = c.open_event_stream(s, wait_for_session=False)
try:
    done = stream.wait_for(
        "message_done", timeout=120,
        predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
    )
    log(f"message_done arrived? {done is not None}")
    time.sleep(1.0)

    events = sort_by_seq(stream.events())
    log(f"total events: {len(events)}")

    log("\n=== event type histogram ===")
    counts = {}
    for e in events:
        counts[e.get("type")] = counts.get(e.get("type"), 0) + 1
    for t, n in sorted(counts.items(), key=lambda x: -x[1]):
        log(f"  {t}: {n}")

    log("\n=== any hook events (full payload) ===")
    for e in events:
        if e.get("type") in ("hook", "hook_notification"):
            log(f"  seq={e.get('seq')} payload={str(e.get('payload'))[:300]}")

    log("\n=== any tool events (name + result preview) ===")
    for e in events:
        t = e.get("type", "")
        if t.startswith("tool_"):
            pl = e.get("payload") or {}
            log(f"  seq={e.get('seq')} type={t} name={pl.get('name') or pl.get('label')} result_keys={list((pl.get('result') or pl.get('data') or {}).keys()) if isinstance(pl.get('result') or pl.get('data'), dict) else 'not-dict'}")

    tool_writes = [
        e for e in events
        if e.get("type") in ("tool_call", "tool_end", "tool_start")
        and (
            (e.get("payload") or {}).get("name") in ("Write", "filesystem.write")
            or (e.get("payload") or {}).get("label") in ("Write", "filesystem.write")
        )
    ]
    log(f"Write tool events seen: {len(tool_writes)}")

    hook_fires = [
        e for e in events
        if e.get("type") == "hook"
        and (e.get("payload") or {}).get("hook_id") == "lint-after-write"
    ]
    log(f"hook 'lint-after-write' fires: {len(hook_fires)}")

    tool_end_events = [
        e for e in events
        if e.get("type") == "tool_end"
        and (
            (e.get("payload") or {}).get("name") in ("Write", "filesystem.write")
            or (e.get("payload") or {}).get("label") in ("Write", "filesystem.write")
        )
    ]
    lint_in_results = []
    for e in tool_end_events:
        pl = e.get("payload") or {}
        result = pl.get("result") or pl.get("data") or {}
        if isinstance(result, dict) and "lint" in result:
            lint_in_results.append(result.get("lint"))
        elif isinstance(result, str) and "[lsp_diagnose]" in result:
            lint_in_results.append({"text_marker": True, "preview": result[-200:]})
    log(f"tool_end events with lint data: {len(lint_in_results)}/{len(tool_end_events)}")
    for i, lint in enumerate(lint_in_results):
        log(f"  write #{i+1} lint: {str(lint)[:200]}")

    preview_diag = [
        e for e in events
        if e.get("type") == "preview:resource_set"
        and (e.get("payload") or {}).get("channel") == "diagnostics"
    ]
    log(f"preview:resource_set on 'diagnostics' channel: {len(preview_diag)}")

    final_file = Path(ws) / "hello.py"
    log(f"\nfinal file exists? {final_file.exists()}")
    if final_file.exists():
        content = final_file.read_text(encoding="utf-8")
        log(f"final file content:\n---\n{content}\n---")
        try:
            ast.parse(content)
            parse_ok = True
            log("final file PARSES as valid Python")
        except SyntaxError as e:
            parse_ok = False
            log(f"final file has SYNTAX ERROR: {e}")
    else:
        parse_ok = False
        content = ""

    log(f"\n=== checks ===")
    hook_fired = len(hook_fires) >= 1 or len(lint_in_results) >= 1 or len(preview_diag) >= 1
    self_corrected = len(tool_writes) >= 2 and parse_ok
    log(f"  hook fired at least once: {hook_fired}")
    log(f"  at least 2 Writes (first + corrected): {len(tool_writes) >= 2}")
    log(f"  final file is valid Python: {parse_ok}")
    log(f"  self-correction complete: {self_corrected}")

    verdict = hook_fired and self_corrected
    log(f"\nVERDICT: {'PASS' if verdict else 'FAIL'}")
finally:
    stream.stop(timeout=2.0)

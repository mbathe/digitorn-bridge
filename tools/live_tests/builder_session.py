"""Live session driver for digitorn-builder — Vague 0 repro.

Feeds the builder a realistic brief ("build me a live task manager
with React preview"), then STREAMS every event into a structured log
so we can diagnose friction without guessing:
  - what tools did the agent call, in which order?
  - did any call 500 / time out / return empty?
  - how long did each turn take end-to-end?
  - did a preview:snapshot fire? what does the generated YAML look
    like at each step?
  - did the agent re-ask the same question (confused prompt)?

Writes the full timeline to ``.stress_test/results/builder_<sid>.json``
so we can inspect offline and compare iterations.

No assertions — this is pure observation. Fixes come after.
"""
from __future__ import annotations

import json
import os as _os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from digitorn.testing.client import DevClient
from digitorn.testing.models import SessionHandle


_DAEMON = _os.environ.get("DAEMON_URL", "http://127.0.0.1:9876")
_APP = "digitorn-builder"


_SIMPLE_BRIEF = (
    "Construis-moi une app Digitorn trivale: un echo chatbot. Un seul "
    "agent, mode conversation, qui renvoie le message de l'utilisateur "
    "en majuscules. Pas d'UI, pas de preview, juste du texte. Utilise "
    "deepseek comme brain. Deploie et auto-teste via dev_tools.Chat: "
    "envoie 'bonjour' et verifie que la reponse contient 'BONJOUR'."
)

_TASK_MANAGER_BRIEF = (
    "Construis-moi une app Digitorn: un task manager conversationnel avec "
    "preview live. Specifications:\n"
    "\n"
    "- Mode conversation, un seul agent qui ecoute des messages utilisateur.\n"
    "- L'utilisateur peut dire: 'ajoute la tache X', 'coche la tache 2', "
    "'supprime la tache 3', 'liste mes taches'.\n"
    "- Une UI React live a droite affiche la liste des taches en temps reel "
    "avec des animations lors d'ajout/completion.\n"
    "- Utilise le module memory pour persister les taches.\n"
    "- Utilise workspace+preview pour l'UI React live.\n"
    "- L'app doit se deployer sans erreur et la preview doit fonctionner au "
    "premier essai.\n"
    "\n"
    "Quand tu as fini, auto-teste en creant une session et en envoyant 'ajoute "
    "la tache Acheter du pain' via dev_tools, puis verifie que la tache "
    "apparait dans la preview. Propose-moi le YAML final avant deploiement."
)

_BRIEF = _SIMPLE_BRIEF if _os.environ.get("SIMPLE", "0") == "1" else _TASK_MANAGER_BRIEF


def _auth() -> str:
    email = "builder-driver@test.local"
    password = "BuilderPassword123!"
    for path in ("/auth/login", "/auth/register"):
        body: dict[str, Any] = {"email": email, "password": password}
        if path.endswith("register"):
            body["username"] = "builder-driver"
            body["name"] = "builder-driver"
        r = httpx.post(f"{_DAEMON}{path}", json=body, timeout=15.0)
        if r.status_code == 200:
            return r.json()["access_token"]
    raise RuntimeError("auth failed")


def _summarize_event(env: dict[str, Any]) -> dict[str, Any]:
    t = env.get("type", "")
    p = env.get("payload") or {}
    out: dict[str, Any] = {
        "seq": env.get("seq"),
        "type": t,
        "ts": env.get("ts"),
    }
    if t == "tool_call":
        out["name"] = p.get("name")
        params = p.get("params")
        if isinstance(params, dict):
            keys = sorted(params.keys())
            out["param_keys"] = keys
            # Keep content values short — we want shape, not dumps.
            out["param_preview"] = {
                k: (
                    params[k] if not isinstance(params[k], (str, list, dict))
                    else (
                        params[k][:120]
                        if isinstance(params[k], str)
                        else f"<{type(params[k]).__name__} len={len(params[k])}>"
                    )
                )
                for k in keys[:8]
            }
        res = p.get("result")
        if isinstance(res, dict):
            out["result_keys"] = sorted(res.keys())
            if "error" in res and res["error"]:
                out["result_error"] = str(res["error"])[:200]
    elif t == "tool_start":
        out["name"] = p.get("name")
    elif t == "memory_update":
        out["action"] = p.get("action")
    elif t == "hook":
        out["hook_id"] = p.get("hook_id")
        out["action_type"] = p.get("action_type")
        out["phase"] = p.get("phase")
    elif t == "assistant_stream_snapshot":
        content = p.get("content", "") or ""
        out["chars"] = len(content)
    elif t == "result":
        out["has_content"] = bool(p.get("content"))
        out["tool_calls_count"] = p.get("tool_calls_count")
        out["turns_used"] = p.get("turns_used")
        out["truncated"] = p.get("truncated")
        out["error"] = p.get("error")
    elif t == "message_done":
        out["correlation_id"] = p.get("correlation_id")
    elif t == "message_cancelled":
        out["reason"] = p.get("reason")
    elif t == "error":
        out["error"] = p.get("error")
        out["code"] = p.get("code")
        out["category"] = p.get("category")
        out["fatal"] = p.get("fatal")
    elif t == "preview:resource_set":
        out["channel"] = p.get("channel")
        out["id"] = p.get("id")
    elif t == "preview:snapshot":
        r = p.get("resources") or {}
        out["channels"] = sorted(r.keys())
        files = r.get("files") or {}
        out["file_count"] = len(files)
    return out


def main() -> int:
    token = _auth()
    client = DevClient.with_token(token, daemon_url=_DAEMON)

    sid = f"builder-v0-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=_APP, daemon_url=_DAEMON, workspace="",
    )
    print(f"session={sid}")

    # Watchdog baseline — we want to know if the builder turn stalls
    # the loop.
    base_wd = httpx.get(f"{_DAEMON}/health", timeout=5.0).json()
    base_stalls = (base_wd.get("event_loop_watchdog") or {}).get("stalls_total") or 0

    t0 = time.perf_counter()
    post = client.post_message_raw(session, _BRIEF)
    cid = (post.get("body") or {}).get("data", {}).get("correlation_id") or ""
    print(f"POST ok correlation_id={cid}")

    # Background auto-approver. Builder agents routinely emit
    # `approval_request` events (for `ask_user` + deploy confirmations).
    # This driver always says YES so the full pipeline runs to Phase 6.
    # CRITICAL: the approver MUST keep running even after the client
    # stream's wait_for times out. Otherwise an approval emitted late
    # in the turn hangs forever and we never see message_done.
    import threading as _th
    stop_approver = _th.Event()

    def _approve_loop() -> None:
        seen: set[str] = set()
        while not stop_approver.is_set():
            try:
                r = httpx.get(
                    f"{_DAEMON}/api/apps/{_APP}/approvals",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=5.0,
                )
                for item in (r.json().get("data") or {}).get("approvals", []) or []:
                    rid = item.get("request_id") or item.get("id") or ""
                    if not rid or rid in seen:
                        continue
                    seen.add(rid)
                    payload = {
                        "request_id": rid,
                        "approved": True,
                        "message": "deploy now",
                        "payload": "deploy now",
                    }
                    httpx.post(
                        f"{_DAEMON}/api/apps/{_APP}/approve",
                        headers={"Authorization": f"Bearer {token}"},
                        json=payload, timeout=5.0,
                    )
                    print(f"[auto-approve] resolved {rid}")
            except Exception:
                pass
            stop_approver.wait(2.0)

    approver = _th.Thread(target=_approve_loop, daemon=True, name="auto-approve")
    approver.start()

    # Budget: up to 30 min wall clock — generous enough for DeepSeek to
    # plow through a Lovable-scale brief (Phase 0→6). We poll the DB
    # for message_done rather than relying on a single long wait_for,
    # because that let us survive approval roundtrips that arrive
    # late.
    total_budget_s = float(_os.environ.get("BUDGET_S", "1800"))

    stream = client.open_event_stream(session, wait_for_session=True)
    done = None
    try:
        import sqlite3 as _sql
        deadline = time.perf_counter() + total_budget_s
        done = stream.wait_for(
            "message_done", timeout=min(900.0, total_budget_s),
            predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
        )
        # If wait_for timed out but we still have budget, poll the DB
        # directly — the turn could still be running server-side while
        # the Socket.IO stream sat idle.
        while done is None and time.perf_counter() < deadline:
            try:
                con = _sql.connect("digitorn.db")
                row = con.execute(
                    "SELECT 1 FROM session_events WHERE session_id=? AND type='message_done' LIMIT 1",
                    (sid,),
                ).fetchone()
                con.close()
                if row:
                    done = {"type": "message_done", "polled": True}
                    break
            except Exception:
                pass
            time.sleep(5.0)
    finally:
        # Run the approver a little longer in case a final `ask_user`
        # comes in for the post-deploy summary.
        time.sleep(2.0)
        stop_approver.set()
        events_raw = list(stream.events())
        stream.stop(timeout=3.0)
        approver.join(timeout=2.0)

    elapsed = time.perf_counter() - t0

    wd_after = httpx.get(f"{_DAEMON}/health", timeout=5.0).json()
    after_stalls = (wd_after.get("event_loop_watchdog") or {}).get("stalls_total") or 0

    timeline = [
        _summarize_event(env) for env in events_raw
        if env.get("session_id") == sid
    ]
    by_type: dict[str, int] = {}
    for env in timeline:
        by_type[env.get("type") or "?"] = by_type.get(env.get("type") or "?", 0) + 1

    tool_calls = [e for e in timeline if e["type"] == "tool_call"]
    errors = [e for e in timeline if e["type"] in ("error",)]
    hooks = [e for e in timeline if e["type"] == "hook" and e.get("action_type")]
    approvals_raw = [e for e in events_raw if e.get("type") == "approval_request"]

    summary = {
        "session_id": sid,
        "prompt": _BRIEF,
        "elapsed_s": round(elapsed, 1),
        "message_done": done is not None,
        "event_counts_by_type": dict(sorted(by_type.items())),
        "tool_calls_count": len(tool_calls),
        "tool_calls_timeline": [
            {
                "seq": tc.get("seq"),
                "name": tc.get("name"),
                "error": tc.get("result_error"),
            }
            for tc in tool_calls
        ],
        "error_events": errors,
        "hook_events_unique": sorted({
            f"{h.get('hook_id')}:{h.get('action_type')}"
            for h in hooks
        }),
        "approvals_pending": len(approvals_raw),
        "new_stalls": after_stalls - base_stalls,
    }

    # Final YAML written by the agent (if any)
    hist = client._get(
        f"/api/apps/{_APP}/sessions/{sid}/history",
        params={"include_system": "false"},
    ).json().get("data") or {}

    pending = hist.get("pending_queue") or []
    summary["pending_queue_end"] = len(pending)
    summary["turn_active_end"] = hist.get("turn_active")

    preview = hist.get("preview_snapshot") or {}
    resources = (preview.get("resources") or {})
    files = resources.get("files") or {}
    summary["workspace_files"] = sorted(files.keys())
    if "app.yaml" in files:
        app_yaml_entry = files["app.yaml"]
        content = app_yaml_entry.get("content", "")
        summary["app_yaml_size"] = len(content) if isinstance(content, str) else 0
        summary["app_yaml_preview"] = (
            content[:500] if isinstance(content, str) else ""
        )

    # Dump everything
    out_dir = Path(".stress_test/results")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"builder_{sid}.json"
    out_path.write_text(
        json.dumps({"summary": summary, "timeline": timeline}, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"saved -> {out_path}")

    # Human summary on stdout
    print()
    print(f"elapsed           : {elapsed:.1f}s")
    print(f"message_done      : {done is not None}")
    print(f"tool_calls        : {len(tool_calls)}")
    print(f"errors            : {len(errors)}")
    print(f"new stalls        : {after_stalls - base_stalls}")
    print(f"workspace files   : {summary['workspace_files']}")
    print(f"approvals pending : {summary['approvals_pending']}")
    print(f"pending queue     : {summary['pending_queue_end']}")
    print(f"turn_active end   : {summary['turn_active_end']}")
    print()
    print("tools called (in order):")
    for tc in tool_calls[:30]:
        tag = "!!" if tc.get("result_error") else "  "
        print(f"  {tag} seq={tc['seq']:>5}  {tc.get('name')}")
        if tc.get("result_error"):
            print(f"         err: {tc['result_error'][:140]}")

    return 0 if (done is not None and len(errors) == 0) else 1


if __name__ == "__main__":
    sys.exit(main())

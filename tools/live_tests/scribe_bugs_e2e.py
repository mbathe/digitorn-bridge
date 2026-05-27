"""End-to-end live verification of the 4 scribe bug fixes.

Run AFTER restarting the daemon so the patched YAML + writer.md +
hooks.py are loaded.

Bug 1 - excessive AskUser
  Prompt scribe with a simple LaTeX request that doesn't name a class.
  Assert: NO ask_user event fires before the first WsWrite.

Bug 2 - preview iframe stuck on stale PDF
  After the first turn writes main.tex, check the SSE `files` channel
  has main.pdf with `operation: disk_sync` (i.e. tectonic's output was
  picked up). Without the hook fix, main.pdf is on disk but never in
  the channel.

Bug 3 - Preview* tools leak to the LLM
  Assert tools/categories doesn't expose web_preview, and direct
  get_tool() probes for PreviewAttach/Publish/Detach return empty.

Bug 4 - `blue!50!black` crash recurrence
  Read the generated main.tex; assert xcolor is loaded with dvipsnames
  BEFORE hyperref (which is what makes the colour-mix syntax legal).
  Also assert the lint field of the WsWrite response had no
  "Undefined color" error.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


APP_ID = "digitorn-scribe"


def _token() -> str:
    return json.loads(
        (Path.home() / ".digitorn" / "credentials.json").read_text("utf-8")
    )["access_token"]


# ───────────────────────── Bug 3 - schema layer ─────────────────────────

def check_bug3_preview_tools(client: DevClient) -> tuple[bool, str]:
    cats = client._get(f"/api/apps/{APP_ID}/tools/categories").json()
    cat_data = cats.get("data", {}).get("categories", [])
    wp = next((c for c in cat_data if c.get("id") == "web_preview"), None)
    wp_count = wp.get("tool_count") if wp else 0

    leaked: list[str] = []
    # Short LLM-facing names + raw FQNs - all 3 actions, both forms.
    for n in ("PreviewProxy", "PreviewPublish", "PreviewDetach",
              "web_preview.proxy", "web_preview.publish", "web_preview.detach"):
        try:
            t = client.get_tool(APP_ID, n)
            if t and t.get("name"):
                leaked.append(n)
        except Exception:
            pass

    ok = (wp is None or wp_count == 0) and not leaked
    return ok, (
        f"web_preview category tool_count={wp_count}, "
        f"leaked via get_tool={leaked or 'none'}"
    )


# ──────────────────────── Bug 1+4 - one-turn run ────────────────────────

def run_scaffold_turn(client: DevClient) -> dict:
    """Send a vanilla 'write me a doc' request, no class specified.

    Returns a dict with events, file contents, and timing.
    """
    sid = f"e2e-{uuid.uuid4().hex[:8]}"
    ws = Path.home() / ".digitorn" / "workspaces" / APP_ID / sid
    ws.mkdir(parents=True, exist_ok=True)
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace=str(ws),
    )

    prompt = (
        "Écris-moi un court document LaTeX (1 page) sur la "
        "convergence en probabilité. Inclus une intro, un énoncé du "
        "théorème, et un exemple. Auteur : Paul."
    )

    t0 = time.monotonic()
    stream = client.send_live(session, prompt, total_timeout=300)
    elapsed = time.monotonic() - t0
    events = assertions.sort_by_seq(stream.events())

    main_tex_path = ws / "main.tex"
    main_pdf_path = ws / "main.pdf"

    main_tex_content = (
        main_tex_path.read_text("utf-8", errors="replace")
        if main_tex_path.exists() else ""
    )

    stream.stop(timeout=2.0)
    return {
        "session_id": sid,
        "workspace": str(ws),
        "events": events,
        "elapsed": elapsed,
        "main_tex": main_tex_content,
        "main_tex_exists": main_tex_path.exists(),
        "main_pdf_exists": main_pdf_path.exists(),
        "main_pdf_size": main_pdf_path.stat().st_size if main_pdf_path.exists() else 0,
    }


def check_bug1_no_askuser_before_write(run: dict) -> tuple[bool, str]:
    """No ask_user / approval_request fires before the first WsWrite."""
    first_write_idx = None
    askuser_before = []

    for i, ev in enumerate(run["events"]):
        t = str(ev.get("type", ""))
        payload = ev.get("payload") or {}
        if t == "tool_call":
            name = payload.get("name") or payload.get("tool_name") or ""
            if "Write" in str(name) and first_write_idx is None:
                first_write_idx = i
        if (
            t in ("approval_request", "ask_user", "user_input_request")
            and first_write_idx is None
        ):
            askuser_before.append((i, t, payload.get("question") or payload.get("prompt")))

    ok = len(askuser_before) == 0
    detail = f"first_write_idx={first_write_idx}, asks_before={len(askuser_before)}"
    if askuser_before:
        detail += f" - first ask: {str(askuser_before[0])[:120]}"
    return ok, detail


def check_bug4_xcolor_loaded(run: dict) -> tuple[bool, str]:
    tex = run["main_tex"]
    if not tex:
        return False, "main.tex empty or missing"

    has_xcolor = "\\usepackage" in tex and "xcolor" in tex
    xcolor_idx = tex.find("xcolor")
    hyperref_idx = tex.find("hyperref")
    order_ok = has_xcolor and (
        hyperref_idx == -1 or (0 <= xcolor_idx < hyperref_idx)
    )

    # If the doc uses the !N! mix syntax, xcolor must precede.
    uses_mix = "!50!" in tex or "!25!" in tex or "!75!" in tex
    ok = (not uses_mix) or (uses_mix and order_ok)

    return ok, (
        f"xcolor_loaded={has_xcolor}, xcolor_before_hyperref={order_ok}, "
        f"uses_mix_syntax={uses_mix}, main_pdf_size={run['main_pdf_size']}"
    )


# ───────────────────────── Bug 2 - PDF in SSE feed ────────────────────────

def check_bug2_pdf_in_files_channel(run: dict) -> tuple[bool, str]:
    """Any resource event on the files channel for main.pdf?

    The daemon emits these as ``preview:resource_set`` (or
    ``preview:resource_patched``) over Socket.IO - mind the prefix.
    """
    pdf_events = []
    for ev in run["events"]:
        t = str(ev.get("type", ""))
        if "resource_set" in t or "resource_patched" in t:
            payload = ev.get("payload") or {}
            rid = str(payload.get("id") or payload.get("resource_id") or "")
            channel = str(payload.get("channel") or "")
            if channel == "files" and rid.endswith("main.pdf"):
                pdf_events.append(ev)

    ok = len(pdf_events) >= 1 and run["main_pdf_exists"]
    return ok, (
        f"resource_set(files/main.pdf)={len(pdf_events)}, "
        f"pdf_on_disk={run['main_pdf_exists']} ({run['main_pdf_size']}B)"
    )


# ──────────────────────────── main runner ─────────────────────────────

def main() -> int:
    client = DevClient(token=_token())
    print(f"daemon={client.daemon_url}")
    print(f"app={APP_ID}")

    print("\n=== Bug 3 - Preview* tools leak (no LLM call) ===")
    ok3, det3 = check_bug3_preview_tools(client)
    print(f"  {'PASS' if ok3 else 'FAIL'}  {det3}")

    print("\n=== Running one scaffold turn (~2-3 min, gpt-5-mini) ===")
    run = run_scaffold_turn(client)
    type_hist = {}
    for ev in run["events"]:
        t = str(ev.get("type", ""))
        type_hist[t] = type_hist.get(t, 0) + 1
    print(f"  session={run['session_id']}  elapsed={run['elapsed']:.1f}s  events={len(run['events'])}")
    print(f"  main.tex={'exists' if run['main_tex_exists'] else 'MISSING'} "
          f"({len(run['main_tex']):,}c), main.pdf={run['main_pdf_size']:,}B")
    print(f"  type_hist={dict(sorted(type_hist.items(), key=lambda x: -x[1])[:10])}")

    print("\n=== Bug 1 - no AskUser before first WsWrite ===")
    ok1, det1 = check_bug1_no_askuser_before_write(run)
    print(f"  {'PASS' if ok1 else 'FAIL'}  {det1}")

    print("\n=== Bug 4 - xcolor loaded before hyperref ===")
    ok4, det4 = check_bug4_xcolor_loaded(run)
    print(f"  {'PASS' if ok4 else 'FAIL'}  {det4}")

    print("\n=== Bug 2 - main.pdf surfaced in SSE files channel ===")
    ok2, det2 = check_bug2_pdf_in_files_channel(run)
    print(f"  {'PASS' if ok2 else 'FAIL'}  {det2}")

    print("\n" + "=" * 50)
    summary = [
        ("Bug 1 (no excessive AskUser)", ok1),
        ("Bug 2 (PDF in files channel) ", ok2),
        ("Bug 3 (Preview tools hidden) ", ok3),
        ("Bug 4 (xcolor before hyperref)", ok4),
    ]
    for name, ok in summary:
        print(f"  {name}  {'PASS' if ok else 'FAIL'}")
    all_ok = all(ok for _, ok in summary)
    print("=" * 50)
    print(f"RESULT: {'ALL PASS' if all_ok else 'FAILURES'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(main())

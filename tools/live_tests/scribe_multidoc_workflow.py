"""Multi-turn workflow test: create -> change -> replace -> rename.

Stresses the prompt rules around WsDelete proactivity (the loosened
"option B" decision matrix) and the live SSE channel hygiene for
artefacts produced by tectonic.

Run AFTER restarting the daemon. The 4 turns share a session, ~4-6
minutes total LLM time on gpt-5-mini.
"""
from __future__ import annotations

import json
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


def _send(client: DevClient, session: SessionHandle, msg: str, label: str,
          timeout: float = 240) -> dict:
    print(f"\n--- TURN [{label}] ---")
    print(f"prompt: {msg}")
    t0 = time.monotonic()
    stream = client.send_live(session, msg, total_timeout=timeout)
    elapsed = time.monotonic() - t0
    events = assertions.sort_by_seq(stream.events())

    # Track tool calls per turn
    tool_calls: list[str] = []
    file_resource_events: list[tuple[str, str, str]] = []  # (channel, id, type)
    for ev in events:
        t = str(ev.get("type", ""))
        p = ev.get("payload") or {}
        if t in ("tool_call", "tool_call_streaming"):
            name = p.get("name") or p.get("tool_name") or ""
            if name:
                tool_calls.append(str(name))
        if "resource_set" in t or "resource_patched" in t or "resource_deleted" in t:
            ch = str(p.get("channel") or "")
            rid = str(p.get("id") or p.get("resource_id") or "")
            file_resource_events.append((ch, rid, t))

    stream.stop(timeout=2.0)
    print(f"  elapsed={elapsed:.1f}s  events={len(events)}  tool_calls={len(tool_calls)}")
    print(f"  tools fired: {tool_calls[:20]}")
    return {
        "label": label,
        "elapsed": elapsed,
        "events": events,
        "tool_calls": tool_calls,
        "file_resource_events": file_resource_events,
    }


def _snapshot_disk(ws: Path) -> dict:
    """List every relevant file with sha256 prefix + size."""
    import hashlib
    out: dict[str, dict] = {}
    if not ws.exists():
        return out
    for p in sorted(ws.iterdir()):
        if p.is_file():
            try:
                data = p.read_bytes()
                h = hashlib.sha256(data).hexdigest()[:12]
            except OSError:
                h = "?"
                data = b""
            out[p.name] = {"size": len(data), "sha12": h}
    return out


def main() -> int:
    client = DevClient(token=_token())
    sid = f"multi-{uuid.uuid4().hex[:8]}"
    ws = Path.home() / ".digitorn" / "workspaces" / APP_ID / sid
    ws.mkdir(parents=True, exist_ok=True)
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace=str(ws),
    )
    print(f"daemon={client.daemon_url}  session={sid}  ws={ws}")

    # ────────────────────────────────────────────────────────────
    # Turn 1: create main.tex on subject A
    # ────────────────────────────────────────────────────────────
    t1 = _send(client, session,
        "Écris un court article LaTeX (1 page) sur le théorème central "
        "limite. Auteur : Paul. Mode article.",
        label="t1_create_clt", timeout=420)
    snap1 = _snapshot_disk(ws)
    print(f"\nafter t1 disk: {list(snap1.keys())}")

    # Hard abort if T1 failed - cascading on the same session locks up.
    if "main.tex" not in snap1:
        print("\n*** T1 produced no main.tex - aborting to avoid cascade ***")
        return 1

    # ────────────────────────────────────────────────────────────
    # Turn 2: change subject in place (should NOT delete main.tex)
    # ────────────────────────────────────────────────────────────
    t2 = _send(client, session,
        "Change le sujet : remplace par la loi des grands nombres. "
        "Garde la même structure, juste réécris le contenu.",
        label="t2_change_subject", timeout=300)
    snap2 = _snapshot_disk(ws)
    print(f"\nafter t2 disk: {list(snap2.keys())}")

    main_tex_changed = (
        "main.tex" in snap1 and "main.tex" in snap2
        and snap1["main.tex"]["sha12"] != snap2["main.tex"]["sha12"]
    )
    no_delete_t2 = "WsDelete" not in t2["tool_calls"]

    # ────────────────────────────────────────────────────────────
    # Turn 3: additive - create cv.tex alongside (should NOT delete main.tex)
    # ────────────────────────────────────────────────────────────
    t3 = _send(client, session,
        "Ajoute aussi un fichier cv.tex avec un CV de candidat data "
        "scientist (3 sections : profil, compétences, expériences). "
        "Garde main.tex intact.",
        label="t3_add_cv", timeout=240)
    snap3 = _snapshot_disk(ws)
    print(f"\nafter t3 disk: {list(snap3.keys())}")

    cv_created = "cv.tex" in snap3
    main_kept = "main.tex" in snap3
    no_delete_t3 = "WsDelete" not in t3["tool_calls"]

    # ────────────────────────────────────────────────────────────
    # Turn 4: replace - delete main.tex, keep cv.tex (option B: proactive)
    # ────────────────────────────────────────────────────────────
    t4 = _send(client, session,
        "Finalement, oublie le document de stats. Supprime main.tex, on "
        "reste sur le CV uniquement.",
        label="t4_delete_main", timeout=180)
    snap4 = _snapshot_disk(ws)
    print(f"\nafter t4 disk: {list(snap4.keys())}")

    main_deleted = "main.tex" not in snap4
    cv_still_there = "cv.tex" in snap4
    delete_called = "WsDelete" in t4["tool_calls"]

    # ────────────────────────────────────────────────────────────
    # Aux artefacts hygiene: after each compile, main.aux/log/toc should
    # never persist as orphans if the source disappeared.
    # ────────────────────────────────────────────────────────────
    aux_orphans = [
        n for n in snap4
        if n.startswith("main.") and n != "main.tex" and not n.endswith(".pdf")
    ]

    # ────────────────────────────────────────────────────────────
    # Report
    # ────────────────────────────────────────────────────────────
    checks = [
        ("T1: main.tex created",         "main.tex" in snap1),
        ("T1: main.pdf compiled",        "main.pdf" in snap1 and snap1["main.pdf"]["size"] > 1000),
        ("T2: main.tex content changed", main_tex_changed),
        ("T2: no WsDelete (in-place)",   no_delete_t2),
        ("T3: cv.tex created",           cv_created),
        ("T3: cv.pdf compiled",          "cv.pdf" in snap3 and snap3.get("cv.pdf",{}).get("size",0) > 1000),
        ("T3: main.tex preserved",       main_kept),
        ("T3: no WsDelete (additive)",   no_delete_t3),
        ("T4: WsDelete fired",           delete_called),
        ("T4: main.tex gone",            main_deleted),
        ("T4: cv.tex still present",     cv_still_there),
        ("T4: no stale main.* orphans",  len(aux_orphans) == 0),
    ]

    print("\n" + "=" * 60)
    all_ok = True
    for name, ok in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"  {mark}  {name}")
    print("=" * 60)
    if aux_orphans:
        print(f"  aux orphans after T4: {aux_orphans}")
    print(f"RESULT: {'ALL PASS' if all_ok else 'FAILURES'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(main())

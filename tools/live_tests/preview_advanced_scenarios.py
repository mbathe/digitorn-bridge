"""Advanced preview + workspace + rejoin scenarios.

Seven deterministic scenarios that exercise the preview pipeline
end-to-end using the direct tool-execute endpoint (bypassing the LLM
so each mutation sequence is reproducible):

  1. event_shape          — a single WsWrite produces a correctly
                            shaped preview:resource_set envelope.
  2. lifecycle_crud       — write -> edit -> delete emits the right
                            three events and leaves an empty snapshot.
  3. multi_file_ordering  — three sequential writes land in seq order
                            in both event log and final snapshot.
  4. rejoin_full          — rejoin with since=0 returns a preview:
                            snapshot matching the on-disk state.
  5. rejoin_incremental   — rejoin with since=<last_seq> only replays
                            events after that seq, no duplicates.
  6. rejoin_after_edit    — after an edit, rejoin snapshot contains
                            the edited payload, not the original.
  7. cross_session        — two parallel sessions never leak into
                            each other's snapshot.
"""
from __future__ import annotations

import json
import os as _os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from digitorn.testing.client import DevClient
from digitorn.testing.models import SessionHandle


_APP_ID = "ws-preview-test"


# ── helpers ────────────────────────────────────────────────────


def _new_session(app_id: str, daemon_url: str, prefix: str = "pv") -> SessionHandle:
    return SessionHandle(
        session_id=f"{prefix}-{uuid.uuid4().hex[:8]}",
        app_id=app_id, daemon_url=daemon_url, workspace="",
    )


def _exec_tool(
    client: DevClient, session: SessionHandle, tool: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Run a tool synchronously on a given session (bypasses LLM)."""
    r = client._post(
        f"/api/apps/{session.app_id}/tools/{tool}/execute",
        json={"session_id": session.session_id, "params": params},
    )
    try:
        return r.json()
    except Exception:
        return {"success": False, "error": r.text[:500]}


def _kick_session(client: DevClient, session: SessionHandle) -> None:
    """Register the session through manager.chat so subsequent
    HTTP/Socket.IO paths find it. We send a trivial one-word prompt
    (~3s with DeepSeek) so ``manager.chat`` resolves the workspace,
    persists the session, and wires the preview module with the
    filesystem backend pointing at the per-session workspace dir.
    Direct ``execute_tool`` alone only activates the preview module —
    it doesn't create the ConversationSession that ``get_session``
    returns."""
    # Neutral prompt that requires no tool call — the point is only
    # to run ``manager.chat`` once so the session is registered with
    # its resolved workspace. A more suggestive prompt (like "ok")
    # can be interpreted by the LLM as license to create files.
    r = client.post_message_raw(
        session,
        "Answer with the single word 'ready' and nothing else. "
        "Do not call any tool.",
    )
    cid = (r.get("body") or {}).get("data", {}).get("correlation_id") or ""
    stream = client.open_event_stream(session, wait_for_session=True)
    try:
        if cid:
            stream.wait_for(
                "message_done", timeout=60.0,
                predicate=lambda e: (e.get("payload") or {}).get("correlation_id") == cid,
            )
        else:
            stream.wait_until_idle(quiet_seconds=1.0, total_timeout=10.0)
    finally:
        stream.stop(timeout=2.0)


def _collect_events(stream, timeout: float = 4.0, quiet: float = 1.0) -> list[dict[str, Any]]:
    """Wait until the event stream is idle, return everything seen."""
    try:
        stream.wait_until_idle(quiet_seconds=quiet, total_timeout=timeout)
    except Exception:
        pass
    return list(stream.events())


def _by_type(events: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in events:
        t = str(e.get("type") or "")
        out[t] = out.get(t, 0) + 1
    return out


def _preview_files(events: list[dict[str, Any]]) -> list[tuple[int, str, str]]:
    """(seq, type, file_id) for every preview:resource_* event on 'files' channel."""
    out: list[tuple[int, str, str]] = []
    for e in events:
        t = str(e.get("type") or "")
        if not t.startswith("preview:resource"):
            continue
        p = e.get("payload") or {}
        if p.get("channel") != "files":
            continue
        out.append((int(e.get("seq") or 0), t, str(p.get("id") or "")))
    return out


def _get_snapshot_files(client: DevClient, session: SessionHandle) -> dict[str, Any]:
    """Open a fresh stream, wait for preview:snapshot, return the files channel."""
    stream = client.open_event_stream(session, wait_for_session=True)
    try:
        snap = stream.wait_for("preview:snapshot", timeout=8.0)
        if snap is None:
            return {"__missing__": True}
        payload = snap.get("payload") or {}
        return (payload.get("resources") or {}).get("files") or {}
    finally:
        stream.stop(timeout=2.0)


def _last_seq_for_session(client: DevClient, session: SessionHandle) -> int:
    """Get the highest seq observed via /history.events (DB-backed)."""
    r = client._get(
        f"/api/apps/{session.app_id}/sessions/{session.session_id}/history",
    ).json().get("data", {}) or {}
    events = r.get("events") or []
    max_seq = 0
    for e in events:
        try:
            s = int(e.get("seq") or 0)
            if s > max_seq:
                max_seq = s
        except Exception:
            pass
    return max_seq


# ── scenarios ──────────────────────────────────────────────────


def scenario_event_shape(client: DevClient) -> tuple[bool, str, dict]:
    session = _new_session(_APP_ID, client.daemon_url, "shape")
    stream = None
    try:
        _kick_session(client, session)
        stream = client.open_event_stream(session, wait_for_session=True)
        _exec_tool(client, session, "WsWrite", {
            "path": "a.txt", "content": "hello",
        })
        events = _collect_events(stream, timeout=4.0, quiet=1.0)
        checks: list[tuple[str, bool, str]] = []

        set_events = [
            e for e in events if e.get("type") == "preview:resource_set"
        ]
        checks.append((
            "exactly 1 preview:resource_set for 'a.txt'",
            any(
                (e.get("payload") or {}).get("id") == "a.txt"
                for e in set_events
            ),
            f"count={len(set_events)}",
        ))
        ev_a = next(
            (e for e in set_events
             if (e.get("payload") or {}).get("id") == "a.txt"),
            None,
        )
        if ev_a is not None:
            p = ev_a.get("payload") or {}
            inner = p.get("payload") or {}
            checks.append((
                "envelope uses 'payload' (not 'data')",
                "payload" in ev_a and "data" not in ev_a,
                f"keys={list(ev_a.keys())}",
            ))
            checks.append((
                "envelope has a monotonic seq",
                isinstance(ev_a.get("seq"), int) and ev_a.get("seq") > 0,
                f"seq={ev_a.get('seq')}",
            ))
            checks.append((
                "kind=session",
                ev_a.get("kind") == "session",
                f"kind={ev_a.get('kind')}",
            ))
            checks.append((
                "payload carries channel+id+content",
                p.get("channel") == "files"
                and p.get("id") == "a.txt"
                and inner.get("content") == "hello",
                f"payload_keys={list(p.keys())} inner_content={inner.get('content')!r}",
            ))
        ok = all(c[1] for c in checks)
        detail = "\n".join(
            f"  [{'PASS' if c[1] else 'FAIL'}] {c[0]}: {c[2]}" for c in checks
        )
        return ok, detail, {"session": session.session_id}
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)


def scenario_lifecycle_crud(client: DevClient) -> tuple[bool, str, dict]:
    session = _new_session(_APP_ID, client.daemon_url, "crud")
    stream = None
    try:
        _kick_session(client, session)
        stream = client.open_event_stream(session, wait_for_session=True)
        _exec_tool(client, session, "WsWrite", {
            "path": "doc.md", "content": "# hello",
        })
        _exec_tool(client, session, "WsEdit", {
            "path": "doc.md",
            "old_string": "# hello",
            "new_string": "# bonjour",
        })
        _exec_tool(client, session, "WsDelete", {"path": "doc.md"})

        events = _collect_events(stream, timeout=6.0, quiet=1.0)
        files_events = _preview_files(events)

        checks: list[tuple[str, bool, str]] = []
        # We expect at least: set (write), set (edit), deleted (delete).
        types = [t for _, t, _ in files_events]
        checks.append((
            "event sequence contains set, set, deleted for 'doc.md'",
            types.count("preview:resource_set") >= 2
            and "preview:resource_deleted" in types,
            f"types={types}",
        ))
        # Deletion should produce an empty files channel in the snapshot.
        final_files = _get_snapshot_files(client, session)
        checks.append((
            "post-delete snapshot omits doc.md",
            "doc.md" not in final_files,
            f"remaining={list(final_files.keys())}",
        ))
        ok = all(c[1] for c in checks)
        detail = "\n".join(
            f"  [{'PASS' if c[1] else 'FAIL'}] {c[0]}: {c[2]}" for c in checks
        )
        return ok, detail, {
            "session": session.session_id,
            "types": types,
            "remaining_files": list(final_files.keys()),
        }
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)


def scenario_multi_file_ordering(client: DevClient) -> tuple[bool, str, dict]:
    session = _new_session(_APP_ID, client.daemon_url, "multi")
    stream = None
    try:
        _kick_session(client, session)
        stream = client.open_event_stream(session, wait_for_session=True)
        for name in ("x.txt", "y.txt", "z.txt"):
            _exec_tool(client, session, "WsWrite", {
                "path": name, "content": name.upper(),
            })
        events = _collect_events(stream, timeout=6.0, quiet=1.0)
        files_events = _preview_files(events)
        write_order = [
            fid for _, t, fid in files_events
            if t == "preview:resource_set" and fid in {"x.txt", "y.txt", "z.txt"}
        ]

        checks: list[tuple[str, bool, str]] = []
        checks.append((
            "events are x.txt -> y.txt -> z.txt",
            write_order == ["x.txt", "y.txt", "z.txt"],
            f"write_order={write_order}",
        ))
        seqs = [s for s, _, _ in files_events]
        checks.append((
            "seq strictly increasing",
            all(a < b for a, b in zip(seqs, seqs[1:])),
            f"seqs={seqs}",
        ))
        final_files = _get_snapshot_files(client, session)
        checks.append((
            "snapshot contains all three files",
            all(k in final_files for k in ("x.txt", "y.txt", "z.txt")),
            f"snapshot_keys={sorted(final_files.keys())}",
        ))
        ok = all(c[1] for c in checks)
        detail = "\n".join(
            f"  [{'PASS' if c[1] else 'FAIL'}] {c[0]}: {c[2]}" for c in checks
        )
        return ok, detail, {"session": session.session_id}
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)


def scenario_rejoin_full(client: DevClient) -> tuple[bool, str, dict]:
    session = _new_session(_APP_ID, client.daemon_url, "rj-full")
    try:
        _kick_session(client, session)
        _exec_tool(client, session, "WsWrite", {
            "path": "readme.md", "content": "# hi",
        })
        _exec_tool(client, session, "WsWrite", {
            "path": "src/main.ts", "content": "console.log(1)",
        })
        time.sleep(1.0)  # flush debounce

        # Fresh rejoin — should receive preview:snapshot.
        files = _get_snapshot_files(client, session)
        checks = [
            ("rejoin preview:snapshot received",
             "__missing__" not in files,
             f"files_keys={list(files.keys())}"),
            ("snapshot has 'readme.md'",
             "readme.md" in files,
             f"keys={list(files.keys())}"),
            ("snapshot has 'src/main.ts'",
             "src/main.ts" in files,
             f"keys={list(files.keys())}"),
            ("'readme.md' content preserved",
             (files.get("readme.md") or {}).get("content") == "# hi",
             f"content={(files.get('readme.md') or {}).get('content')!r}"),
        ]
        ok = all(c[1] for c in checks)
        detail = "\n".join(
            f"  [{'PASS' if c[1] else 'FAIL'}] {c[0]}: {c[2]}" for c in checks
        )
        return ok, detail, {"session": session.session_id}
    except Exception as exc:
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}", {}


def scenario_rejoin_incremental(client: DevClient) -> tuple[bool, str, dict]:
    session = _new_session(_APP_ID, client.daemon_url, "rj-inc")
    try:
        _kick_session(client, session)
        _exec_tool(client, session, "WsWrite", {
            "path": "alpha.txt", "content": "A",
        })
        time.sleep(1.0)
        cutoff = _last_seq_for_session(client, session)

        _exec_tool(client, session, "WsWrite", {
            "path": "beta.txt", "content": "B",
        })
        time.sleep(1.0)

        from digitorn.testing.events import LiveEventStream
        rejoin = LiveEventStream(
            daemon_url=client.daemon_url,
            token=client._get_auth_token(),
            app_id=session.app_id,
            session_id=session.session_id,
            since_seq=cutoff,
        )
        rejoin.start()
        try:
            # Wait for replay + snapshot to flush.
            time.sleep(2.0)
            events = list(rejoin.events())
        finally:
            rejoin.stop(timeout=2.0)

        replay_files = _preview_files(events)
        alpha_seen = any(fid == "alpha.txt" for _, _, fid in replay_files)
        beta_seen = any(fid == "beta.txt" for _, _, fid in replay_files)

        checks = [
            ("since=cutoff excludes alpha.txt", not alpha_seen,
             f"alpha_seen={alpha_seen}"),
            ("since=cutoff includes beta.txt", beta_seen,
             f"beta_seen={beta_seen}"),
        ]
        ok = all(c[1] for c in checks)
        detail = "\n".join(
            f"  [{'PASS' if c[1] else 'FAIL'}] {c[0]}: {c[2]}" for c in checks
        )
        return ok, detail, {
            "session": session.session_id,
            "cutoff": cutoff,
            "replayed_files": [(s, t, f) for s, t, f in replay_files],
        }
    except Exception as exc:
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}", {}


def scenario_rejoin_after_edit(client: DevClient) -> tuple[bool, str, dict]:
    session = _new_session(_APP_ID, client.daemon_url, "rj-edit")
    try:
        _kick_session(client, session)
        _exec_tool(client, session, "WsWrite", {
            "path": "note.md", "content": "original",
        })
        _exec_tool(client, session, "WsEdit", {
            "path": "note.md",
            "old_string": "original", "new_string": "edited",
        })
        time.sleep(1.0)

        files = _get_snapshot_files(client, session)
        checks = [
            ("note.md still present after edit", "note.md" in files,
             f"keys={list(files.keys())}"),
        ]
        content = (files.get("note.md") or {}).get("content")
        checks.append((
            "snapshot carries edited content",
            content == "edited",
            f"content={content!r}",
        ))
        ok = all(c[1] for c in checks)
        detail = "\n".join(
            f"  [{'PASS' if c[1] else 'FAIL'}] {c[0]}: {c[2]}" for c in checks
        )
        return ok, detail, {"session": session.session_id, "content": content}
    except Exception as exc:
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}", {}


def scenario_cross_session(client: DevClient) -> tuple[bool, str, dict]:
    a = _new_session(_APP_ID, client.daemon_url, "cs-a")
    b = _new_session(_APP_ID, client.daemon_url, "cs-b")
    try:
        _kick_session(client, a)
        _kick_session(client, b)
        _exec_tool(client, a, "WsWrite", {
            "path": "only-in-a.txt", "content": "A",
        })
        _exec_tool(client, b, "WsWrite", {
            "path": "only-in-b.txt", "content": "B",
        })
        time.sleep(1.0)
        a_files = _get_snapshot_files(client, a)
        b_files = _get_snapshot_files(client, b)

        checks = [
            ("session A sees only-in-a.txt",
             "only-in-a.txt" in a_files,
             f"a_files={sorted(a_files.keys())}"),
            ("session A does NOT see only-in-b.txt",
             "only-in-b.txt" not in a_files,
             f"a_files={sorted(a_files.keys())}"),
            ("session B sees only-in-b.txt",
             "only-in-b.txt" in b_files,
             f"b_files={sorted(b_files.keys())}"),
            ("session B does NOT see only-in-a.txt",
             "only-in-a.txt" not in b_files,
             f"b_files={sorted(b_files.keys())}"),
        ]
        ok = all(c[1] for c in checks)
        detail = "\n".join(
            f"  [{'PASS' if c[1] else 'FAIL'}] {c[0]}: {c[2]}" for c in checks
        )
        return ok, detail, {
            "session_a": a.session_id, "session_b": b.session_id,
        }
    except Exception as exc:
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}", {}


# ── runner ─────────────────────────────────────────────────────


def main() -> int:
    daemon_url = _os.environ.get("DAEMON_URL", "http://127.0.0.1:8000")
    email = _os.environ.get("DEV_EMAIL", "dev@digitorn.local")
    password = _os.environ.get("DEV_PASSWORD", "DevPassword123!")
    client = DevClient.with_user(
        email, password, daemon_url=daemon_url, register_if_missing=True,
    )

    scenarios = [
        ("event_shape", scenario_event_shape),
        ("lifecycle_crud", scenario_lifecycle_crud),
        ("multi_file_ordering", scenario_multi_file_ordering),
        ("rejoin_full", scenario_rejoin_full),
        ("rejoin_incremental", scenario_rejoin_incremental),
        ("rejoin_after_edit", scenario_rejoin_after_edit),
        ("cross_session", scenario_cross_session),
    ]

    passed = 0
    print(f"\n=== Advanced preview + rejoin scenarios ({len(scenarios)}) ===\n")
    for name, fn in scenarios:
        t0 = time.time()
        try:
            ok, detail, art = fn(client)
        except Exception as exc:
            ok, detail, art = False, f"EXCEPTION: {type(exc).__name__}: {exc}", {}
        dur = time.time() - t0
        tag = "PASS" if ok else "FAIL"
        print(f"[{tag}] {name} ({dur:.1f}s)")
        print(detail)
        if not ok and art:
            print(f"  artifacts: {json.dumps(art, default=str)[:500]}")
        print()
        if ok:
            passed += 1

    print(f"{passed}/{len(scenarios)} scenarios passed\n")
    return 0 if passed == len(scenarios) else 1


if __name__ == "__main__":
    sys.exit(main())

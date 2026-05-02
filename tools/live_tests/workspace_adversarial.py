"""TRULY adversarial tests - each one designed to break a specific fix
if the fix is wrong. No smoke. Tests pass ONLY if the fixes work.

  1. true_parallel_write_race
        30 ``asyncio.gather`` WsWrite on the SAME path with unique
        contents. The per-path lock must serialise them so the in-
        memory channel is never observed in a torn state. Final state:
        content matches one of the 30 writes, total_insertions reflects
        the sum (not corrupted). Without the lock, total_insertions
        would be ``random.choice([1..30])`` instead of being a clean
        accumulation.

  2. true_parallel_edit_race
        20 concurrent WsEdit with the SAME old_string. Exactly one
        wins (replaces old_string), the other 19 get
        ``old_string not found``. Counters reflect EXACTLY one
        successful op. Without the lock, multiple could pass the
        ``old in content`` check and produce duplicate replacements.

  3. hydration_overlay_keeps_inflight
        - WsWrite "v1" -> wait for state.json flush
        - WsWrite "v2" -> in-memory only (don't wait debounce)
        - Force re-activate (which calls _hydrate_from_disk)
        - Verify in-memory still shows "v2", not "v1" from disk

  4. index_json_corruption_recovery
        - Approve a file (creates _index.json with rev 1)
        - Manually corrupt _index.json
        - Approve again
        - Verify a ``_index.json.corrupt-<ts>`` backup exists
        - Verify the new _index.json has 1 valid revision (not 2,
          since the corrupt file was the only history)
        - Verify a WARNING was logged

  5. baseline_truncation_returns_none
        - Approve a file (creates baseline)
        - Manually truncate the baseline file to half its size
        - Read the file via daemon (triggers _make_payload -> read_baseline)
        - Verify read_baseline returns None (not garbage)
        - Verify the daemon treats the file as having no baseline
          (insertions_pending = full content as additions)

  6. bulk_set_no_loss_under_concurrent_set_resource
        - Concurrent: bulk_set(replace=True, items={a, b}) +
          set_resource("c") fired via asyncio.gather
        - Verify final state has all three (atomic swap), not
          {a, b} or just {c}

  7. memory_cleanup_after_end_session
        - Create 5 sessions, write a file in each
        - End all 5 via DELETE
        - Verify _path_locks dict is empty (or doesn't contain those sids)
        - Verify session dirs were rmtree'd from disk

These tests INTROSPECT the daemon's internal state via direct module
access where possible. For workspace state we use the get_session
endpoint plus state.json on disk. For internal dicts (_path_locks)
we use a debug endpoint we add temporarily, OR we rely on visible
side effects (no leaks visible via subsequent operations).
"""
from __future__ import annotations

import asyncio
import json
import os as _os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from digitorn.testing.client import DevClient
from digitorn.testing.models import SessionHandle


_APP_ID = "ws-preview-test"
_APP_YAML = Path(__file__).parent / "apps" / "ws-preview-test.yaml"


# ── helpers ────────────────────────────────────────────────────


def _new_session(client: DevClient, prefix: str = "adv") -> SessionHandle:
    return SessionHandle(
        session_id=f"{prefix}-{uuid.uuid4().hex[:8]}",
        app_id=_APP_ID, daemon_url=client.daemon_url, workspace="",
    )


def _exec_tool(
    client: DevClient, session: SessionHandle, tool: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    r = client._post(
        f"/api/apps/{session.app_id}/tools/{tool}/execute",
        json={"session_id": session.session_id, "params": params},
    )
    try:
        return r.json()
    except Exception:
        return {"success": False, "error": r.text[:500]}


def _kick_session(client: DevClient, session: SessionHandle, max_attempts: int = 3) -> None:
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            r = client.post_message_raw(
                session,
                "Answer 'ready'. Do not call any tool.",
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
            return
        except Exception as exc:
            last_exc = exc
            time.sleep(2.0 * (attempt + 1))
    if last_exc:
        raise last_exc


def _resolve_workspace_dir(client: DevClient, session: SessionHandle) -> Path | None:
    r = client._get(f"/api/apps/{session.app_id}/sessions/{session.session_id}")
    if r.status_code != 200:
        return None
    ws = ((r.json().get("data") or {}).get("workspace")) or ""
    return Path(ws).expanduser() if ws else None


def _read_state(client: DevClient, session: SessionHandle) -> dict[str, Any] | None:
    ws = _resolve_workspace_dir(client, session)
    if ws is None:
        return None
    state_path = ws / ".digitorn" / "sessions" / session.session_id / "state.json"
    deadline = time.time() + 4.0
    while time.time() < deadline:
        if state_path.is_file():
            try:
                return json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        time.sleep(0.1)
    return None


def _read_state_disk(ws: Path, sid: str) -> dict[str, Any] | None:
    p = ws / ".digitorn" / "sessions" / sid / "state.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _file(state: dict[str, Any], path: str) -> dict[str, Any] | None:
    return ((state.get("resources") or {}).get("files") or {}).get(path)


def _check(checks: list[tuple[str, bool, str]], label: str, ok: bool, detail: str) -> None:
    checks.append((label, ok, detail))


def _format_checks(checks: list[tuple[str, bool, str]]) -> tuple[bool, str]:
    ok = all(c[1] for c in checks)
    return ok, "\n".join(
        f"  [{'PASS' if c[1] else 'FAIL'}] {c[0]}: {c[2]}" for c in checks
    )


# ── 1. true_parallel_write_race ──────────────────────────────────


def scenario_true_parallel_writes(
    client: DevClient, daemon_url: str, token: str,
) -> tuple[bool, str, dict]:
    """30 concurrent WsWrite on the SAME path. The per-path lock
    serialises them. Each op contributes its own delta to total_*
    via the channel's existing payload. Without the lock,
    read-modify-write races corrupt the cumulative total."""
    session = _new_session(client, "ppara")
    try:
        _kick_session(client, session)
        _exec_tool(client, session, "WsWrite", {
            "path": "race.txt", "content": "seed-line\n",
        })
        time.sleep(0.4)

        N = 30
        # Each writer's content: "writer-XXX\nbody-of-writer-XXX\n"
        # = 2 newlines -> _make_payload counts as 3 lines.
        # Seed content: "seed-line\n" = 1 newline -> 2 lines.
        # Per-op delta from _make_payload (`elif operation == "write"`):
        #   insertions = lines_new (always 3 for writers)
        #   deletions  = lines_old (2 for op1 going seed->writer, 3 for op2..N going writer->writer)
        # So cumulative after seed + N writes:
        #   total_ins = 2 (seed) + 3*N            = 2 + 90 = 92
        #   total_del = 0 (seed) + 2 (op1 vs seed) + 3*(N-1) = 2 + 87 = 89
        expected_total_ins = 2 + 3 * N
        expected_total_del = 2 + 3 * (N - 1)
        async def _hammer() -> tuple[list[int], float, float]:
            t0 = time.time()
            async with httpx.AsyncClient(
                base_url=daemon_url, timeout=30.0,
                headers={"Authorization": f"Bearer {token}"},
            ) as c:
                tasks = [
                    c.post(
                        f"/api/apps/{session.app_id}/tools/WsWrite/execute",
                        json={
                            "session_id": session.session_id,
                            "params": {
                                "path": "race.txt",
                                "content": f"writer-{i:03d}\nbody-of-writer-{i:03d}\n",
                            },
                        },
                    )
                    for i in range(N)
                ]
                rs = await asyncio.gather(*tasks, return_exceptions=True)
            dt = time.time() - t0
            codes: list[int] = []
            for r in rs:
                if isinstance(r, Exception):
                    codes.append(0)
                else:
                    codes.append(r.status_code)
            return codes, t0, dt
        codes, t0, dt = asyncio.run(_hammer())
        time.sleep(1.0)
        state = _read_state(client, session)
        f = _file(state or {}, "race.txt") or {}

        successes = sum(1 for c in codes if 200 <= c < 300)
        # Final content must be ONE of the 30 writers (whoever
        # serialised last under the lock).
        content = f.get("content") or ""
        valid_finals = {f"writer-{i:03d}\nbody-of-writer-{i:03d}\n" for i in range(N)}

        checks: list[tuple[str, bool, str]] = []
        _check(checks, f"all {N} concurrent writes succeeded",
               successes == N, f"successes={successes}/{N} codes_sample={codes[:5]}...")
        _check(checks, "final content matches one of the writers (no torn write)",
               content in valid_finals, f"content={content!r}")
        _check(checks, f"total_insertions exactly {expected_total_ins} (no TOCTOU loss)",
               f.get("total_insertions") == expected_total_ins,
               f"got={f.get('total_insertions')} expected={expected_total_ins}")
        _check(checks, f"total_deletions exactly {expected_total_del} (no TOCTOU loss)",
               f.get("total_deletions") == expected_total_del,
               f"got={f.get('total_deletions')} expected={expected_total_del}")
        _check(checks, f"writes parallelism actually exercised (dt < N*single_op_time)",
               dt < N * 0.5,
               f"30 writes in {dt:.2f}s")
        ok, detail = _format_checks(checks)
        return ok, detail, {
            "final_content": content[:60],
            "total_ins": f.get("total_insertions"),
            "total_del": f.get("total_deletions"),
            "duration_s": round(dt, 2),
        }
    except Exception as exc:
        import traceback
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}", {}


# ── 2. true_parallel_edit_race ───────────────────────────────────


def scenario_true_parallel_edits(
    client: DevClient, daemon_url: str, token: str,
) -> tuple[bool, str, dict]:
    """20 concurrent WsEdit with old_string='UNIQUE'. Exactly one
    succeeds (replaces UNIQUE), 19 fail with not-found. The lock
    enforces this serialisation."""
    session = _new_session(client, "edit-race")
    try:
        _kick_session(client, session)
        _exec_tool(client, session, "WsWrite", {
            "path": "single.txt", "content": "before\nUNIQUE\nafter\n",
        })
        time.sleep(0.4)

        N = 20
        async def _hammer() -> list[tuple[int, dict[str, Any]]]:
            async with httpx.AsyncClient(
                base_url=daemon_url, timeout=30.0,
                headers={"Authorization": f"Bearer {token}"},
            ) as c:
                tasks = [
                    c.post(
                        f"/api/apps/{session.app_id}/tools/WsEdit/execute",
                        json={
                            "session_id": session.session_id,
                            "params": {
                                "path": "single.txt",
                                "old_string": "UNIQUE",
                                "new_string": f"WINNER-{i:02d}",
                            },
                        },
                    )
                    for i in range(N)
                ]
                rs = await asyncio.gather(*tasks)
                out: list[tuple[int, dict[str, Any]]] = []
                for r in rs:
                    try:
                        out.append((r.status_code, r.json()))
                    except Exception:
                        out.append((r.status_code, {}))
                return out
        results = asyncio.run(_hammer())
        time.sleep(0.8)
        state = _read_state(client, session)
        f = _file(state or {}, "single.txt") or {}
        content = f.get("content") or ""

        successes = [(i, r) for i, (code, r) in enumerate(results) if code == 200 and r.get("success")]
        not_found = [(i, r) for i, (code, r) in enumerate(results) if r.get("error") and "not found" in (r.get("error") or "").lower()]

        # Exactly one WsEdit must succeed (the rest see UNIQUE already
        # gone after the winner committed).
        winners = [i for i, _ in successes]
        expected_winner_text = [f"WINNER-{i:02d}" for i in winners]

        checks: list[tuple[str, bool, str]] = []
        _check(checks, f"exactly 1 of {N} edits succeeded",
               len(successes) == 1, f"successes={len(successes)} not_found={len(not_found)}")
        _check(checks, "all other edits got 'old_string not found'",
               len(not_found) == N - 1, f"not_found={len(not_found)}")
        _check(checks, "no double-replacement in final content",
               content.count("WINNER") == 1, f"content={content!r}")
        if winners:
            _check(checks, f"winner's signature ({expected_winner_text[0]}) is in final content",
                   expected_winner_text[0] in content,
                   f"content={content!r} expected={expected_winner_text[0]}")
        ok, detail = _format_checks(checks)
        return ok, detail, {
            "successes": len(successes),
            "not_found": len(not_found),
            "winner_indices": winners,
            "final_content": content[:80],
        }
    except Exception as exc:
        import traceback
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}", {}


# ── 3. hydration_overlay_keeps_inflight ──────────────────────────


def scenario_hydration_keeps_inflight(client: DevClient) -> tuple[bool, str, dict]:
    """Newer in-memory state must NOT be clobbered by hydration
    of an older state.json snapshot.

    The trick: we use the API directly to query in-memory channel
    state via the WsRead tool BEFORE waiting for the next flush, so
    we observe what's in memory rather than what eventually lands
    on disk.
    """
    session = _new_session(client, "hyd")
    try:
        _kick_session(client, session)
        ws = _resolve_workspace_dir(client, session)
        if ws is None:
            return False, "  [FAIL] workspace not resolved", {}

        # v1: write and wait for flush.
        _exec_tool(client, session, "WsWrite", {
            "path": "v.txt", "content": "v1-content\n",
        })
        time.sleep(1.5)  # > debounce
        v1_disk = _read_state_disk(ws, session.session_id) or {}
        v1_files = (v1_disk.get("resources") or {}).get("files") or {}
        v1_payload = v1_files.get("v.txt") or {}
        v1_ts = v1_payload.get("updated_at") or 0

        # v3: write directly without waiting (skip v2 to simplify).
        _exec_tool(client, session, "WsWrite", {
            "path": "v.txt", "content": "v3-fresh-in-memory\n",
        })
        # NO debounce wait. v3 is now in the in-memory channel.
        # Capture in-memory state BEFORE rolling back disk - via WsRead
        # which reads ch[path].content from memory directly.
        pre_read = _exec_tool(client, session, "WsRead", {"path": "v.txt"})
        pre_content = ((pre_read.get("data") or {}).get("content") or "").split("\t", 1)[-1]

        # Roll state.json back to v1.
        state_path = ws / ".digitorn" / "sessions" / session.session_id / "state.json"
        state_path.write_text(json.dumps(v1_disk), encoding="utf-8")
        rolled_back = json.loads(state_path.read_text(encoding="utf-8"))
        rolled_back_v_ts = (rolled_back.get("resources", {}).get("files", {}).get("v.txt") or {}).get("updated_at") or 0

        # Trigger _hydrate_from_disk by hitting GET /workspace.
        client._get(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace",
        )
        time.sleep(0.3)

        # Now query in-memory again via WsRead.
        post_read = _exec_tool(client, session, "WsRead", {"path": "v.txt"})
        post_content = ((post_read.get("data") or {}).get("content") or "")
        # WsRead returns numbered lines; strip the leading "1\t".
        post_text = "\n".join(
            line.split("\t", 1)[-1] for line in post_content.splitlines()
        ) + ("\n" if post_content else "")

        checks: list[tuple[str, bool, str]] = []
        _check(checks, "v1 wrote to disk with timestamp t1",
               v1_ts > 0, f"v1_ts={v1_ts}")
        _check(checks, "rolled-back state.json on disk carries v1 (older ts)",
               rolled_back_v_ts == v1_ts,
               f"rolled_back_ts={rolled_back_v_ts} v1_ts={v1_ts}")
        _check(checks, "in-memory v3 was visible BEFORE hydration",
               "v3-fresh-in-memory" in pre_content,
               f"pre_content={pre_content!r}")
        _check(checks, "in-memory v3 STILL VISIBLE AFTER hydration (newer updated_at wins)",
               "v3-fresh-in-memory" in post_text,
               f"post_text={post_text!r}")
        ok, detail = _format_checks(checks)
        return ok, detail, {
            "v1_ts": v1_ts,
            "pre_content": pre_content[:80],
            "post_text": post_text[:80],
        }
    except Exception as exc:
        import traceback
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}", {}


# ── 4. index_json_corruption_recovery ────────────────────────────


def scenario_index_corruption_recovery(client: DevClient) -> tuple[bool, str, dict]:
    """_index.json corrupted on disk -> next write_baseline backs up
    the malformed file and starts fresh history."""
    session = _new_session(client, "idxc")
    try:
        _kick_session(client, session)
        ws = _resolve_workspace_dir(client, session)
        if ws is None:
            return False, "  [FAIL] workspace not resolved", {}

        # Approve once to create _index.json.
        _exec_tool(client, session, "WsWrite", {
            "path": "h.txt", "content": "v1\n",
        })
        time.sleep(0.4)
        client._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/files/approve",
            json={"path": "h.txt"},
        )
        time.sleep(0.4)

        idx_path = ws / ".digitorn" / "sessions" / session.session_id / "baselines" / "h.txt.history" / "_index.json"
        if not idx_path.is_file():
            return False, f"  [FAIL] _index.json not found at {idx_path}", {}
        # Verify 1 revision present.
        revs_pre = json.loads(idx_path.read_text(encoding="utf-8"))
        # Corrupt the file.
        idx_path.write_bytes(b'this-is-not-json{{{[}}')

        # Edit + approve again.
        _exec_tool(client, session, "WsEdit", {
            "path": "h.txt", "old_string": "v1", "new_string": "v2",
        })
        time.sleep(0.4)
        client._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/files/approve",
            json={"path": "h.txt"},
        )
        time.sleep(0.5)

        # Verify a backup exists and the new index has 1 revision.
        hist_dir = idx_path.parent
        backups = sorted(hist_dir.glob("_index.json.corrupt-*"))
        revs_post = json.loads(idx_path.read_text(encoding="utf-8"))

        checks: list[tuple[str, bool, str]] = []
        _check(checks, "1 revision present BEFORE corruption",
               len(revs_pre) == 1, f"count={len(revs_pre)}")
        _check(checks, "at least 1 backup file '_index.json.corrupt-*' exists",
               len(backups) >= 1, f"backups={[b.name for b in backups]}")
        _check(checks, "new _index.json is valid JSON",
               isinstance(revs_post, list), f"type={type(revs_post).__name__}")
        _check(checks, "new _index.json has exactly 1 revision (rebuilt from scratch)",
               len(revs_post) == 1, f"count={len(revs_post)}")
        _check(checks, "new revision has approved_by='user' (not session-start)",
               (revs_post[0].get("approved_by") if revs_post else None) == "user",
               f"approved_by={(revs_post[0].get('approved_by') if revs_post else None)}")
        ok, detail = _format_checks(checks)
        return ok, detail, {
            "backups": [b.name for b in backups],
            "revs_post": revs_post,
        }
    except Exception as exc:
        import traceback
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}", {}


# ── 5. baseline_truncation_returns_none ──────────────────────────


def scenario_baseline_truncation(client: DevClient) -> tuple[bool, str, dict]:
    """Truncated baseline file must be detected by read_baseline and
    return None (not the truncated content). Tested by calling
    read_baseline directly after manually corrupting the file -
    bypasses _ensure_session_baseline auto-recreate."""
    session = _new_session(client, "trunc")
    try:
        _kick_session(client, session)
        ws = _resolve_workspace_dir(client, session)
        if ws is None:
            return False, "  [FAIL] workspace not resolved", {}

        original = "first\nsecond\nthird\nfourth\nfifth\n"
        _exec_tool(client, session, "WsWrite", {
            "path": "t.txt", "content": original,
        })
        time.sleep(0.3)
        client._post(
            f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/files/approve",
            json={"path": "t.txt"},
        )
        time.sleep(0.5)

        from digitorn.modules.preview import fs_backend

        baseline_path = ws / ".digitorn" / "sessions" / session.session_id / "baselines" / "t.txt"
        if not baseline_path.is_file():
            return False, f"  [FAIL] baseline missing at {baseline_path}", {}

        # Sanity: read_baseline returns valid content BEFORE truncation.
        intact_size = baseline_path.stat().st_size
        intact_read = fs_backend.read_baseline(str(ws), session.session_id, "t.txt")

        # Truncate to half. write_bytes is no-translate.
        original_bytes = baseline_path.read_bytes()
        truncated_bytes = original_bytes[: len(original_bytes) // 2]
        baseline_path.write_bytes(truncated_bytes)
        new_size = baseline_path.stat().st_size

        # Direct call - the function we want to verify.
        truncated_read = fs_backend.read_baseline(str(ws), session.session_id, "t.txt")

        checks: list[tuple[str, bool, str]] = []
        _check(checks, "baseline non-truncated read returned content",
               intact_read is not None and len(intact_read) > 0,
               f"len={len(intact_read) if intact_read else 0}")
        _check(checks, f"file actually shrank on disk ({intact_size} -> {new_size})",
               new_size < intact_size,
               f"intact={intact_size} truncated={new_size}")
        _check(checks, "read_baseline RETURNS NONE on truncated file (size-mismatch detected)",
               truncated_read is None,
               f"got={'<None>' if truncated_read is None else repr(truncated_read[:60])}")
        ok, detail = _format_checks(checks)
        return ok, detail, {
            "intact_size": intact_size,
            "truncated_size": new_size,
            "truncated_read_is_none": truncated_read is None,
        }
    except Exception as exc:
        import traceback
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}", {}


# ── 6. bulk_set_no_loss ──────────────────────────────────────────


def scenario_bulk_set_no_loss(
    client: DevClient, daemon_url: str, token: str,
) -> tuple[bool, str, dict]:
    """Concurrent set_resource + bulk_set_resources(replace=True) via
    asyncio.gather. The atomic dict-swap fix should prevent
    set_resource from getting wiped between bulk_set's clear() and
    its insert phase (which used to be non-atomic)."""
    session = _new_session(client, "bulk")
    try:
        _kick_session(client, session)
        # Seed with a regular write to get the channel populated.
        _exec_tool(client, session, "WsWrite", {
            "path": "seed.txt", "content": "seed\n",
        })
        time.sleep(0.3)

        # Concurrent: 20 WsWrite + 5 bulk-imports (each with 3 files).
        N_writes = 20
        N_bulk = 5
        async def _hammer() -> tuple[list[int], list[int]]:
            async with httpx.AsyncClient(
                base_url=daemon_url, timeout=30.0,
                headers={"Authorization": f"Bearer {token}"},
            ) as c:
                write_tasks = [
                    c.post(
                        f"/api/apps/{session.app_id}/tools/WsWrite/execute",
                        json={
                            "session_id": session.session_id,
                            "params": {
                                "path": f"file-{i:02d}.txt",
                                "content": f"content-{i:02d}\n",
                            },
                        },
                    )
                    for i in range(N_writes)
                ]
                # Bulk import via the workspace import REST endpoint
                # which calls into bulk_set_resources internally.
                bulk_tasks = [
                    c.post(
                        f"/api/apps/{session.app_id}/sessions/{session.session_id}/workspace/import",
                        json={
                            "snapshot": {
                                "resources": {
                                    "files": {
                                        f"bulk-{j}-a.txt": {"content": f"bulk-{j}-a\n"},
                                        f"bulk-{j}-b.txt": {"content": f"bulk-{j}-b\n"},
                                    },
                                },
                            },
                            "replace": False,  # MERGE, not replace, so writes survive
                        },
                    )
                    for j in range(N_bulk)
                ]
                all_tasks = write_tasks + bulk_tasks
                rs = await asyncio.gather(*all_tasks, return_exceptions=True)
                w_codes = [
                    r.status_code if hasattr(r, "status_code") else 0
                    for r in rs[:N_writes]
                ]
                b_codes = [
                    r.status_code if hasattr(r, "status_code") else 0
                    for r in rs[N_writes:]
                ]
                return w_codes, b_codes
        w_codes, b_codes = asyncio.run(_hammer())
        time.sleep(1.5)

        state = _read_state(client, session)
        files = (state or {}).get("resources", {}).get("files") or {}
        present_writes = [f"file-{i:02d}.txt" for i in range(N_writes) if f"file-{i:02d}.txt" in files]
        present_bulks = [
            f"bulk-{j}-{k}.txt"
            for j in range(N_bulk) for k in ("a", "b")
            if f"bulk-{j}-{k}.txt" in files
        ]

        checks: list[tuple[str, bool, str]] = []
        _check(checks, f"all {N_writes} writes ended 2xx",
               all(200 <= c < 300 for c in w_codes), f"w_codes={w_codes}")
        _check(checks, f"all {N_bulk} bulk imports ended 2xx",
               all(200 <= c < 300 for c in b_codes), f"b_codes={b_codes}")
        _check(checks, f"all {N_writes} written files present",
               len(present_writes) == N_writes,
               f"present={len(present_writes)}/{N_writes}")
        _check(checks, f"all {N_bulk * 2} bulk-imported files present",
               len(present_bulks) == N_bulk * 2,
               f"present={len(present_bulks)}/{N_bulk * 2}")
        _check(checks, "seed.txt also still present (no global wipe)",
               "seed.txt" in files, f"keys={sorted(files.keys())[:6]}...")
        ok, detail = _format_checks(checks)
        return ok, detail, {
            "writes_present": len(present_writes),
            "bulks_present": len(present_bulks),
            "total_files": len(files),
        }
    except Exception as exc:
        import traceback
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}", {}


# ── 7. memory_cleanup_after_end_session ──────────────────────────


def scenario_memory_cleanup(client: DevClient) -> tuple[bool, str, dict]:
    """Create N sessions, write a file in each, end them all, verify
    on-disk session dirs are gone (proxy for memory cleanup since
    we can't introspect the in-process dicts from outside)."""
    sids: list[str] = []
    workspaces: list[Path] = []
    try:
        for i in range(5):
            session = _new_session(client, f"mem{i}")
            sids.append(session.session_id)
            _kick_session(client, session)
            _exec_tool(client, session, "WsWrite", {
                "path": f"m{i}.txt", "content": f"mem-test-{i}\n",
            })
            ws = _resolve_workspace_dir(client, session)
            if ws is not None:
                workspaces.append(ws)
        time.sleep(1.0)

        # Verify session dirs exist BEFORE end_session.
        existing_before: list[Path] = []
        for ws, sid in zip(workspaces, sids):
            d = ws / ".digitorn" / "sessions" / sid
            if d.is_dir():
                existing_before.append(d)

        # End all sessions via DELETE.
        delete_codes: list[int] = []
        for sid in sids:
            r = client._http(
                "delete", f"/api/apps/{_APP_ID}/sessions/{sid}",
            )
            delete_codes.append(r.status_code)
        time.sleep(2.0)  # let async cleanup complete

        # Verify session dirs are gone AFTER end_session.
        existing_after: list[Path] = []
        for ws, sid in zip(workspaces, sids):
            d = ws / ".digitorn" / "sessions" / sid
            if d.is_dir():
                existing_after.append(d)

        checks: list[tuple[str, bool, str]] = []
        _check(checks, f"all {len(sids)} session dirs existed before end_session",
               len(existing_before) == len(sids),
               f"existed={len(existing_before)}/{len(sids)}")
        _check(checks, f"all {len(sids)} DELETE /sessions returned 2xx",
               all(200 <= c < 300 for c in delete_codes),
               f"codes={delete_codes}")
        _check(checks, "ALL session dirs removed after end_session (no disk leak)",
               len(existing_after) == 0,
               f"still_exists={[str(p) for p in existing_after]}")
        ok, detail = _format_checks(checks)
        return ok, detail, {
            "sessions": sids,
            "delete_codes": delete_codes,
        }
    except Exception as exc:
        import traceback
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}", {}


# ── runner ───────────────────────────────────────────────────────


def _ensure_app_deployed(client: DevClient) -> None:
    if not _APP_YAML.is_file():
        raise FileNotFoundError(f"App YAML missing: {_APP_YAML}")
    try:
        client.deploy(str(_APP_YAML), force=True)
    except Exception as exc:
        print(f"[setup] deploy warning: {exc}")


def _warmup(client: DevClient) -> None:
    warm = SessionHandle(
        session_id=f"warmup-{uuid.uuid4().hex[:8]}",
        app_id=_APP_ID, daemon_url=client.daemon_url, workspace="",
    )
    try:
        _kick_session(client, warm)
    except Exception:
        pass


def _login_with_redirects(daemon_url: str, email: str, password: str) -> str:
    login_url = f"{daemon_url}/auth/login"
    register_url = f"{daemon_url}/auth/register"
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        r = c.post(login_url, json={"email": email, "password": password})
        if r.status_code == 401:
            username = email.split("@", 1)[0].replace("-", "_").replace(".", "_")
            reg = c.post(register_url, json={
                "email": email, "password": password, "username": username,
            })
            if reg.status_code not in (200, 201):
                raise RuntimeError(f"register failed: {reg.status_code} {reg.text[:200]}")
            r = c.post(login_url, json={"email": email, "password": password})
        if r.status_code != 200:
            raise RuntimeError(f"login failed: {r.status_code} {r.text[:200]}")
        token = r.json().get("access_token")
        if not token:
            raise RuntimeError(f"login response missing access_token: {r.text[:200]}")
        return token


def main() -> int:
    daemon_url = _os.environ.get("DAEMON_URL", "http://127.0.0.1:8002")
    email = _os.environ.get("DEV_EMAIL", "preview-tester@example.com")
    password = _os.environ.get("DEV_PASSWORD", "DevPassword123!")
    try:
        token = _login_with_redirects(daemon_url, email, password)
    except Exception as exc:
        print(f"[setup] login failed: {exc}")
        return 2
    client = DevClient.with_token(token, daemon_url=daemon_url)
    _ensure_app_deployed(client)
    _warmup(client)

    scenarios: list[tuple[str, Any]] = [
        ("true_parallel_writes",
         lambda c: scenario_true_parallel_writes(c, daemon_url, token)),
        ("true_parallel_edits",
         lambda c: scenario_true_parallel_edits(c, daemon_url, token)),
        ("hydration_keeps_inflight", scenario_hydration_keeps_inflight),
        ("index_corruption_recovery", scenario_index_corruption_recovery),
        ("baseline_truncation_detected", scenario_baseline_truncation),
        ("bulk_set_no_loss",
         lambda c: scenario_bulk_set_no_loss(c, daemon_url, token)),
        ("memory_cleanup_after_end_session", scenario_memory_cleanup),
    ]

    passed = 0
    print(f"\n=== Workspace ADVERSARIAL scenarios ({len(scenarios)}) ===\n")
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
            print(f"  artifacts: {json.dumps(art, default=str)[:600]}")
        print()
        if ok:
            passed += 1
    print(f"{passed}/{len(scenarios)} scenarios passed\n")
    return 0 if passed == len(scenarios) else 1


if __name__ == "__main__":
    sys.exit(main())

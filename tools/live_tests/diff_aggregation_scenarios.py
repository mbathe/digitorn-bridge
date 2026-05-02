"""Live tests for the multi-edit diff aggregation fix.

Repro of the user-reported bug: doing multiple edits on a file used to
leave ``unified_diff_pending`` stuck at ``diff("", current_content)``
because the daemon never auto-snapshotted a baseline. So the diff view
showed "current file as all additions, never any deletions", regardless
of how many edits had happened.

Fix (workspace/module.py): on first write/edit, call
``_ensure_session_baseline`` to pin the pre-mutation state as the
session baseline. Subsequent edits then diff against THAT baseline and
accumulate -/+ pairs correctly.

Three scenarios:

  1. new_file_multi_edit       - brand-new file, 4 edits → diff shows
                                 cumulative -/+ vs initial write
  2. existing_file_overwrite   - pre-existing disk file, agent overwrites
                                 → diff shows -/+ vs disk content
  3. read_then_edit            - agent reads disk file then edits → diff
                                 shows -/+ vs disk content
"""
from __future__ import annotations

import os as _os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from digitorn.testing.client import DevClient
from digitorn.testing.models import SessionHandle


_APP_ID = "ws-preview-test"
_APP_YAML = Path(__file__).parent / "apps" / "ws-preview-test.yaml"


# ── helpers ────────────────────────────────────────────────────


def _new_session(client: DevClient, prefix: str = "diffagg") -> SessionHandle:
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


def _kick_session(client: DevClient, session: SessionHandle) -> None:
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


def _read_file_payload(
    client: DevClient, session: SessionHandle, path: str,
) -> dict[str, Any] | None:
    """Fetch the channel entry for a file by reading state.json from disk
    (the canonical source of truth post-simplification)."""
    state_path = _session_state_json(client, session)
    if state_path is None:
        return None
    # Debounce window is 500ms; give it enough margin.
    deadline = time.time() + 4.0
    while time.time() < deadline:
        if state_path.is_file():
            try:
                import json as _json
                data = _json.loads(state_path.read_text(encoding="utf-8"))
                files = (data.get("resources") or {}).get("files") or {}
                if path in files:
                    return files[path]
            except Exception:
                pass
        time.sleep(0.1)
    return None


def _session_state_json(client: DevClient, session: SessionHandle) -> Path | None:
    r = client._get(
        f"/api/apps/{session.app_id}/sessions/{session.session_id}"
    )
    if r.status_code != 200:
        return None
    ws = ((r.json().get("data") or {}).get("workspace")) or ""
    if not ws:
        return None
    return Path(ws).expanduser() / ".digitorn" / "sessions" / session.session_id / "state.json"


def _count_unified_diff(diff: str) -> tuple[int, int]:
    """Count + and - lines in a unified diff (excluding headers)."""
    if not diff:
        return 0, 0
    ins, dele = 0, 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            ins += 1
        elif line.startswith("-"):
            dele += 1
    return ins, dele


def _resolve_workspace_dir(client: DevClient, session: SessionHandle) -> Path | None:
    r = client._get(f"/api/apps/{session.app_id}/sessions/{session.session_id}")
    if r.status_code != 200:
        return None
    ws = ((r.json().get("data") or {}).get("workspace")) or ""
    return Path(ws).expanduser() if ws else None


# ── scenarios ──────────────────────────────────────────────────


def scenario_new_file_multi_edit(client: DevClient) -> tuple[bool, str, dict]:
    """Brand-new file + 4 edits. unified_diff_pending must show
    cumulative -/+ pairs vs the initial write, not "current as all +".
    """
    session = _new_session(client, "newedit")
    try:
        _kick_session(client, session)

        # Initial write of a 3-line file. After this, the session
        # baseline should be auto-snapshotted to the just-written
        # content so subsequent edits diff against it.
        ops = []
        ops.append(_exec_tool(client, session, "WsWrite", {
            "path": "demo.txt",
            "content": "alpha\nbravo\ncharlie\n",
        }))

        # Four edits, each changes ONE line. Cumulative effect: every
        # original line replaced + one line appended.
        ops.append(_exec_tool(client, session, "WsEdit", {
            "path": "demo.txt",
            "old_string": "charlie", "new_string": "CHARLIE",
        }))
        ops.append(_exec_tool(client, session, "WsEdit", {
            "path": "demo.txt",
            "old_string": "bravo", "new_string": "BRAVO",
        }))
        ops.append(_exec_tool(client, session, "WsEdit", {
            "path": "demo.txt",
            "old_string": "alpha", "new_string": "ALPHA",
        }))
        ops.append(_exec_tool(client, session, "WsEdit", {
            "path": "demo.txt",
            "old_string": "ALPHA\nBRAVO\nCHARLIE\n",
            "new_string": "ALPHA\nBRAVO\nCHARLIE\nDELTA\n",
        }))
        op_results = [
            (
                i,
                bool(r.get("success")) if r else None,
                str((r.get("error") if r else "") or "")[:100],
            )
            for i, r in enumerate(ops)
        ]
        # Give the 500ms persistence debounce time to flush to state.json.
        time.sleep(1.0)

        payload = _read_file_payload(client, session, "demo.txt")
        if payload is None:
            return False, "  [FAIL] file not in channel after writes", {}

        diff = payload.get("unified_diff_pending") or ""
        ins, dele = _count_unified_diff(diff)
        total_ins = payload.get("total_insertions") or 0
        total_del = payload.get("total_deletions") or 0
        ins_pending = payload.get("insertions_pending") or 0
        del_pending = payload.get("deletions_pending") or 0

        checks: list[tuple[str, bool, str]] = []
        # The 4 edits replaced alpha, bravo, charlie + added delta.
        # Diff vs baseline "alpha\nbravo\ncharlie\n":
        #   - 3 deletions (alpha, bravo, charlie)
        #   - 4 insertions (ALPHA, BRAVO, CHARLIE, DELTA)
        checks.append((
            "unified_diff_pending shows BOTH deletions AND additions",
            ins > 0 and dele > 0,
            f"parsed_diff: ins={ins} del={dele} (raw_diff_first_300={diff[:300]!r})",
        ))
        checks.append((
            "deletions_pending matches the parsed diff",
            del_pending == dele,
            f"deletions_pending={del_pending} parsed_del={dele}",
        ))
        checks.append((
            "insertions_pending matches the parsed diff",
            ins_pending == ins,
            f"insertions_pending={ins_pending} parsed_ins={ins}",
        ))
        # Cumulative session counters - the main check is that they
        # accumulate ABOVE the per-op delta of the last edit (which
        # would be 1/1 if accumulation broke).
        checks.append((
            "total_insertions accumulates beyond last-op delta (>=5)",
            total_ins >= 5,
            f"total_insertions={total_ins}",
        ))
        checks.append((
            "total_deletions accumulates beyond last-op delta (>=2)",
            total_del >= 2,
            f"total_deletions={total_del}",
        ))

        ok = all(c[1] for c in checks)
        detail = "\n".join(
            f"  [{'PASS' if c[1] else 'FAIL'}] {c[0]}: {c[2]}" for c in checks
        )
        return ok, detail, {
            "diff_preview": diff[:200],
            "insertions_pending": ins_pending,
            "deletions_pending": del_pending,
            "total_insertions": total_ins,
            "total_deletions": total_del,
            "op_results": op_results,
            "current_content": (payload.get("content") or "")[:200],
        }
    except Exception as exc:
        import traceback
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}", {}


def scenario_existing_file_overwrite(client: DevClient) -> tuple[bool, str, dict]:
    """File pre-exists on disk (sync_to_disk). Agent overwrites it via
    WsWrite. unified_diff_pending must show -/+ vs the disk content,
    not "current as all +".
    """
    session = _new_session(client, "overwrite")
    try:
        _kick_session(client, session)
        ws_dir = _resolve_workspace_dir(client, session)
        if ws_dir is None:
            return False, "  [FAIL] could not resolve workspace dir", {}

        # Plant a pre-existing file ON DISK (bypass the agent).
        target = ws_dir / "preexisting.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("disk-line-1\ndisk-line-2\ndisk-line-3\n", encoding="utf-8")

        # Agent overwrites with completely different content.
        _exec_tool(client, session, "WsWrite", {
            "path": "preexisting.txt",
            "content": "new-line-A\nnew-line-B\nnew-line-C\n",
        })

        payload = _read_file_payload(client, session, "preexisting.txt")
        if payload is None:
            return False, "  [FAIL] file not in channel", {}

        diff = payload.get("unified_diff_pending") or ""
        ins, dele = _count_unified_diff(diff)
        checks: list[tuple[str, bool, str]] = []
        checks.append((
            "unified_diff_pending shows deletions of disk content (>=3)",
            dele >= 3,
            f"parsed_del={dele}",
        ))
        checks.append((
            "unified_diff_pending shows insertions of new content (>=3)",
            ins >= 3,
            f"parsed_ins={ins}",
        ))
        checks.append((
            "diff contains 'disk-line' as a removal",
            "-disk-line" in diff,
            f"diff_first_300={diff[:300]!r}",
        ))
        ok = all(c[1] for c in checks)
        detail = "\n".join(
            f"  [{'PASS' if c[1] else 'FAIL'}] {c[0]}: {c[2]}" for c in checks
        )
        return ok, detail, {"diff_preview": diff[:300]}
    except Exception as exc:
        import traceback
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}", {}


def scenario_read_then_edit(client: DevClient) -> tuple[bool, str, dict]:
    """Agent reads a pre-existing disk file, then edits it. The baseline
    must be the disk content so the edit shows up as a -/+ pair.
    """
    session = _new_session(client, "readedit")
    try:
        _kick_session(client, session)
        ws_dir = _resolve_workspace_dir(client, session)
        if ws_dir is None:
            return False, "  [FAIL] could not resolve workspace dir", {}

        target = ws_dir / "readme.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("first\nsecond\nthird\n", encoding="utf-8")

        # Agent reads it (load into channel via read-through).
        _exec_tool(client, session, "WsRead", {"path": "readme.txt"})
        # Agent edits one line.
        _exec_tool(client, session, "WsEdit", {
            "path": "readme.txt",
            "old_string": "second", "new_string": "SECOND",
        })

        payload = _read_file_payload(client, session, "readme.txt")
        if payload is None:
            return False, "  [FAIL] file not in channel", {}

        diff = payload.get("unified_diff_pending") or ""
        ins, dele = _count_unified_diff(diff)
        checks: list[tuple[str, bool, str]] = [
            (
                "diff contains '-second'",
                "-second" in diff,
                f"diff_first_300={diff[:300]!r}",
            ),
            (
                "diff contains '+SECOND'",
                "+SECOND" in diff,
                "ok",
            ),
            (
                "exactly 1 deletion, 1 insertion",
                ins == 1 and dele == 1,
                f"parsed: ins={ins} del={dele}",
            ),
        ]
        ok = all(c[1] for c in checks)
        detail = "\n".join(
            f"  [{'PASS' if c[1] else 'FAIL'}] {c[0]}: {c[2]}" for c in checks
        )
        return ok, detail, {"diff_preview": diff[:300]}
    except Exception as exc:
        import traceback
        return False, f"EXCEPTION: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}", {}


# ── runner ─────────────────────────────────────────────────────


def _ensure_app_deployed(client: DevClient) -> None:
    if not _APP_YAML.is_file():
        raise FileNotFoundError(f"App YAML missing: {_APP_YAML}")
    try:
        client.deploy(str(_APP_YAML), force=True)
        print(f"[setup] deployed {_APP_ID} from {_APP_YAML}")
    except Exception as exc:
        print(f"[setup] deploy warning: {exc}")


def _warmup(client: DevClient) -> None:
    """First-scenario flake mitigation: run one disposable session
    before the real ones so the app is warm in the daemon."""
    warm = SessionHandle(
        session_id=f"warmup-{uuid.uuid4().hex[:8]}",
        app_id=_APP_ID, daemon_url=client.daemon_url, workspace="",
    )
    try:
        _kick_session(client, warm)
        print(f"[setup] warmup ran on session {warm.session_id}")
    except Exception as exc:
        print(f"[setup] warmup warning: {exc}")


def _login_with_redirects(daemon_url: str, email: str, password: str) -> str:
    import httpx
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
    daemon_url = _os.environ.get("DAEMON_URL", "http://127.0.0.1:8000")
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

    scenarios = [
        ("new_file_multi_edit", scenario_new_file_multi_edit),
        ("existing_file_overwrite", scenario_existing_file_overwrite),
        ("read_then_edit", scenario_read_then_edit),
    ]
    passed = 0
    print(f"\n=== Diff aggregation scenarios ({len(scenarios)}) ===\n")
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
            import json as _json
            print(f"  artifacts: {_json.dumps(art, default=str)[:500]}")
        print()
        if ok:
            passed += 1
    print(f"{passed}/{len(scenarios)} scenarios passed\n")
    return 0 if passed == len(scenarios) else 1


if __name__ == "__main__":
    sys.exit(main())

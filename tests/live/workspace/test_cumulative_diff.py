"""Reproducer for the "last edit only" bug in workspace pending counters.

Simulates the exact scenario the user reported: agent performs N
successive WsEdit calls against a file and we check that
``insertions_pending`` / ``deletions_pending`` / ``unified_diff_pending``
reflect the CUMULATIVE delta vs. the last-approved baseline, not the
per-operation delta.

Runs in-process — no daemon required. Mocks the minimal preview surface
that ``WorkspaceModule._make_payload`` needs.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Make the packages/ layout importable without installing
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages"))

from digitorn.modules.workspace.module import WorkspaceModule
from digitorn.modules.preview.fs_backend import write_baseline


class FakeSession:
    def __init__(self) -> None:
        self._channels: dict[str, dict] = {}

    def channel(self, name: str) -> dict:
        return self._channels.setdefault(name, {})


class FakePreview:
    def __init__(self, ws_dir: str, sid: str) -> None:
        self._sid = sid
        self._sess = FakeSession()
        self._session_workspaces = {sid: ws_dir}

    def _session(self) -> FakeSession:
        return self._sess

    def _resolve_session_id(self) -> str:
        return self._sid


def _count_diff_lines(diff: str, prefix: str) -> int:
    n = 0
    for line in diff.split("\n"):
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith(prefix):
            n += 1
    return n


def run() -> int:
    failures: list[str] = []
    ws = tempfile.mkdtemp(prefix="dg-wstest-")
    sid = "test-session"

    mod = WorkspaceModule()
    mod._preview = FakePreview(ws, sid)

    # ── Test 1: new file, no baseline → diff must not be empty (Fix A)
    p1 = mod._make_payload("foo.py", "l1\nl2\nl3\n", operation="write")
    mod._channel()["foo.py"] = p1
    if p1["insertions_pending"] != 3:
        failures.append(
            f"T1 new-file insertions_pending: expected 3, got {p1['insertions_pending']}"
        )
    if p1["deletions_pending"] != 0:
        failures.append(
            f"T1 new-file deletions_pending: expected 0, got {p1['deletions_pending']}"
        )
    if not p1["unified_diff_pending"]:
        failures.append("T1 new-file unified_diff_pending: expected non-empty (Fix A)")
    if "+l1" not in p1["unified_diff_pending"]:
        failures.append("T1 new-file unified_diff_pending: missing +l1")

    # ── Test 2: approve → baseline matches current → pending = 0
    write_baseline(ws, sid, "foo.py", "l1\nl2\nl3\n")
    p2 = mod._make_payload(
        "foo.py", "l1\nl2\nl3\n",
        old_content="l1\nl2\nl3\n", operation="edit",
    )
    mod._channel()["foo.py"] = p2
    if p2["insertions_pending"] != 0 or p2["deletions_pending"] != 0:
        failures.append(
            f"T2 post-approve pending: expected 0/0, got "
            f"{p2['insertions_pending']}/{p2['deletions_pending']}"
        )
    if p2["unified_diff_pending"] != "":
        failures.append(
            f"T2 post-approve diff: expected empty, got {len(p2['unified_diff_pending'])} chars"
        )

    # ── Test 3: 100 successive edits, each appends one line
    # Expected: insertions_pending grows 1..100, deletions_pending stays 0.
    current = "l1\nl2\nl3\n"
    for i in range(100):
        prev = current
        current = current + f"extra{i}\n"
        p = mod._make_payload(
            "foo.py", current,
            old_content=prev, operation="edit",
        )
        mod._channel()["foo.py"] = p
        expected_ins = i + 1
        if p["insertions_pending"] != expected_ins:
            failures.append(
                f"T3 iter {i}: insertions_pending expected {expected_ins}, "
                f"got {p['insertions_pending']}"
            )
            break
        if p["deletions_pending"] != 0:
            failures.append(
                f"T3 iter {i}: deletions_pending expected 0, got {p['deletions_pending']}"
            )
            break
        if not p["unified_diff_pending"]:
            failures.append(f"T3 iter {i}: unified_diff_pending is empty")
            break

    final = mod._channel()["foo.py"]
    diff_plus = _count_diff_lines(final["unified_diff_pending"], "+")
    diff_minus = _count_diff_lines(final["unified_diff_pending"], "-")

    print(f"After 100 incremental appends:")
    print(f"  insertions_pending = {final['insertions_pending']}  (expected 100)")
    print(f"  deletions_pending  = {final['deletions_pending']}   (expected 0)")
    print(f"  diff + lines       = {diff_plus}")
    print(f"  diff - lines       = {diff_minus}")
    print(f"  total_insertions   = {final['total_insertions']}  (session-wide, grows to 100+)")
    print(f"  total_deletions    = {final['total_deletions']}")

    # ── Test 4: 50 compensating edits (add then remove same line)
    # insertions_pending should end at 100 (the appended lines), NOT 100+50+50.
    for i in range(50):
        prev = current
        # Add a line then remove it
        current = current + "COMPENSATE\n"
        p_add = mod._make_payload("foo.py", current, old_content=prev, operation="edit")
        mod._channel()["foo.py"] = p_add
        current = prev  # remove the line
        p_rem = mod._make_payload("foo.py", current, old_content=p_add["content"], operation="edit")
        mod._channel()["foo.py"] = p_rem

    final2 = mod._channel()["foo.py"]
    if final2["insertions_pending"] != 100:
        failures.append(
            f"T4 after 50 compensating edits: insertions_pending expected 100 "
            f"(unchanged), got {final2['insertions_pending']}"
        )
    if final2["deletions_pending"] != 0:
        failures.append(
            f"T4 after 50 compensating edits: deletions_pending expected 0, "
            f"got {final2['deletions_pending']}"
        )

    print(f"\nAfter 50 compensating add+remove cycles:")
    print(f"  insertions_pending = {final2['insertions_pending']}  (expected 100 — unchanged)")
    print(f"  deletions_pending  = {final2['deletions_pending']}   (expected 0)")
    print(f"  total_insertions   = {final2['total_insertions']}  (grows — includes compensated)")
    print(f"  total_deletions    = {final2['total_deletions']}")

    if failures:
        print(f"\nFAIL: {len(failures)} assertion(s)")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nPASS: all cumulative-counter assertions hold")
    return 0


if __name__ == "__main__":
    sys.exit(run())

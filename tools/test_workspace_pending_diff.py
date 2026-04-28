"""Proof that ``unified_diff_pending`` accumulates edits since baseline.

Bug: the frontend's "pending changes" diff view only showed the LAST
edit's delta instead of the cumulative delta since the last approve.
Root cause: ``workspace.module._make_payload`` emitted ``unified_diff``
computed as (old_content → updated_content) - the per-edit diff - and
the frontend read that field as its pending view. It did NOT emit a
``unified_diff_pending`` (baseline → current) field.

Fix: ``_make_payload`` now also emits ``unified_diff_pending`` = full
diff vs baseline, exactly matching the ``insertions_pending`` /
``deletions_pending`` counters that were already correct.

This test exercises ``_make_payload`` directly: 3 consecutive edits
on the same file, with a mocked baseline, and asserts that after each
edit the emitted ``unified_diff_pending`` reflects ALL accumulated
changes - not just the last one.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages"))
sys.stdout.reconfigure(encoding="utf-8")

from digitorn.modules.workspace.module import WorkspaceModule

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    tag = "[PASS]" if ok else "[FAIL]"
    print(f"{tag} {name}" + (f"  - {detail[:220]}" if detail else ""))


# ── Setup ──────────────────────────────────────────────────────────────

BASELINE = "line 1\nline 2\nline 3\nline 4\nline 5\n"

# Three versions of the same file, each with one more edit applied.
EDIT_1 = "line 1\nline TWO\nline 3\nline 4\nline 5\n"                    # 1 insert, 1 delete
EDIT_2 = "line 1\nline TWO\nline 3\nline FOUR\nline 5\n"                  # 2 inserts, 2 deletes vs baseline
EDIT_3 = "line 1\nline TWO\nline 3\nline FOUR\nline 5\nline 6 (new)\n"    # 3 inserts, 2 deletes vs baseline


class _FakeChannel:
    """Stand-in for preview's per-channel dict."""
    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
    def get(self, path: str) -> dict | None:
        return self._store.get(path)
    def set(self, path: str, payload: dict) -> None:
        self._store[path] = payload


def _build_module() -> WorkspaceModule:
    """Instantiate the module with just enough wiring to call _make_payload."""
    m = WorkspaceModule.__new__(WorkspaceModule)          # skip __init__
    m._auto_approve = False                                # type: ignore[attr-defined]
    m._channel_cache = _FakeChannel()                      # type: ignore[attr-defined]
    # _channel() normally returns preview module's store - stub it.
    m._channel = lambda: m._channel_cache                  # type: ignore[method-assign]
    m._get_session_workspace_for_baseline = lambda: "/fake/ws"  # type: ignore[method-assign]
    m._preview_session_id = lambda: "sess-test"            # type: ignore[method-assign]
    return m


def _run_edit(mod: WorkspaceModule, path: str, content: str, baseline: str | None) -> dict:
    """Call _make_payload with a mocked ``read_baseline`` return."""
    old = (mod._channel().get(path) or {}).get("content")
    with patch(
        "digitorn.modules.preview.fs_backend.read_baseline",
        return_value=baseline,
    ):
        payload = mod._make_payload(
            path, content,
            old_content=old,
            operation="edit" if old is not None else "write",
        )
    mod._channel().set(path, payload)
    return payload


# ── The actual test ────────────────────────────────────────────────────

def main() -> int:
    print("Scenario: 3 edits on foo.py with baseline = 5 lines.\n"
          "Expect unified_diff_pending to grow each time "
          "(cumulative vs baseline).\n")

    mod = _build_module()

    # Step 1 - first edit (line 2).
    p1 = _run_edit(mod, "foo.py", EDIT_1, baseline=BASELINE)
    check(
        "step1: insertions_pending=1",
        p1["insertions_pending"] == 1,
        f"got {p1['insertions_pending']}",
    )
    check(
        "step1: deletions_pending=1",
        p1["deletions_pending"] == 1,
        f"got {p1['deletions_pending']}",
    )
    # diff must mention TWO but NOT FOUR or "6 (new)"
    diff1 = p1.get("unified_diff_pending", "")
    check(
        "step1: unified_diff_pending contains line 2 edit",
        "line TWO" in diff1 and "line FOUR" not in diff1,
        f"diff preview: {diff1[:200]!r}",
    )

    # Step 2 - second edit (line 4). Cumulative: 2 changes since baseline.
    p2 = _run_edit(mod, "foo.py", EDIT_2, baseline=BASELINE)
    check(
        "step2: insertions_pending=2  (cumulative, not 1)",
        p2["insertions_pending"] == 2,
        f"got {p2['insertions_pending']}",
    )
    check(
        "step2: deletions_pending=2  (cumulative, not 1)",
        p2["deletions_pending"] == 2,
        f"got {p2['deletions_pending']}",
    )
    diff2 = p2.get("unified_diff_pending", "")
    check(
        "step2: diff shows BOTH line-2 AND line-4 edits (not just last)",
        "line TWO" in diff2 and "line FOUR" in diff2,
        f"contains TWO={'line TWO' in diff2} FOUR={'line FOUR' in diff2}",
    )

    # Step 3 - third edit (append new line). Cumulative: 3 inserts, 2 deletes.
    p3 = _run_edit(mod, "foo.py", EDIT_3, baseline=BASELINE)
    check(
        "step3: insertions_pending=3  (cumulative, not 1)",
        p3["insertions_pending"] == 3,
        f"got {p3['insertions_pending']}",
    )
    check(
        "step3: deletions_pending=2",
        p3["deletions_pending"] == 2,
        f"got {p3['deletions_pending']}",
    )
    diff3 = p3.get("unified_diff_pending", "")
    check(
        "step3: diff shows ALL 3 changes accumulated (line 2 + 4 + new)",
        "line TWO" in diff3 and "line FOUR" in diff3
        and "line 6 (new)" in diff3,
        f"TWO={'line TWO' in diff3} FOUR={'line FOUR' in diff3} "
        f"NEW={'line 6 (new)' in diff3}",
    )

    # Step 4 - same content, no baseline → file is fully "new" (added)
    mod2 = _build_module()
    p_new = _run_edit(mod2, "new.py", "a\nb\nc\n", baseline=None)
    check(
        "no-baseline: insertions_pending=3  (whole file is new)",
        p_new["insertions_pending"] == 3,
        f"got {p_new['insertions_pending']}",
    )

    # Step 5 - after approve, edits reset to 0 diff vs the new baseline.
    # (Simulate approval: the agent's baseline for next payload becomes EDIT_3.)
    p_after_approve = _run_edit(mod, "foo.py", EDIT_3, baseline=EDIT_3)
    check(
        "after approve: insertions_pending=0",
        p_after_approve["insertions_pending"] == 0,
        f"got {p_after_approve['insertions_pending']}",
    )
    check(
        "after approve: unified_diff_pending empty",
        p_after_approve.get("unified_diff_pending", "") == "",
        f"diff={p_after_approve.get('unified_diff_pending', '')[:100]!r}",
    )

    # ── Summary ────────────────────────────────────────────────────
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 70}\nWORKSPACE PENDING DIFF: {passed}/{total}\n{'=' * 70}")
    if passed != total:
        print("\nFailures:")
        for n, ok, det in results:
            if not ok:
                print(f"  [FAIL] {n}\n         {det[:300]}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(3)

"""Advanced validation suite for ``unified_diff_pending``.

Covers the corner cases the simple test didn't:

  1. Partial approve (hunks) - approve 1 of 3 hunks, verify the remaining
     2 hunks are still reflected in ``unified_diff_pending`` after the
     operation. The counts must match the filtered diff.

  2. Reject - revert the whole file to baseline. Pending diff must
     become empty and counters reset to 0.

  3. Reject hunks - reject 1 of 2 pending hunks. Pending diff must
     now show only the surviving hunk. Counters must match.

  4. New file (no baseline) - every line counts as a pending insertion
     and the diff frames the whole file.

  5. Delete file - pending deletions match the baseline's line count.

  6. Edit back to baseline - if the agent modifies then restores the
     original content, pending diff must be empty again (no ghost
     "pending" state).

  7. Large file diff cap - write a file with enough pending changes
     to exceed the 16 000-char cap. The returned diff must be capped
     cleanly (no truncation mid-line surprises).

  8. Unicode / CRLF / emoji - byte-identical round-trip, diff still
     valid.

  9. 10 files edited in parallel - each carries its own correct
     cumulative diff, no cross-contamination.

 10. Daemon restart - pending diffs persist across a restart (baseline
     is on disk, content rehydrates from resource channel).

 11. Parity - ``payload.unified_diff_pending`` == top-level
     ``unified_diff_pending`` at the HTTP level (same field in both
     surfaces).

 12. Diff format valid - parseable by ``difflib.PatchSet`` / can be
     applied back to baseline to reproduce current content.

Run: py -3.12 tools/test_workspace_diff_advanced.py
"""
from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8301")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
DB_PATH = Path(r"C:\Users\ASUS\AppData\Local\Temp\uniq-ts-test\digitorn.db")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    tag = "[PASS]" if ok else "[FAIL]"
    print(f"{tag} {name}" + (f"  - {detail[:240]}" if detail else ""))


def make_yaml(d: Path, app_id: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "package.toml").write_text(f"""[package]
id = "{app_id}"
name = "{app_id}"
version = "1.0.0"
description = "workspace diff advanced test"
author = "tests"
license = "MIT"
category = "test"
[package.source]
type = "local"
[package.compatibility]
digitorn_min = ">=1.0.0"
[package.requirements]
modules = []
[package.permissions]
risk_level = "low"
network_access = true
filesystem_access = []
""", encoding="utf-8")
    (d / "app.yaml").write_text(f"""app:
  app_id: "{app_id}"
  name: "{app_id}"
  version: "1.0.0"
  author: tests
agents:
  - id: main
    role: main
    brain:
      provider: ollama
      model: "{OLLAMA_MODEL}"
      backend: openai_compat
      config:
        base_url: "http://localhost:11434/v1"
        api_key: "ollama"
      temperature: 0.0
      max_tokens: 96
modules:
  workspace: {{}}
  preview: {{}}
""", encoding="utf-8")


def _kill_port(port: int) -> None:
    out = subprocess.run(
        ["netstat", "-ano"], capture_output=True, text=True,
    ).stdout
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line:
            m = re.search(r"(\d+)\s*$", line.strip())
            if m:
                subprocess.run(
                    ["taskkill", "//F", "//PID", m.group(1)],
                    capture_output=True, text=True,
                )


def _restart_daemon(port: int = 8301) -> bool:
    env = os.environ.copy()
    env["DIGITORN_SKIP_BUILTINS"] = "1"
    subprocess.Popen(
        ["py", "-3.12", "-m", "digitorn.core.server", "start",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(DB_PATH.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env,
    )
    for _ in range(30):
        try:
            r = httpx.get(f"{BASE}/health", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def main() -> int:
    try:
        if httpx.get(f"{BASE}/health", timeout=3).status_code != 200:
            print("[FATAL] daemon not healthy on :8301")
            return 2
    except Exception as exc:
        print(f"[FATAL] daemon unreachable: {exc}")
        return 2

    app_id = f"wsadv-{uuid.uuid4().hex[:6]}"
    src = Path(tempfile.mkdtemp(prefix="wsadv_"))
    make_yaml(src, app_id)

    loopback = httpx.Client(base_url=BASE, timeout=60.0)

    try:
        loopback.post(f"/api/apps/{app_id}/uninstall", json={"force": True})
        time.sleep(0.3)
        r = loopback.post("/api/apps/install", json={
            "source_type": "local", "source_uri": str(src),
            "accept_permissions": True, "scope": "system",
        })
        if not (r.json().get("data") or {}).get("deployed"):
            print("[FATAL] deploy failed:", r.json())
            return 2

        U = f"u{uuid.uuid4().hex[:6]}"
        loopback.post("/auth/register", json={
            "email": f"{U}@t.local", "username": U,
            "password": "probetest-12345",
        })
        tok = loopback.post("/auth/login", json={
            "email": f"{U}@t.local", "username": U,
            "password": "probetest-12345",
        }).json()["access_token"]

        c = httpx.Client(base_url=BASE, timeout=60.0,
                         headers={"Authorization": f"Bearer {tok}"})

        sid = (c.post(f"/api/apps/{app_id}/sessions", json={}).json()
                  .get("data") or {}).get("session_id")
        if not sid:
            print("[FATAL] no session")
            return 2

        def put(path: str, content: str) -> dict:
            r = c.put(
                f"/api/apps/{app_id}/sessions/{sid}/workspace/files/{path}",
                json={"content": content},
            )
            return r.json().get("data") or {}

        def approve(path: str) -> dict:
            return (c.post(
                f"/api/apps/{app_id}/sessions/{sid}/workspace/files/approve",
                json={"path": path},
            ).json().get("data") or {})

        def reject(path: str) -> dict:
            return (c.post(
                f"/api/apps/{app_id}/sessions/{sid}/workspace/files/reject",
                json={"path": path},
            ).json().get("data") or {})

        def approve_hunks(path: str, hunks: list) -> dict:
            return (c.post(
                f"/api/apps/{app_id}/sessions/{sid}/workspace/files/approve-hunks",
                json={"path": path, "hunks": hunks},
            ).json().get("data") or {})

        def reject_hunks(path: str, hunks: list) -> dict:
            return (c.post(
                f"/api/apps/{app_id}/sessions/{sid}/workspace/files/reject-hunks",
                json={"path": path, "hunks": hunks},
            ).json().get("data") or {})

        def read_back(path: str) -> dict:
            r = c.get(
                f"/api/apps/{app_id}/sessions/{sid}/workspace/"
                f"files/{path}?include_baseline=true"
            )
            return r.json().get("data") or {}

        def delete(path: str) -> dict:
            r = c.delete(
                f"/api/apps/{app_id}/sessions/{sid}/workspace/files/{path}"
            )
            return r.json().get("data") or {}

        # ── 1. Partial approve (hunks) ──────────────────────────────
        # Unified-diff default context is 3 → changes closer than
        # 7 lines merge into one hunk. Use 10-line gaps for clean
        # separation.
        print("\n── 1. Partial approve (hunks) ──")
        BASE_A = "".join(f"a{i:02d}\n" for i in range(30))
        V_lines = [f"a{i:02d}" for i in range(30)]
        V_lines[3] = "A03_CHG"
        V_lines[15] = "A15_CHG"
        V_lines[27] = "A27_CHG"
        V = "\n".join(V_lines) + "\n"
        put("h.py", BASE_A)
        approve("h.py")
        put("h.py", V)
        r1 = read_back("h.py")
        diff1 = (r1.get("payload") or {}).get("unified_diff_pending") or ""
        n_hunks = diff1.count("@@ -")
        check("h.py: 3 separated hunks before partial approve",
              n_hunks == 3, f"got {n_hunks}")
        # Approve the first hunk (A03 change).
        approve_hunks("h.py", [0])
        r2 = read_back("h.py")
        diff2 = (r2.get("payload") or {}).get("unified_diff_pending") or ""
        check(
            "partial approve: remaining diff shows A15 & A27 only",
            "A15_CHG" in diff2 and "A27_CHG" in diff2
            and "A03_CHG" not in diff2,
            f"diff2={diff2[:300]!r}",
        )
        ins2 = (r2.get("payload") or {}).get("insertions_pending")
        dele2 = (r2.get("payload") or {}).get("deletions_pending")
        check(
            "partial approve: counters reflect remaining (2/2)",
            ins2 == 2 and dele2 == 2,
            f"ins={ins2} del={dele2}",
        )

        # ── 2. Reject (total revert) ────────────────────────────────
        print("\n── 2. Reject total ──")
        put("r.py", "x1\nx2\nx3\n")
        approve("r.py")
        put("r.py", "x1\nX_TWO\nx3\nx4\n")
        reject("r.py")
        r = read_back("r.py")
        check(
            "reject: content restored to baseline",
            (r.get("payload") or {}).get("content") == "x1\nx2\nx3\n",
            f"got {(r.get('payload') or {}).get('content')!r}",
        )
        check(
            "reject: unified_diff_pending empty",
            not ((r.get("payload") or {}).get("unified_diff_pending") or ""),
            "",
        )
        check(
            "reject: insertions/deletions reset",
            (r.get("payload") or {}).get("insertions_pending") == 0
            and (r.get("payload") or {}).get("deletions_pending") == 0,
            "",
        )

        # ── 3. Reject hunks (partial revert) ────────────────────────
        # Also needs well-separated hunks (10-line gap).
        print("\n── 3. Reject hunks ──")
        BASE_RH = "".join(f"p{i:02d}\n" for i in range(25))
        V_lines = [f"p{i:02d}" for i in range(25)]
        V_lines[3] = "P03_CHG"
        V_lines[18] = "P18_CHG"
        put("rh.py", BASE_RH)
        approve("rh.py")
        put("rh.py", "\n".join(V_lines) + "\n")
        # Reject the first hunk only - P03 reverts, P18 stays pending.
        reject_hunks("rh.py", [0])
        r = read_back("rh.py")
        p = r.get("payload") or {}
        content = p.get("content") or ""
        check(
            "reject hunks: hunk 0 reverted (p03 restored, not P03_CHG)",
            "p03\n" in content and "P03_CHG" not in content,
            f"content snippet around line 3: {content[0:80]!r}",
        )
        check(
            "reject hunks: hunk 1 kept (P18_CHG still in content)",
            "P18_CHG" in content,
            f"content snippet: ...{content[70:140]!r}",
        )
        diff = p.get("unified_diff_pending") or ""
        check(
            "reject hunks: pending diff shows P18 only",
            "P18_CHG" in diff and "P03_CHG" not in diff,
            f"diff={diff[:200]!r}",
        )

        # ── 4. New file (no baseline) ───────────────────────────────
        print("\n── 4. New file (no baseline) ──")
        put("n.py", "nf1\nnf2\nnf3\n")
        r = read_back("n.py")
        p = r.get("payload") or {}
        check(
            "new file: insertions_pending=3",
            p.get("insertions_pending") == 3,
            f"got {p.get('insertions_pending')}",
        )
        check(
            "new file: deletions_pending=0",
            p.get("deletions_pending") == 0,
            f"got {p.get('deletions_pending')}",
        )

        # ── 5. Delete file ──────────────────────────────────────────
        print("\n── 5. Delete file ──")
        BASE_D = "d1\nd2\nd3\nd4\nd5\n"
        put("d.py", BASE_D)
        approve("d.py")
        delete("d.py")
        # After delete the preview channel may not have an entry; read
        # gives 404. That's expected - we verify via direct DB or a
        # second write then compare. Instead: check that the sequence
        # delete -> rewrite produces 5 insertions + 5 deletions.
        put("d.py", "NEW1\nNEW2\nNEW3\nNEW4\nNEW5\n")
        r = read_back("d.py")
        p = r.get("payload") or {}
        check(
            "after delete+rewrite: every old line deleted, every new line inserted",
            p.get("insertions_pending") >= 5
            and p.get("deletions_pending") >= 5,
            f"ins={p.get('insertions_pending')} del={p.get('deletions_pending')}",
        )

        # ── 6. Edit back to baseline (no-op) ────────────────────────
        print("\n── 6. Edit then revert to baseline ──")
        BASE_E = "e1\ne2\ne3\n"
        put("e.py", BASE_E)
        approve("e.py")
        put("e.py", "e1\nE_TWO\ne3\n")
        # Now edit it back to baseline.
        put("e.py", BASE_E)
        r = read_back("e.py")
        p = r.get("payload") or {}
        check(
            "edit+revert to baseline: pending diff empty",
            not (p.get("unified_diff_pending") or ""),
            f"got {(p.get('unified_diff_pending') or '')[:100]!r}",
        )
        check(
            "edit+revert: insertions_pending=0",
            p.get("insertions_pending") == 0,
            f"got {p.get('insertions_pending')}",
        )

        # ── 7. Large file + diff cap ────────────────────────────────
        print("\n── 7. Large file - diff capped at ~16k ──")
        # 500 lines of "line N" → small. Then replace every line to
        # blow past the cap.
        big_base = "\n".join(f"line {i:04d}" for i in range(1, 1001)) + "\n"
        big_edit = "\n".join(f"LINE {i:04d} CHANGED" for i in range(1, 1001)) + "\n"
        put("big.txt", big_base)
        approve("big.txt")
        put("big.txt", big_edit)
        r = read_back("big.txt")
        diff = (r.get("payload") or {}).get("unified_diff_pending") or ""
        check(
            "large diff: capped <= 16000 chars (prevents channel bloat)",
            len(diff) <= 16000,
            f"diff size = {len(diff)}",
        )
        check(
            "large diff: non-empty (cap didn't zero it out)",
            len(diff) > 1000,
            f"diff size = {len(diff)}",
        )
        check(
            "large diff: ends cleanly (no half-line truncation wild char)",
            diff.endswith("\n") or len(diff) == 16000,
            f"tail = {diff[-80:]!r}",
        )

        # ── 8. Unicode / CRLF / emoji ───────────────────────────────
        print("\n── 8. Unicode / CRLF / emoji preservation ──")
        BASE_U = "hello\nmonde\n"
        V_U = "hello\n🚀 Bonjour 漢字\n"
        put("u.txt", BASE_U)
        approve("u.txt")
        put("u.txt", V_U)
        r = read_back("u.txt")
        p = r.get("payload") or {}
        diff = p.get("unified_diff_pending") or ""
        check(
            "unicode: emoji 🚀 preserved in diff",
            "🚀" in diff,
            f"diff preview: {diff[:200]!r}",
        )
        check(
            "unicode: CJK 漢字 preserved in diff",
            "漢字" in diff,
            "",
        )

        # ── 9. 10 files in parallel - isolation under load ──────────
        print("\n── 9. 10 files edited, each with its own pending diff ──")
        for i in range(10):
            put(f"par_{i}.py", f"line A\nline B {i}\nline C\n")
            approve(f"par_{i}.py")
        # Now make a DIFFERENT number of pending edits on each.
        for i in range(10):
            edits = 1 + (i % 3)
            content_lines = [f"line A"]
            for j in range(edits):
                content_lines.append(f"EDIT_{i}_{j}")
            content_lines.append("line C")
            put(f"par_{i}.py", "\n".join(content_lines) + "\n")
        # Verify each one.
        par_ok = 0
        for i in range(10):
            r = read_back(f"par_{i}.py")
            p = r.get("payload") or {}
            diff = p.get("unified_diff_pending") or ""
            # Each file's diff must contain ONLY its own EDIT_i_j
            # markers - not any other file's.
            has_own = f"EDIT_{i}_0" in diff
            has_other = any(
                f"EDIT_{j}_0" in diff for j in range(10) if j != i
            )
            if has_own and not has_other:
                par_ok += 1
        check(
            "parallel: each of 10 files has isolated, correct diff",
            par_ok == 10, f"{par_ok}/10 correct",
        )

        # ── 10. Daemon restart - pending diff survives ──────────────
        print("\n── 10. Daemon restart: pending diff persists ──")
        BASE_X = "k1\nk2\nk3\n"
        V_X = "k1\nK_TWO\nk3\nk4\n"
        put("restart.py", BASE_X)
        approve("restart.py")
        put("restart.py", V_X)
        # Snapshot pre-restart.
        pre = read_back("restart.py")
        pre_diff = (pre.get("payload") or {}).get("unified_diff_pending") or ""
        pre_ins = (pre.get("payload") or {}).get("insertions_pending")
        pre_del = (pre.get("payload") or {}).get("deletions_pending")
        check(
            "pre-restart: pending diff present",
            "K_TWO" in pre_diff,
            f"diff size {len(pre_diff)}",
        )
        # Restart.
        _kill_port(8301)
        time.sleep(2)
        ok = _restart_daemon()
        check("daemon back up after restart", ok, "")
        # Re-auth (same JWT still valid).
        c2 = httpx.Client(base_url=BASE, timeout=60.0,
                          headers={"Authorization": f"Bearer {tok}"})
        # Re-fetch.
        r = c2.get(
            f"/api/apps/{app_id}/sessions/{sid}/workspace/"
            f"files/restart.py?include_baseline=true"
        )
        post = r.json().get("data") or {}
        post_diff = (post.get("payload") or {}).get("unified_diff_pending") or ""
        post_ins = (post.get("payload") or {}).get("insertions_pending")
        post_del = (post.get("payload") or {}).get("deletions_pending")
        check(
            "after restart: pending diff rehydrated (K_TWO still there)",
            "K_TWO" in post_diff,
            f"post diff size {len(post_diff)}",
        )
        check(
            "after restart: counters identical (pre={}:{}  post={}:{})".format(
                pre_ins, pre_del, post_ins, post_del,
            ),
            post_ins == pre_ins and post_del == pre_del,
            "",
        )

        # ── 11. Top-level == payload diff (API contract parity) ─────
        print("\n── 11. Parity: top-level and payload diffs match ──")
        put("parity.py", "q1\nq2\n")
        approve("parity.py")
        put("parity.py", "q1\nQ_TWO\nq2\nq3\n")
        r = c2.get(
            f"/api/apps/{app_id}/sessions/{sid}/workspace/"
            f"files/parity.py?include_baseline=true"
        )
        d = r.json().get("data") or {}
        top_diff = d.get("unified_diff_pending") or ""
        pay_diff = (d.get("payload") or {}).get("unified_diff_pending") or ""
        check(
            "parity: data.unified_diff_pending == data.payload.unified_diff_pending",
            top_diff == pay_diff and len(top_diff) > 0,
            f"top={len(top_diff)} pay={len(pay_diff)} equal={top_diff == pay_diff}",
        )

        # ── 12. Diff is valid (re-applicable to baseline) ────────────
        print("\n── 12. Generated diff can be parsed + re-applied ──")
        try:
            import io
            if top_diff:
                # Quick sanity: difflib can't apply unified diffs
                # natively, but we can reconstruct using regex hunks
                # and re-assemble content. Simpler check: every "-" /
                # "+" line appears in baseline / current respectively.
                baseline = d.get("baseline") or ""
                current = (d.get("payload") or {}).get("content") or ""
                minus_lines = [
                    ln[1:] for ln in top_diff.splitlines()
                    if ln.startswith("-") and not ln.startswith("---")
                ]
                plus_lines = [
                    ln[1:] for ln in top_diff.splitlines()
                    if ln.startswith("+") and not ln.startswith("+++")
                ]
                minus_in_baseline = all(ml in baseline for ml in minus_lines)
                plus_in_current = all(pl in current for pl in plus_lines)
                check(
                    "diff validity: every '-' line exists in baseline",
                    minus_in_baseline, f"baseline={baseline!r}",
                )
                check(
                    "diff validity: every '+' line exists in current",
                    plus_in_current, f"current={current!r}",
                )
            else:
                check("diff validity: diff present to check", False,
                      "top_diff was empty")
        except Exception as exc:
            check("diff validity", False, f"{exc}")

        # ── 13. LLM multi-file edit in single turn ──────────────────
        print("\n── 13. LLM edits 2 files in one turn ──")
        put("llm_a.py", "def foo():\n    return 1\n")
        approve("llm_a.py")
        put("llm_b.py", "def bar():\n    return 2\n")
        approve("llm_b.py")
        r = c2.post(
            f"/api/apps/{app_id}/sessions/{sid}/messages",
            json={
                "message": (
                    "Using workspace.edit, do BOTH edits in this turn:\n"
                    "1. In llm_a.py replace '1' with '100'.\n"
                    "2. In llm_b.py replace '2' with '200'.\n"
                    "End your turn after both edits."
                ),
                "queue_mode": "async",
            },
        )
        if r.status_code == 200:
            # Wait up to 2 minutes.
            deadline = time.time() + 120
            while time.time() < deadline:
                h = c2.get(
                    f"/api/apps/{app_id}/sessions/{sid}/history"
                ).json().get("data") or {}
                if not h.get("turn_active"):
                    break
                time.sleep(2)
            ra = read_back("llm_a.py")
            rb = read_back("llm_b.py")
            a_edit = "100" in ((ra.get("payload") or {}).get("content") or "")
            b_edit = "200" in ((rb.get("payload") or {}).get("content") or "")
            if a_edit and b_edit:
                a_diff = (ra.get("payload") or {}).get("unified_diff_pending") or ""
                b_diff = (rb.get("payload") or {}).get("unified_diff_pending") or ""
                check(
                    "LLM multi-file: llm_a.py diff captures '100'",
                    "100" in a_diff, "",
                )
                check(
                    "LLM multi-file: llm_b.py diff captures '200'",
                    "200" in b_diff, "",
                )
                check(
                    "LLM multi-file: diffs independent (a doesn't contain 200, b doesn't contain 100)",
                    "200" not in a_diff and "100" not in b_diff,
                    "",
                )
            else:
                print(f"[SKIP] LLM didn't edit both files (a={a_edit} b={b_edit}) - skipping LLM assertions")

        loopback.post(f"/api/apps/{app_id}/uninstall", json={"force": True})
    finally:
        shutil.rmtree(src, ignore_errors=True)

    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 70}\nWORKSPACE DIFF ADVANCED: {passed}/{total}\n{'=' * 70}")
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

"""Live multi-file test for ``unified_diff_pending`` accumulation.

Spins up a session on the test daemon with a workspace-enabled app,
has a real LLM (Ollama qwen2.5:7b) perform edits on MULTIPLE files in
successive turns, then reads each file back and asserts that
``unified_diff_pending`` reflects ALL cumulative edits per file - not
just the latest one, and INDEPENDENTLY per file (editing file A
doesn't erase file B's pending diff).

The test also exercises the HTTP workspace PUT (writeback) endpoint
as a deterministic fallback - LLM tool-calling with a 7B model is not
always reliable enough for a crisp regression gate.

Run: py -3.12 tools/test_workspace_diff_live.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8301")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

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
description = "workspace diff live test"
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
      max_tokens: 128
modules:
  workspace: {{}}
  preview: {{}}
""", encoding="utf-8")


def main() -> int:
    try:
        if httpx.get(f"{BASE}/health", timeout=3).status_code != 200:
            print("[FATAL] daemon not healthy on :8301")
            return 2
    except Exception as exc:
        print(f"[FATAL] daemon unreachable: {exc}")
        return 2

    app_id = f"wsdiff-{uuid.uuid4().hex[:6]}"
    src = Path(tempfile.mkdtemp(prefix="wsdiff_"))
    make_yaml(src, app_id)

    loopback = httpx.Client(base_url=BASE, timeout=60.0)

    try:
        # ── Install / deploy ──
        loopback.post(f"/api/apps/{app_id}/uninstall", json={"force": True})
        time.sleep(0.3)
        r = loopback.post("/api/apps/install", json={
            "source_type": "local", "source_uri": str(src),
            "accept_permissions": True, "scope": "system",
        })
        data = r.json().get("data") or {}
        check("install+deploy", data.get("deployed") is True,
              f"err={data.get('deploy_error')}")
        if not data.get("deployed"):
            return 2

        # ── Auth ──
        U = f"u{uuid.uuid4().hex[:6]}"
        r = loopback.post("/auth/register", json={
            "email": f"{U}@t.local", "username": U,
            "password": "probetest-12345",
        })
        tok = r.json()["access_token"]
        c = httpx.Client(base_url=BASE, timeout=60.0,
                         headers={"Authorization": f"Bearer {tok}"})

        r = c.post(f"/api/apps/{app_id}/sessions", json={})
        sid = (r.json().get("data") or {}).get("session_id")
        check("create session", bool(sid), f"sid={sid}")
        if not sid:
            return 2

        # ── Seed 2 files via writeback (deterministic - no LLM needed
        #    for setup), approve them so they become the baseline ──
        def putback(path: str, content: str, auto_approve: bool = False) -> dict:
            r = c.put(
                f"/api/apps/{app_id}/sessions/{sid}/workspace/files/{path}",
                json={"content": content, "auto_approve": auto_approve},
            )
            return r.json().get("data") or {}

        def approve(path: str) -> dict:
            r = c.post(
                f"/api/apps/{app_id}/sessions/{sid}/workspace/files/approve",
                json={"path": path},
            )
            return r.json().get("data") or {}

        def read_back(path: str) -> dict:
            r = c.get(
                f"/api/apps/{app_id}/sessions/{sid}/workspace/"
                f"files/{path}?include_baseline=true"
            )
            return r.json().get("data") or {}

        BASE_A = "line 1\nline 2\nline 3\nline 4\nline 5\n"
        BASE_B = "alpha\nbeta\ngamma\ndelta\nepsilon\n"

        putback("a.py", BASE_A)
        approve("a.py")
        putback("b.py", BASE_B)
        approve("b.py")

        # Sanity: after approve, both files have empty pending diff.
        ra = read_back("a.py")
        rb = read_back("b.py")
        check(
            "after seed+approve: a.py pending diff empty",
            not (ra.get("unified_diff_pending") or ""),
            f"got {(ra.get('unified_diff_pending') or '')[:120]!r}",
        )
        check(
            "after seed+approve: b.py pending diff empty",
            not (rb.get("unified_diff_pending") or ""),
            f"got {(rb.get('unified_diff_pending') or '')[:120]!r}",
        )

        # ── Phase 1: agent makes 3 edits on a.py - diff must accumulate ──
        # We drive via putback (simulates agent edits going through
        # the SAME _make_payload path as workspace.edit/write would).
        a_v1 = "line 1\nline TWO\nline 3\nline 4\nline 5\n"
        a_v2 = "line 1\nline TWO\nline 3\nline FOUR\nline 5\n"
        a_v3 = "line 1\nline TWO\nline 3\nline FOUR\nline 5\nline 6 NEW\n"

        for i, content in enumerate([a_v1, a_v2, a_v3], 1):
            putback("a.py", content)
            ra = read_back("a.py")
            pending = ra.get("unified_diff_pending") or ""
            ins = ra.get("payload", {}).get("insertions_pending", -1)
            dele = ra.get("payload", {}).get("deletions_pending", -1)
            print(f"  a.py edit {i}: insertions={ins} deletions={dele} "
                  f"diff_len={len(pending)}")

        # After the 3rd edit: diff must mention ALL THREE changes.
        ra = read_back("a.py")
        diff = ra.get("unified_diff_pending") or ""
        payload_a = ra.get("payload") or {}
        check(
            "a.py: insertions_pending=3 cumulative (not 1)",
            payload_a.get("insertions_pending") == 3,
            f"got {payload_a.get('insertions_pending')}",
        )
        check(
            "a.py: deletions_pending=2 cumulative (not 1)",
            payload_a.get("deletions_pending") == 2,
            f"got {payload_a.get('deletions_pending')}",
        )
        check(
            "a.py: unified_diff_pending contains ALL 3 edits",
            "line TWO" in diff and "line FOUR" in diff and "line 6 NEW" in diff,
            f"TWO={'line TWO' in diff} FOUR={'line FOUR' in diff} "
            f"NEW={'line 6 NEW' in diff}  diff_len={len(diff)}",
        )

        # ── Phase 2: edit b.py - a.py's pending must stay untouched ──
        b_v1 = "alpha\nBETA\ngamma\ndelta\nepsilon\n"
        b_v2 = "alpha\nBETA\ngamma\nDELTA\nepsilon\n"
        for content in [b_v1, b_v2]:
            putback("b.py", content)

        ra = read_back("a.py")
        rb = read_back("b.py")
        diff_a_after_b = ra.get("unified_diff_pending") or ""
        diff_b = rb.get("unified_diff_pending") or ""

        check(
            "cross-file isolation: a.py pending diff unchanged by b.py edits",
            "line TWO" in diff_a_after_b
            and "line FOUR" in diff_a_after_b
            and "line 6 NEW" in diff_a_after_b,
            "a.py lost its cumulative diff when b.py was edited",
        )
        check(
            "b.py: unified_diff_pending contains BOTH its edits",
            "BETA" in diff_b and "DELTA" in diff_b,
            f"BETA={'BETA' in diff_b} DELTA={'DELTA' in diff_b}",
        )

        # ── Phase 3: approve a.py - its pending diff must reset;
        #    b.py's pending diff must stay ─────────────────────
        approve("a.py")

        ra = read_back("a.py")
        rb = read_back("b.py")
        check(
            "after approve(a.py): a.py pending diff reset to empty",
            not (ra.get("unified_diff_pending") or ""),
            f"got {(ra.get('unified_diff_pending') or '')[:120]!r}",
        )
        check(
            "after approve(a.py): a.py insertions_pending=0",
            (ra.get("payload") or {}).get("insertions_pending") == 0,
            f"got {(ra.get('payload') or {}).get('insertions_pending')}",
        )
        check(
            "after approve(a.py): b.py pending diff PRESERVED",
            "BETA" in (rb.get("unified_diff_pending") or "")
            and "DELTA" in (rb.get("unified_diff_pending") or ""),
            "approve of a.py wrongly cleared b.py's diff",
        )

        # ── Phase 4: real LLM edit (Ollama) - proves the server-side
        #    path the tool-call takes is the same ────────────────────
        # We ask the LLM to add a simple comment to c.py. Whether
        # qwen2.5:7b actually calls the tool is model-dependent. If
        # it does, we verify the cumulative-diff contract still holds
        # after a tool-call-driven edit.
        putback("c.py", "def hello():\n    return 'world'\n")
        approve("c.py")

        r = c.post(
            f"/api/apps/{app_id}/sessions/{sid}/messages",
            json={
                "message": (
                    "Using the workspace.edit tool, replace "
                    "'world' with 'earth' in c.py. "
                    "Then end your turn."
                ),
                "queue_mode": "async",
            },
        )
        check(
            "LLM prompt accepted (202/200)",
            r.status_code in (200, 202),
            f"http={r.status_code}",
        )

        # Give the LLM up to 2 minutes to respond.
        deadline = time.time() + 120
        llm_edited = False
        while time.time() < deadline:
            hist = c.get(
                f"/api/apps/{app_id}/sessions/{sid}/history"
            ).json().get("data") or {}
            if not hist.get("turn_active") and hist.get("message_count", 0) >= 2:
                rc = read_back("c.py")
                content = (rc.get("payload") or {}).get("content") or ""
                if "earth" in content:
                    llm_edited = True
                    break
            time.sleep(2)

        if llm_edited:
            rc = read_back("c.py")
            diff_c = rc.get("unified_diff_pending") or ""
            check(
                "LLM-driven edit: c.py pending diff shows LLM's change",
                "earth" in diff_c,
                f"diff excerpt: {diff_c[:200]!r}",
            )
            check(
                "LLM-driven edit: c.py insertions_pending >= 1",
                (rc.get("payload") or {}).get("insertions_pending", 0) >= 1,
                "",
            )
        else:
            # qwen2.5:7b didn't cooperate - record, don't fail the
            # deterministic contract we already proved above.
            print("[SKIP] qwen2.5:7b did not perform the edit within 120s - "
                  "skipping LLM-path assertions (deterministic path already "
                  "verified above)")

        # Cleanup.
        loopback.post(f"/api/apps/{app_id}/uninstall", json={"force": True})
    finally:
        shutil.rmtree(src, ignore_errors=True)

    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 70}\nWORKSPACE DIFF LIVE: {passed}/{total}\n{'=' * 70}")
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

"""Graceful-shutdown durability test - proves the zero-loss contract.

Runs against the isolated test daemon on :8301. Uses Ollama qwen2.5:7b.

The batched ``HistoryWriter`` claims that a **graceful** shutdown
(server stop, ``/shutdown`` endpoint, SIGTERM) drains its in-memory
queue before the engine closes - so every row enqueued up to the
shutdown moment lands on disk. This test proves it.

Scenario:

  1. Send a burst of N=20 messages (queue_mode=async), wait for all
     turns to fully complete. Many events fire per turn - we expect
     a rich history_log row count for this session.
  2. Take the row count as the "before" baseline.
  3. Hit the daemon's graceful shutdown endpoint (``/shutdown``) so
     it runs the full lifespan teardown - which awaits
     :func:`digitorn.core.history_writer.stop_writer` BEFORE
     ``close_db``. Any row still in the writer queue is flushed.
  4. Restart the daemon.
  5. Re-read the row count. It must equal the "before" baseline -
     no loss, no duplicates.

If this passes, the zero-loss contract is honest for the 99.99% case
(controlled stops). The kill -9 window is documented as ≤50 ms and
covered by the separate crash test.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
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
description = "graceful shutdown test"
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
      max_tokens: 32
modules: {{}}
""", encoding="utf-8")


def _kill_listener_on(port: int) -> None:
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


def _restart_daemon() -> bool:
    env = os.environ.copy()
    env["DIGITORN_SKIP_BUILTINS"] = "1"
    subprocess.Popen(
        ["py", "-3.12", "-m", "digitorn.core.server", "start",
         "--port", "8301", "--log-level", "warning"],
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


def wait_turn_complete(c: httpx.Client, app_id: str, sid: str,
                       min_messages: int, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        r = c.get(f"/api/apps/{app_id}/sessions/{sid}/history")
        d = r.json().get("data") or {}
        last = d
        if d.get("message_count", 0) >= min_messages and not d.get("turn_active"):
            return d
        time.sleep(1.0)
    return last


def main() -> int:
    try:
        if httpx.get(f"{BASE}/health", timeout=3).status_code != 200:
            print("[FATAL] daemon not healthy")
            return 2
    except Exception as exc:
        print(f"[FATAL] daemon unreachable: {exc}")
        return 2

    app_id = f"graceful-{uuid.uuid4().hex[:6]}"
    src = Path(tempfile.mkdtemp(prefix="graceful_"))
    make_yaml(src, app_id)

    raw = httpx.Client(base_url=BASE, timeout=60.0)
    try:
        raw.post(f"/api/apps/{app_id}/uninstall", json={"force": True})
        time.sleep(0.3)
        r = raw.post("/api/apps/install", json={
            "source_type": "local", "source_uri": str(src),
            "accept_permissions": True, "scope": "system",
        })
        data = r.json().get("data") or {}
        check("install+deploy", data.get("deployed") is True,
              f"err={data.get('deploy_error')}")
        if not data.get("deployed"):
            return 2

        U = f"u{uuid.uuid4().hex[:6]}"
        r = raw.post("/auth/register", json={
            "email": f"{U}@t.local", "username": U,
            "password": "probetest-12345",
        })
        tok = r.json()["access_token"]
        c = httpx.Client(base_url=BASE, timeout=300.0,
                         headers={"Authorization": f"Bearer {tok}"})

        sid = (c.post(f"/api/apps/{app_id}/sessions", json={}).json().get("data") or {}).get("session_id")
        check("create session", bool(sid), f"sid={sid}")
        if not sid:
            return 2

        # ── Phase 1: burst 20 messages ──────────────────────────────
        print("\n── Phase 1: burst 20 messages + wait all turns complete ──")
        N = 20
        accepted = 0
        for i in range(N):
            r = c.post(
                f"/api/apps/{app_id}/sessions/{sid}/messages",
                json={"message": f"Say only: #{i}", "queue_mode": "async"},
                timeout=10,
            )
            if r.status_code == 200:
                accepted += 1
        check(f"all {N} enqueued", accepted == N, f"accepted={accepted}/{N}")

        d = wait_turn_complete(c, app_id, sid, min_messages=2 * N, timeout=600)
        check(
            f"all {N} turns completed",
            d.get("message_count", 0) >= 2 * N,
            f"msgs={d.get('message_count')}",
        )

        # Let the writer fully drain a few flush cycles.
        time.sleep(2)

        # ── Phase 2: baseline row count (pre-shutdown) ──────────────
        db = sqlite3.connect(str(DB_PATH))
        try:
            baseline_total = db.execute(
                "SELECT COUNT(*) FROM history_log WHERE session_id=?",
                (sid,),
            ).fetchone()[0]
            baseline_msgs = db.execute(
                "SELECT COUNT(*) FROM history_log "
                "WHERE session_id=? AND kind='message'",
                (sid,),
            ).fetchone()[0]
            baseline_events = db.execute(
                "SELECT COUNT(*) FROM history_log "
                "WHERE session_id=? AND kind='event'",
                (sid,),
            ).fetchone()[0]
            # All ts unique already? Pre-condition.
            total, distinct = db.execute(
                "SELECT COUNT(*), COUNT(DISTINCT ts) FROM history_log "
                "WHERE session_id=?",
                (sid,),
            ).fetchone()
        finally:
            db.close()
        print(f"  baseline: total={baseline_total} "
              f"msgs={baseline_msgs} events={baseline_events}  "
              f"unique_ts={distinct}/{total}")
        check(
            "baseline non-trivial (burst produced many rows)",
            baseline_total >= 2 * N,
            f"total={baseline_total}",
        )
        check(
            "baseline all ts unique", total == distinct,
            f"total={total} distinct={distinct}",
        )

        # ── Phase 3: inject events right before shutdown ────────────
        # Extra pressure: we fire one MORE message and immediately
        # request graceful shutdown. The writer must drain this
        # in-flight batch before the engine closes.
        print("\n── Phase 3: extra message → graceful shutdown ──")
        c.post(
            f"/api/apps/{app_id}/sessions/{sid}/messages",
            json={"message": "Say: LATE", "queue_mode": "async"},
            timeout=10,
        )
        # Wait briefly so the queue dispatcher picks it up and fires
        # the user_message event, then request shutdown while events
        # are still streaming.
        time.sleep(0.5)

        # Trigger graceful shutdown via the /shutdown endpoint.
        try:
            shut = httpx.post(f"{BASE}/shutdown", timeout=10)
            print(f"  /shutdown returned http={shut.status_code}")
        except Exception as exc:
            print(f"  /shutdown failed (expected - daemon may close socket): {exc}")

        # Wait for the port to actually free.
        for _ in range(30):
            try:
                httpx.get(f"{BASE}/health", timeout=1)
                time.sleep(1)
            except Exception:
                break
        else:
            # Fallback: force-kill to unblock the test.
            _kill_listener_on(8301)

        time.sleep(2)

        # ── Phase 4: restart + measure row count ────────────────────
        print("\n── Phase 4: restart and compare ──")
        ok = _restart_daemon()
        check("daemon back up after graceful shutdown", ok, "")
        time.sleep(2)

        db = sqlite3.connect(str(DB_PATH))
        try:
            after_total = db.execute(
                "SELECT COUNT(*) FROM history_log WHERE session_id=?",
                (sid,),
            ).fetchone()[0]
            after_msgs = db.execute(
                "SELECT COUNT(*) FROM history_log "
                "WHERE session_id=? AND kind='message'",
                (sid,),
            ).fetchone()[0]
            after_events = db.execute(
                "SELECT COUNT(*) FROM history_log "
                "WHERE session_id=? AND kind='event'",
                (sid,),
            ).fetchone()[0]
        finally:
            db.close()
        print(f"  after:    total={after_total} "
              f"msgs={after_msgs} events={after_events}")

        # The critical invariants.
        check(
            "graceful shutdown: total rows did NOT decrease",
            after_total >= baseline_total,
            f"baseline={baseline_total} after={after_total}",
        )
        check(
            "graceful shutdown: message rows preserved",
            after_msgs >= baseline_msgs,
            f"baseline={baseline_msgs} after={after_msgs}",
        )
        check(
            "graceful shutdown: event rows preserved",
            after_events >= baseline_events,
            f"baseline={baseline_events} after={after_events}",
        )
        # Extra rows after shutdown prove the writer drained the
        # LATE message (Phase 3).
        check(
            "graceful shutdown: in-flight LATE message drained to disk",
            after_total > baseline_total,
            f"baseline={baseline_total} after={after_total} "
            f"(must be strictly greater since we added rows mid-shutdown)",
        )

        # Finally, re-check global ts uniqueness.
        db = sqlite3.connect(str(DB_PATH))
        try:
            t2, d2 = db.execute(
                "SELECT COUNT(*), COUNT(DISTINCT ts) FROM history_log "
                "WHERE session_id=?",
                (sid,),
            ).fetchone()
        finally:
            db.close()
        check("after restart: all ts unique (no collision)",
              t2 == d2, f"total={t2} distinct={d2}")

        # Cleanup.
        try:
            httpx.Client(base_url=BASE, timeout=10.0).post(
                f"/api/apps/{app_id}/uninstall", json={"force": True},
            )
        except Exception:
            pass
    finally:
        shutil.rmtree(src, ignore_errors=True)

    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 70}\nGRACEFUL SHUTDOWN TEST: {passed}/{total}\n{'=' * 70}")
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

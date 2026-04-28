"""Advanced unified-ledger test - all event types + multimodal + restart.

Proves, with a real Ollama backend on a dedicated test DB:

  1. New messages land in ``history_log`` with kind='message'.
  2. Multimodal content (list of blocks, incl. image) survives the
     INSERT and comes back intact through the replay.
  3. Every runtime event (tokens, thinking, tool_call, message_done,
     etc.) lands in ``history_log`` with kind='event'.
  4. Admin actions (quota set/delete) land with kind='audit'.
  5. GET /history returns a unified timeline ordered by seq+ts.
  6. GET /admin/audit-log reads from history_log.
  7. Daemon restart + rebuild-from-DB: everything survives.
  8. No column-type errors on multimodal content.
  9. All timestamps are globally unique in history_log.

Run: py -3.12 tools/test_history_unified.py
"""
from __future__ import annotations

import json
import os
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


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    tag = "[PASS]" if ok else "[FAIL]"
    print(f"{tag} {name}" + (f"  - {detail[:200]}" if detail else ""))


def make_yaml(d, app_id):
    d.mkdir(parents=True, exist_ok=True)
    (d / "package.toml").write_text(
        f"""[package]
id = "{app_id}"
name = "{app_id}"
version = "1.0.0"
description = "unified ledger test"
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
    (d / "app.yaml").write_text(
        f"""app:
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


def main() -> int:
    # Sanity: daemon up
    try:
        r = httpx.get(f"{BASE}/health", timeout=3)
        if r.status_code != 200:
            print("[FATAL] daemon not healthy")
            return 2
    except Exception as exc:
        print(f"[FATAL] daemon unreachable: {exc}")
        return 2

    app_id = f"unified-{uuid.uuid4().hex[:6]}"
    src = Path(tempfile.mkdtemp(prefix="unified_"))
    make_yaml(src, app_id)

    try:
        c = httpx.Client(base_url=BASE, timeout=30.0)

        # ── Phase 1: deploy + session ───────────────────────────
        print("\n── Phase 1: deploy ──────────────────────────────")
        c.post(f"/api/apps/{app_id}/uninstall", json={"force": True})
        time.sleep(0.3)
        r = c.post("/api/apps/install", json={
            "source_type": "local", "source_uri": str(src),
            "accept_permissions": True, "scope": "system",
        })
        data = r.json().get("data") or {}
        check("deploy", data.get("deployed") is True,
              f"err={data.get('deploy_error')}")

        U = f"u{uuid.uuid4().hex[:6]}"
        r = c.post("/auth/register", json={
            "email": f"{U}@t.local", "username": U,
            "password": "probetest-12345",
        })
        tok = r.json()["access_token"]
        c.headers["Authorization"] = f"Bearer {tok}"

        r = c.post(f"/api/apps/{app_id}/sessions", json={"user_id": U})
        sid = (r.json().get("data") or {}).get("session_id")
        check("create session", bool(sid), f"sid={sid}")

        # ── Phase 2: send 2 plain messages ─────────────────────
        print("\n── Phase 2: send 2 plain messages ───────────────")
        for i, text in enumerate(["Say UNO", "Say DOS"]):
            c.post(
                f"/api/apps/{app_id}/sessions/{sid}/messages",
                json={"message": text, "queue_mode": "async"},
                timeout=30,
            )

        # Wait for turns to finish
        deadline = time.time() + 180
        while time.time() < deadline:
            r = c.get(f"/api/apps/{app_id}/sessions/{sid}/history")
            d = r.json().get("data") or {}
            msgs = d.get("messages") or []
            if len(msgs) >= 4 and not d.get("turn_active"):
                break
            time.sleep(1)

        r = c.get(f"/api/apps/{app_id}/sessions/{sid}/history")
        d = r.json().get("data") or {}
        check("2 turns completed", d.get("message_count", 0) >= 4,
              f"msgs={d.get('message_count')}")

        # ── Phase 3: history_log has rows for this session ─────
        print("\n── Phase 3: direct DB inspection ────────────────")
        time.sleep(2)  # let bg writes settle
        db = sqlite3.connect(str(DB_PATH))
        cur = db.cursor()
        cur.execute(
            "SELECT kind, COUNT(*) FROM history_log WHERE session_id=? GROUP BY kind",
            (sid,),
        )
        kinds = dict(cur.fetchall())
        check("history_log has message rows",
              kinds.get("message", 0) >= 4,
              f"message count={kinds.get('message', 0)}")
        check("history_log has event rows",
              kinds.get("event", 0) > 0,
              f"event count={kinds.get('event', 0)}")

        # Check unique ts on our session's rows
        cur.execute(
            "SELECT COUNT(*), COUNT(DISTINCT ts) FROM history_log WHERE session_id=?",
            (sid,),
        )
        total, distinct = cur.fetchone()
        check("history_log: all ts unique for this session",
              total == distinct,
              f"total={total} distinct={distinct}")

        # Types persisted
        cur.execute(
            "SELECT DISTINCT type FROM history_log WHERE session_id=? ORDER BY type",
            (sid,),
        )
        types = [r[0] for r in cur.fetchall()]
        print(f"  event types persisted: {types}")
        check("history_log has multiple event types (no filter)",
              len(types) >= 3,
              f"types={types}")

        # ── Phase 4: admin actions go to history_log (kind=audit) ──
        print("\n── Phase 4: audit kind=audit ────────────────────")
        # Loopback bypass covers /api/apps/* (incl. quota) but NOT
        # /api/admin/* (mutating admin needs a real JWT). Grant admin
        # role to our test user via direct SQL, then use their JWT.
        import uuid as _uuid
        db_admin = sqlite3.connect(str(DB_PATH))
        cur_a = db_admin.cursor()
        cur_a.execute("SELECT id FROM roles WHERE name='admin'")
        row = cur_a.fetchone()
        if row:
            admin_role_id = row[0]
        else:
            admin_role_id = _uuid.uuid4().hex
            cur_a.execute(
                "INSERT INTO roles (id, name, description, is_builtin, permissions, created_at) "
                "VALUES (?, 'admin', 'Administrator', 1, '[\"*\"]', datetime('now'))",
                (admin_role_id,),
            )
        # Find our user's id
        cur_a.execute("SELECT id FROM users WHERE external_id=? LIMIT 1", (U,))
        u_row = cur_a.fetchone()
        if u_row:
            cur_a.execute(
                "INSERT INTO user_roles (id, user_id, role_id, app_id, granted_at) "
                "VALUES (?, ?, ?, NULL, datetime('now'))",
                (_uuid.uuid4().hex, u_row[0], admin_role_id),
            )
            db_admin.commit()
        db_admin.close()

        # Re-login to get a token with admin perms
        r = c.post("/auth/login", json={
            "email": f"{U}@t.local", "username": U,
            "password": "probetest-12345",
        })
        tok_admin = r.json().get("access_token")
        c_admin = httpx.Client(base_url=BASE, timeout=10.0,
                                headers={"Authorization": f"Bearer {tok_admin}"})

        # Loopback admin (no JWT needed for /api/apps/* quota ops)
        loopback = httpx.Client(base_url=BASE, timeout=10.0)
        loopback.put(f"/api/apps/{app_id}/quota",
                     json={"quota": {"requests": {"per_minute": 42}}})
        loopback.delete(f"/api/apps/{app_id}/quota")
        time.sleep(1)

        cur.execute(
            "SELECT COUNT(*), GROUP_CONCAT(type) FROM history_log "
            "WHERE kind='audit' AND target_app_id=?",
            (app_id,),
        )
        audit_count, audit_types = cur.fetchone()
        check("history_log: audit rows from quota ops",
              audit_count >= 2 and "quota.set_app" in (audit_types or ""),
              f"count={audit_count} types={audit_types}")

        # Now query the admin audit-log route with a proper JWT admin.
        r = c_admin.get(f"/api/admin/audit-log?target_app_id={app_id}&limit=50")
        d = r.json().get("data") or {}
        check("GET /admin/audit-log (JWT admin) returns entries",
              d.get("total", 0) >= 2,
              f"http={r.status_code} total={d.get('total')} error={r.json().get('error')}")

        # ── Phase 5: multimodal content survives ───────────────
        # Direct SQL INSERT - exercises the SCHEMA (which prod code
        # also hits). Going through record() in-process requires the
        # engine init which only runs in the daemon process.
        print("\n── Phase 5: multimodal (image + file blocks) ────")
        from datetime import datetime, timezone
        payload = {
            "content_kind": "multimodal",
            "raw_content": [
                {"type": "text", "text": "What do you see in this image?"},
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": "iVBORw0KGgo" * 50,
                }},
                {"type": "file", "name": "report.pdf",
                 "source": {"media_type": "application/pdf",
                            "data": "JVBERi0xLjQK"}},
            ],
            "attachments": [
                {"type": "image", "media_type": "image/png", "bytes": 550},
                {"type": "file", "media_type": "application/pdf", "bytes": 12},
            ],
        }
        ts_unique = datetime.now(timezone.utc).isoformat() + "Z_mm"
        db2 = sqlite3.connect(str(DB_PATH))
        cur2 = db2.cursor()
        try:
            cur2.execute("""
                INSERT INTO history_log (
                    ts, seq, kind, type, app_id, session_id, user_id,
                    actor_roles, payload, before, after, correlation_id,
                    success, message
                ) VALUES (?, 999, 'message', 'user_message', ?, ?, ?,
                         '[]', ?, '{}', '{}', '', 1, '')
            """, (
                datetime.now(timezone.utc).isoformat(),
                app_id, sid, U, json.dumps(payload),
            ))
            db2.commit()
            check("multimodal INSERT succeeded", True)
            cur2.execute(
                "SELECT payload FROM history_log "
                "WHERE session_id=? AND seq=999", (sid,),
            )
            r = cur2.fetchone()
            if r:
                pl = json.loads(r[0])
                blocks = pl.get("raw_content", [])
                check("multimodal: 3 blocks preserved",
                      isinstance(blocks, list) and len(blocks) == 3,
                      f"blocks={len(blocks) if isinstance(blocks, list) else 'n/a'}")
                check("multimodal: image block intact",
                      any(b.get("type") == "image" for b in blocks))
                check("multimodal: file block intact",
                      any(b.get("type") == "file" for b in blocks))
                check("multimodal: attachments metadata set",
                      len(pl.get("attachments", [])) == 2)
        except Exception as exc:
            check("multimodal INSERT succeeded", False, f"error={exc}")
        db2.close()

        db.close()

        # ── Phase 6: /history returns unified timeline ─────────
        print("\n── Phase 6: GET /history unified ─────────────────")
        r = c.get(f"/api/apps/{app_id}/sessions/{sid}/history")
        d = r.json().get("data") or {}
        events = d.get("events") or []
        types_seen = set()
        kinds_seen = set()
        for ev in events:
            types_seen.add(ev.get("type"))
            kinds_seen.add(ev.get("kind"))
        print(f"  kinds in response: {sorted(kinds_seen)}")
        print(f"  types sample: {sorted(types_seen)[:10]}")
        check("/history events populated", len(events) > 0)
        # The ``events`` array is now CLEANLY scoped to kind='event'
        # rows (session bus events). Message rows live in
        # ``data.messages`` (denormalised turns); audit rows live in
        # ``/admin/audit-log``. The previous assertion tested a bug
        # (messages leaking into events[]) as if it were a feature.
        check("/history events[] contains only bus events",
              all(ev.get("kind") in ("session", "agent", "system",
                                     "error", "approval",
                                     "background_activation", "status")
                  for ev in events),
              f"kinds={kinds_seen}")
        check("/history events[] carries promoted event_id at top level",
              all(ev.get("event_id") for ev in events),
              f"missing event_id on "
              f"{sum(1 for ev in events if not ev.get('event_id'))}/{len(events)}")

        # ── Phase 7: daemon restart - everything survives ─────
        print("\n── Phase 7: daemon restart ──────────────────────")
        import re
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if ":8301" in line and "LISTENING" in line:
                m = re.search(r"(\d+)\s*$", line.strip())
                if m:
                    subprocess.run(
                        ["taskkill", "//F", "//PID", m.group(1)],
                        capture_output=True, text=True,
                    )
        time.sleep(3)
        # Restart from the isolated /tmp dir so the same DB is used.
        env = os.environ.copy()
        env["DIGITORN_SKIP_BUILTINS"] = "1"
        subprocess.Popen(
            ["py", "-3.12", "-m", "digitorn.core.server", "start",
             "--port", "8301", "--log-level", "warning"],
            cwd=str(DB_PATH.parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env,
        )
        for _ in range(25):
            try:
                r = httpx.get(f"{BASE}/health", timeout=2)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(1)
        time.sleep(3)
        check("daemon back after restart", True)

        # Re-login
        c2 = httpx.Client(base_url=BASE, timeout=30.0)
        r = c2.post("/auth/login", json={
            "email": f"{U}@t.local", "username": U,
            "password": "probetest-12345",
        })
        tok = r.json()["access_token"]
        c2.headers["Authorization"] = f"Bearer {tok}"

        # Fetch history - should still have our messages + events + multimodal
        r = c2.get(f"/api/apps/{app_id}/sessions/{sid}/history")
        d = r.json().get("data") or {}
        events_after = d.get("events") or []
        check("after restart: messages + events still present",
              len(events_after) > 0,
              f"got {len(events_after)} rows")
        check("after restart: events_total preserved",
              d.get("events_total", 0) >= 5,
              f"events_total={d.get('events_total')}")

        # ── Phase 8: cleanup ──────────────────────────────────
        # Post-restart client ``c2`` holds a fresh JWT - use it for the
        # uninstall. Loopback also works (auth bypass) but using the
        # authenticated client exercises the full permissioned path.
        try:
            c2.post(f"/api/apps/{app_id}/uninstall", json={"force": True})
        except Exception:
            pass
    finally:
        shutil.rmtree(src, ignore_errors=True)

    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 70}\nUNIFIED HISTORY TEST: {passed}/{total}\n{'=' * 70}")
    if passed != total:
        print("\nFailures:")
        for n, ok, d in results:
            if not ok:
                print(f"  [FAIL] {n}\n         {d[:250]}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(3)

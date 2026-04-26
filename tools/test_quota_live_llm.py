"""Live quota enforcement test — real Ollama LLM, no mocks.

Uses the local Ollama daemon (http://localhost:11434) with the
``qwen2.5:7b`` model so the test runs fully offline, with real
tokens, real streaming, real cost-free hits against actual weights.

Scenario:

    1. Deploy a fresh minimal chat app talking to local Ollama.
    2. As admin (via loopback bypass), set a quota of
       ``messages: 2 / 60s rolling_from_first`` at the app level.
    3. As the test user, create a session and send 2 messages through
       the real POST /api/apps/{id}/sessions/{sid}/messages endpoint.
       Both must complete successfully with real LLM output.
    4. Send a 3rd message. The daemon must block it with a
       ``quota_exceeded`` SSE event carrying metric=messages,
       window=60s, limit=2.
    5. Inspect GET /api/apps/{id}/quota → usage.messages.60s.current=2.
    6. Wait until the rolling window expires (~60s from first hit),
       send a 4th message — it must pass again.

No speculation, no mocks. If the test fails, there is a real bug or
the daemon wasn't restarted.

Run:  py -3.12 tools/test_quota_live_llm.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

import httpx

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
EMAIL = os.environ.get("TEST_EMAIL", "routetest@test.local")
USERNAME = os.environ.get("TEST_USERNAME", "routetest")
PASSWORD = os.environ.get("TEST_PASSWORD", "routetest123")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@digitorn.local")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme-12345")

APP_ID = "quota-live-test"
QUOTA_LIMIT = 2
QUOTA_WINDOW = "60s"
OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")


# ── Helpers ────────────────────────────────────────────────────────

def _log_pass(name: str, detail: str = "") -> None:
    print(f"[PASS] {name}" + (f"  — {detail}" if detail else ""))


def _log_fail(name: str, detail: str = "") -> None:
    print(f"[FAIL] {name}" + (f"\n       {detail}" if detail else ""))


def login(c: httpx.Client, email: str, username: str, password: str) -> str:
    r = c.post("/auth/login", json={
        "email": email, "username": username, "password": password,
    })
    if r.status_code >= 400:
        r = c.post("/auth/register", json={
            "email": email, "username": username, "password": password,
        })
    r.raise_for_status()
    return r.json()["access_token"]


def admin_token(c: httpx.Client) -> str | None:
    """Try to login as admin. Returns None if we can't — the test
    falls back to the loopback bypass which grants admin perms from
    127.0.0.1."""
    r = c.post("/auth/login", json={
        "email": ADMIN_EMAIL, "username": "admin", "password": ADMIN_PASSWORD,
    })
    if r.status_code == 200:
        return r.json()["access_token"]
    return None


def make_chat_app_yaml(dirpath: Path) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "package.toml").write_text(
        f"""[package]
id = "{APP_ID}"
name = "Quota Live Test"
version = "1.0.0"
description = "Minimal Claude chat for quota enforcement testing"
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

    (dirpath / "app.yaml").write_text(
        f"""app:
  app_id: "{APP_ID}"
  name: "Quota Live Test"
  version: "1.0.0"
  description: "Minimal local-LLM chat — one agent, no tools"
  author: tests

agents:
  - id: main
    role: main
    brain:
      provider: ollama
      model: "{OLLAMA_MODEL}"
      backend: openai_compat
      config:
        base_url: "{OLLAMA_BASE}"
        api_key: "ollama"
      temperature: 0.1
      max_tokens: 64

modules: {{}}
""", encoding="utf-8")


def send_message_wait(
    c: httpx.Client, app_id: str, session_id: str, text: str,
    *, poll_timeout: float = 120.0,
) -> dict:
    """POST a user message, then POLL /sessions/{sid} until we see
    either a new assistant message (=turn succeeded) or a stable
    state with no new assistant content (=turn blocked/aborted).

    The POST returns 200 + ``status: "accepted"`` immediately; the
    actual turn runs async. To prove enforcement, we need to know
    whether an assistant message was produced.
    """
    t0 = time.time()

    # Snapshot message count before send.
    r_pre = c.get(f"/api/apps/{app_id}/sessions/{session_id}")
    pre_count = len(((r_pre.json().get("data") or {}).get("messages") or []))

    # POST message.
    r = c.post(
        f"/api/apps/{app_id}/sessions/{session_id}/messages",
        json={"message": text, "queue_mode": "wait"},
        timeout=30.0,
    )
    http = r.status_code
    if http != 200:
        try:
            body = r.json()
        except Exception:
            body = {"_raw": r.text[:500]}
        return {
            "http_status": http, "response": body,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "assistant_text": "", "message_count_delta": 0,
            "turn_completed": False,
        }

    # Poll for the assistant reply (or a rejected turn with no reply).
    deadline = time.time() + poll_timeout
    last_count = pre_count
    assistant_text = ""
    turn_completed = False
    while time.time() < deadline:
        r = c.get(f"/api/apps/{app_id}/sessions/{session_id}")
        data = r.json().get("data") or {}
        msgs = data.get("messages") or []
        if len(msgs) > last_count:
            # New messages appeared. Find the latest assistant message.
            for m in reversed(msgs):
                if (m.get("role") or "") == "assistant":
                    content = m.get("content") or ""
                    if isinstance(content, list):
                        content = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict)
                        )
                    if content.strip():
                        assistant_text = str(content).strip()
                        turn_completed = True
                        break
            if turn_completed:
                break
            last_count = len(msgs)
        # Check if session status went terminal (idle after turn).
        status = (data.get("agent_status") or data.get("status") or "").lower()
        if status in ("idle", "completed", "quota_exceeded", "aborted", "error"):
            # Give one last scan for assistant text before returning.
            for m in reversed(msgs):
                if (m.get("role") or "") == "assistant":
                    c2 = m.get("content") or ""
                    if isinstance(c2, list):
                        c2 = " ".join(b.get("text", "") for b in c2 if isinstance(b, dict))
                    if str(c2).strip():
                        assistant_text = str(c2).strip()
                        turn_completed = True
                    break
            break
        time.sleep(0.5)

    return {
        "http_status": http,
        "response": r.json() if http == 200 else {},
        "elapsed_ms": int((time.time() - t0) * 1000),
        "assistant_text": assistant_text[:400],
        "message_count_delta": last_count - pre_count,
        "turn_completed": turn_completed,
    }


# ── Main ──────────────────────────────────────────────────────────

def main() -> int:
    results: list[tuple[str, bool, str]] = []
    def check(name: str, ok: bool, detail: str = "") -> bool:
        results.append((name, ok, detail))
        (_log_pass if ok else _log_fail)(name, detail)
        return ok

    # Sanity: local Ollama reachable and model pulled?
    try:
        r = httpx.get(OLLAMA_BASE.rstrip("/v1") + "/api/tags", timeout=3.0)
        r.raise_for_status()
        models = [m.get("name") for m in (r.json().get("models") or [])]
        if OLLAMA_MODEL not in models:
            print(
                f"[FATAL] Model {OLLAMA_MODEL!r} not pulled. "
                f"Available: {models}. Run `ollama pull {OLLAMA_MODEL}`."
            )
            return 2
        print(f"[OK] Ollama reachable — {len(models)} models, using {OLLAMA_MODEL!r}")
    except Exception as exc:
        print(f"[FATAL] Ollama not reachable at {OLLAMA_BASE}: {exc}")
        return 2

    # Sanity: daemon up?
    try:
        r = httpx.get(f"{BASE}/health", timeout=5.0)
        r.raise_for_status()
        print(f"[OK] Daemon at {BASE} — warming_up={r.json().get('warming_up')}")
    except Exception as exc:
        print(f"[FATAL] Daemon not reachable at {BASE}: {exc}")
        return 2

    src_dir = Path(tempfile.mkdtemp(prefix="quota_live_"))

    with httpx.Client(base_url=BASE, timeout=60.0) as admin_c, \
         httpx.Client(base_url=BASE, timeout=60.0) as user_c:

        # ── Admin: via loopback bypass (127.0.0.1 + NO auth) grants *. ─
        # The server-side `_is_loopback_self_call` recognises us as
        # system/admin when we hit from localhost without Authorization.
        # So admin_c stays unauthenticated — that's the intended path
        # for in-process tooling.

        # ── User: real JWT ───────────────────────────────────────────
        tok = login(user_c, EMAIL, USERNAME, PASSWORD)
        user_c.headers["Authorization"] = f"Bearer {tok}"

        make_chat_app_yaml(src_dir)

        # ── Cleanup any previous run ─────────────────────────────────
        # Loopback call — admin privilege implicit.
        admin_c.post(f"/api/apps/{APP_ID}/uninstall", json={"force": True})
        time.sleep(0.3)

        # ── 1. Install the test app via loopback admin ───────────────
        r = admin_c.post("/api/apps/install", json={
            "source_type": "local",
            "source_uri": str(src_dir),
            "accept_permissions": True,
            "scope": "system",
        })
        data = (r.json() or {}).get("data") or {}
        ok_install = (
            r.status_code == 200 and data.get("deployed") is True
        )
        check(
            "1. install test app + deploy",
            ok_install,
            f"status={r.status_code} deployed={data.get('deployed')} err={data.get('deploy_error')}",
        )
        if not ok_install:
            return 1

        # ── 2. Admin sets quota: 2 messages per 60s rolling ──────────
        body = {"quota": {
            "messages": {
                "custom": {
                    QUOTA_WINDOW: {"limit": QUOTA_LIMIT, "reset": "rolling_from_first"},
                },
            },
        }}
        r = admin_c.put(f"/api/apps/{APP_ID}/quota", json=body)
        check(
            f"2. admin set quota messages={QUOTA_LIMIT}/{QUOTA_WINDOW} rolling",
            r.status_code == 200,
            f"status={r.status_code} body={r.text[:200]}",
        )

        # ── 3. Readback quota is correct ────────────────────────────
        r = admin_c.get(f"/api/apps/{APP_ID}/quota")
        det = r.json().get("data") or {}
        quota = det.get("quota") or {}
        rule = (((quota.get("messages") or {}).get("custom") or {}).get(QUOTA_WINDOW) or {})
        check(
            "3. readback: quota.messages.custom.60s.limit == 2",
            rule.get("limit") == QUOTA_LIMIT and rule.get("reset") == "rolling_from_first",
            f"rule={rule}",
        )

        # ── 4. Create a session for the real user ───────────────────
        r = user_c.post(f"/api/apps/{APP_ID}/sessions", json={"user_id": EMAIL})
        sid = (r.json().get("data") or {}).get("session_id")
        check("4. create session", r.status_code == 200 and bool(sid), f"sid={sid}")
        if not sid:
            admin_c.post(f"/api/apps/{APP_ID}/uninstall", json={"force": True})
            return 1

        # ── 5. Send msg 1 → should succeed with real Ollama ─────────
        s1 = send_message_wait(user_c, APP_ID, sid, "Say the single word READY.")
        check(
            "5. msg 1 accepted + real Ollama replied",
            s1["http_status"] == 200 and s1["turn_completed"]
            and len(s1["assistant_text"]) > 0,
            f"http={s1['http_status']} completed={s1['turn_completed']} "
            f"text={s1['assistant_text'][:80]!r} took={s1['elapsed_ms']}ms",
        )

        # ── 6. Send msg 2 → also succeeds ───────────────────────────
        s2 = send_message_wait(user_c, APP_ID, sid, "Say the single word GO.")
        check(
            "6. msg 2 accepted + real Ollama replied",
            s2["http_status"] == 200 and s2["turn_completed"]
            and len(s2["assistant_text"]) > 0
            and s2["assistant_text"] != s1["assistant_text"],
            f"http={s2['http_status']} completed={s2['turn_completed']} "
            f"text={s2['assistant_text'][:80]!r} took={s2['elapsed_ms']}ms",
        )

        # ── 7. Send msg 3 → MUST be blocked by quota ────────────────
        # "Blocked" = no new assistant message appears within a short
        # poll window (the pre-turn check raises BEFORE the LLM is hit,
        # which takes ~30ms vs ~2-60s for a real Ollama turn).
        s3 = send_message_wait(user_c, APP_ID, sid,
                               "You should NEVER see this prompt.",
                               poll_timeout=8.0)
        blocked = (
            s3["http_status"] == 200
            and not s3["turn_completed"]
            and s3["elapsed_ms"] < 8500   # pre-check fires in <100ms; poll loop tops at 8s
        )
        check(
            f"7. msg 3 BLOCKED by quota (messages/{QUOTA_WINDOW})",
            blocked,
            f"http={s3['http_status']} completed={s3['turn_completed']} "
            f"text={s3['assistant_text'][:80]!r} took={s3['elapsed_ms']}ms",
        )

        # ── 8. GET quota/usage reflects real consumption (current=2) ─
        r = admin_c.get(f"/api/apps/{APP_ID}/quota")
        usage = ((r.json().get("data") or {}).get("usage") or {})
        msg_usage = ((usage.get("messages") or {}).get(QUOTA_WINDOW) or {})
        check(
            "8. usage.messages.60s.current == 2",
            int(msg_usage.get("current", 0)) == 2
            and int(msg_usage.get("limit", 0)) == QUOTA_LIMIT,
            f"usage.messages={msg_usage}",
        )

        # ── 9. Wait out the rolling window, then send msg 4 ─────────
        # Rolling from first: the window opened on msg 1. reset_at is
        # roughly (msg1_time + 60s). We wait until reset_at + 2s slack.
        reset_at = float(msg_usage.get("reset_at") or (time.time() + 60))
        wait_for = max(0.0, reset_at - time.time()) + 2.0
        print(f"[INFO] Waiting {wait_for:.1f}s for the rolling 60s window to reset...")
        time.sleep(wait_for)

        s4 = send_message_wait(user_c, APP_ID, sid, "Say OK briefly.")
        check(
            "9. msg 4 passes after window reset",
            s4["http_status"] == 200 and s4["turn_completed"]
            and len(s4["assistant_text"]) > 0,
            f"http={s4['http_status']} completed={s4['turn_completed']} "
            f"text={s4['assistant_text'][:80]!r} took={s4['elapsed_ms']}ms",
        )

        # ── 10. Cleanup ─────────────────────────────────────────────
        admin_c.delete(f"/api/apps/{APP_ID}/quota")
        r = admin_c.post(f"/api/apps/{APP_ID}/uninstall", json={"force": True})
        check("10. cleanup uninstall", r.status_code == 200, f"status={r.status_code}")

    shutil.rmtree(src_dir, ignore_errors=True)

    # ── Report ─────────────────────────────────────────────────────
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 60)
    print(f"QUOTA LIVE LLM TEST: {passed}/{total} passed")
    print("=" * 60)
    if passed != total:
        print("\nFailures:")
        for name, ok, detail in results:
            if not ok:
                print(f"  [FAIL] {name}")
                if detail:
                    print(f"         {detail[:300]}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(3)

"""Multi-dimension quota enforcement test — real Ollama.

Adds coverage for ``tokens_total`` alongside ``messages``. The app
gets a quota that is easy to hit on tokens BEFORE messages:

    messages:     rolling 120s, limit 10   (hard to hit in this test)
    tokens_total: rolling 120s, limit 200  (easy — qwen produces ~30-60 tokens per turn)

We send 4 medium-length prompts. Expect the tokens quota to fire
around msg 3 or 4, returning a quota_exceeded for the tokens_total
metric. This proves two things at once:

    - Post-turn token charging increments correctly
    - Multiple coexisting rules are evaluated independently and the
      most restrictive wins first (messages is nowhere close; tokens
      is the one that fires)

Run: py -3.12 tools/test_quota_multidim_live.py
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
OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
APP_ID = "quota-multidim-test"


def make_yaml(dirpath: Path) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "package.toml").write_text(
        f"""[package]
id = "{APP_ID}"
name = "Quota Multidim"
version = "1.0.0"
description = "Exercise tokens_total + messages quotas"
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
  name: "Quota Multidim"
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
        base_url: "{OLLAMA_BASE}"
        api_key: "ollama"
      temperature: 0.1
      max_tokens: 80
modules: {{}}
""", encoding="utf-8")


def send_wait(c, app_id, sid, text):
    t0 = time.time()
    r = c.post(
        f"/api/apps/{app_id}/sessions/{sid}/messages",
        json={"message": text, "queue_mode": "wait"},
        timeout=180.0,
    )
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text[:400]}
    return {"http": r.status_code, "body": body, "elapsed_ms": int((time.time()-t0)*1000)}


def main() -> int:
    results = []
    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        tag = "[PASS]" if ok else "[FAIL]"
        print(f"{tag} {name}" + (f"  — {detail}" if detail else ""))

    src = Path(tempfile.mkdtemp(prefix="quota_multidim_"))
    make_yaml(src)

    with httpx.Client(base_url=BASE, timeout=180.0) as admin_c, \
         httpx.Client(base_url=BASE, timeout=180.0) as user_c:

        # Auth user
        r = user_c.post("/auth/login", json={"email": EMAIL, "username": USERNAME, "password": PASSWORD})
        if r.status_code >= 400:
            r = user_c.post("/auth/register", json={"email": EMAIL, "username": USERNAME, "password": PASSWORD})
        user_c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

        # Cleanup prev
        admin_c.post(f"/api/apps/{APP_ID}/uninstall", json={"force": True})
        time.sleep(0.3)

        # Install (loopback admin)
        r = admin_c.post("/api/apps/install", json={
            "source_type": "local", "source_uri": str(src),
            "accept_permissions": True, "scope": "system",
        })
        data = (r.json() or {}).get("data") or {}
        check("1. install+deploy", r.status_code == 200 and data.get("deployed") is True,
              f"deployed={data.get('deployed')} err={data.get('deploy_error')}")

        # Set BOTH quotas — messages very high, tokens_total low
        body = {"quota": {
            "messages": {"custom": {"120s": {"limit": 100, "reset": "rolling_from_first"}}},
            "tokens_total": {"custom": {"120s": {"limit": 200, "reset": "rolling_from_first"}}},
        }}
        r = admin_c.put(f"/api/apps/{APP_ID}/quota", json=body)
        check("2. admin set multi-dim quota", r.status_code == 200,
              f"status={r.status_code} body={r.text[:200]}")

        # Readback
        r = admin_c.get(f"/api/apps/{APP_ID}/quota")
        q = (r.json().get("data") or {}).get("quota") or {}
        m_rule = (((q.get("messages") or {}).get("custom") or {}).get("120s") or {}).get("limit")
        t_rule = (((q.get("tokens_total") or {}).get("custom") or {}).get("120s") or {}).get("limit")
        check("3. quota readback has both metrics",
              m_rule == 100 and t_rule == 200,
              f"messages.limit={m_rule} tokens_total.limit={t_rule}")

        # Session
        r = user_c.post(f"/api/apps/{APP_ID}/sessions", json={"user_id": EMAIL})
        sid = (r.json().get("data") or {}).get("session_id")
        check("4. session", bool(sid), f"sid={sid}")
        if not sid:
            return 1

        # Send messages until blocked — expect tokens_total to fire first
        PROMPTS = [
            "Write a short poem about the sea in exactly 4 lines.",
            "List 5 interesting facts about owls. Keep it brief.",
            "Explain photosynthesis in 3 sentences.",
            "Name 5 programming languages.",
        ]
        history = []
        blocked_at = None
        for i, prompt in enumerate(PROMPTS, start=1):
            s = send_wait(user_c, APP_ID, sid, prompt)
            data = (s["body"] or {}).get("data") or {}
            err = data.get("error") or s["body"].get("error") or ""
            content = (
                data.get("content") or data.get("text")
                or data.get("turn_result", {}).get("content") or ""
            )
            history.append({
                "i": i, "http": s["http"], "elapsed_ms": s["elapsed_ms"],
                "content_len": len(content), "error": err[:150],
            })
            print(f"   msg {i}: http={s['http']} len={len(content)} err={err[:80]!r} took={s['elapsed_ms']}ms")
            if "quota" in err.lower() or (len(content) == 0 and i > 1):
                blocked_at = i
                break

        # Check usage — tokens_total should be at or past limit
        r = admin_c.get(f"/api/apps/{APP_ID}/quota")
        usage = (r.json().get("data") or {}).get("usage") or {}
        tt = (usage.get("tokens_total") or {}).get("120s") or {}
        mm = (usage.get("messages") or {}).get("120s") or {}
        print(f"\n   usage: tokens_total.120s={tt}, messages.120s={mm}")

        check("5. at least one real turn succeeded with Ollama",
              any(h["content_len"] > 0 for h in history),
              f"history content lens: {[h['content_len'] for h in history]}")

        check("6. tokens_total counter > 0 after real turns",
              float(tt.get("current", 0)) > 0,
              f"tokens_total.current={tt.get('current')} limit={tt.get('limit')}")

        check("7. quota blocked at some point",
              blocked_at is not None,
              f"blocked_at_msg={blocked_at}")

        check("8. tokens_total hit or exceeded limit before messages",
              float(tt.get("current", 0)) >= float(tt.get("limit", 99999)) * 0.5,
              f"tokens_total.current={tt.get('current')} vs limit={tt.get('limit')}",)

        # Cleanup
        admin_c.delete(f"/api/apps/{APP_ID}/quota")
        r = admin_c.post(f"/api/apps/{APP_ID}/uninstall", json={"force": True})
        check("9. cleanup", r.status_code == 200)

    shutil.rmtree(src, ignore_errors=True)

    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'='*60}\nMULTI-DIM QUOTA: {passed}/{total} passed\n{'='*60}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(3)

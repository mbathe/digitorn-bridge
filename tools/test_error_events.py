"""Live test: errors are classified + reach the client.

Triggers a deliberate failure (LLM provider unreachable) and verifies:

  1. An ``error`` event row lands in ``history_log`` with the full
     structured payload (``code``, ``category``, ``retry``, ``error``,
     ``detail``).
  2. The /history endpoint returns that event to an authenticated
     client.
  3. The payload carries the ``category`` the Flutter client uses to
     render the right UI (billing → balance banner, rate_limit → wait
     toast, auth → credential picker, …).

Run: py -3.12 tools/test_error_events.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8301")
DB_PATH = Path(r"C:\Users\ASUS\AppData\Local\Temp\uniq-ts-test\digitorn.db")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    tag = "[PASS]" if ok else "[FAIL]"
    print(f"{tag} {name}" + (f"  - {detail[:220]}" if detail else ""))


def make_yaml(d: Path, app_id: str) -> None:
    """App with a provider URL that WILL fail (port nobody listens on)."""
    d.mkdir(parents=True, exist_ok=True)
    (d / "package.toml").write_text(f"""[package]
id = "{app_id}"
name = "{app_id}"
version = "1.0.0"
description = "error classification test"
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
    # Hit the REAL Ollama with a model it doesn't have - Ollama returns
    # a 404 immediately, which should classify as provider_error. This
    # avoids the long connect-retry chain (which also fails eventually
    # but takes 60+ s and exercises timeouts instead of classification).
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
      model: "this-model-definitely-does-not-exist-v99"
      backend: openai_compat
      config:
        base_url: "http://localhost:11434/v1"
        api_key: "ollama"
      temperature: 0.0
      max_tokens: 16
modules: {{}}
""", encoding="utf-8")


def main() -> int:
    try:
        if httpx.get(f"{BASE}/health", timeout=3).status_code != 200:
            print("[FATAL] daemon not healthy")
            return 2
    except Exception as exc:
        print(f"[FATAL] daemon unreachable: {exc}")
        return 2

    app_id = f"errclf-{uuid.uuid4().hex[:6]}"
    src = Path(tempfile.mkdtemp(prefix="errclf_"))
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

        U = f"u{uuid.uuid4().hex[:6]}"
        r = raw.post("/auth/register", json={
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

        # Trigger a turn. It MUST fail because the provider URL is dead.
        print("\nSending a message that will fail (provider unreachable)…")
        c.post(
            f"/api/apps/{app_id}/sessions/{sid}/messages",
            json={"message": "Hi", "queue_mode": "async"},
            timeout=10,
        )

        # Poll /history until an error event lands (up to 60s).
        error_payload: dict | None = None
        deadline = time.time() + 60
        while time.time() < deadline:
            r = c.get(f"/api/apps/{app_id}/sessions/{sid}/history")
            d = r.json().get("data") or {}
            for ev in d.get("events") or []:
                if ev.get("type") in ("error", "credential_required"):
                    error_payload = ev
                    break
            if error_payload:
                break
            time.sleep(1)

        check(
            "error event surfaced in /history within 60s",
            error_payload is not None,
            f"payload keys={list((error_payload or {}).keys())}",
        )
        if error_payload:
            pl = error_payload.get("payload") or {}
            print(f"  event type={error_payload.get('type')}")
            print(f"  payload  ={pl}")
            check(
                "error payload has 'error' (human message)",
                bool(pl.get("error")),
                f"error={pl.get('error')}",
            )
            check(
                "error payload has 'code' (machine-readable)",
                bool(pl.get("code")),
                f"code={pl.get('code')}",
            )
            check(
                "error payload has 'category' (for Flutter UI routing)",
                bool(pl.get("category")),
                f"category={pl.get('category')}",
            )
            check(
                "error payload has 'retry' flag",
                "retry" in pl,
                f"retry={pl.get('retry')}",
            )
            # Bad-model (404) should classify as auth/provider - NOT
            # the generic 'internal' fallback bucket.
            cat = pl.get("category")
            check(
                "bad-model error classified (not the generic 'internal' bucket)",
                cat in ("network", "provider", "auth", "rate_limit", "billing"),
                f"category={cat}",
            )

        # Direct DB check: the row is in history_log with kind='event',
        # type='error', payload carrying the classification.
        db = sqlite3.connect(str(DB_PATH))
        try:
            row = db.execute(
                "SELECT type, payload FROM history_log "
                "WHERE kind='event' AND session_id=? AND type IN ('error','credential_required') "
                "ORDER BY ts DESC LIMIT 1",
                (sid,),
            ).fetchone()
        finally:
            db.close()
        check(
            "error row persisted in history_log (kind=event, type=error)",
            row is not None,
            f"row present? {bool(row)}",
        )

        # Cleanup.
        try:
            raw.post(f"/api/apps/{app_id}/uninstall", json={"force": True})
        except Exception:
            pass
    finally:
        shutil.rmtree(src, ignore_errors=True)

    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 70}\nERROR EVENTS TEST: {passed}/{total}\n{'=' * 70}")
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

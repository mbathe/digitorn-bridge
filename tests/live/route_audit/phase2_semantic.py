"""Phase 2 - semantic roundtrip tests on critical routes.

Phase 1 proved no handler crashes (250/250 routes returned non-5xx with
dummy inputs). Phase 2 goes deeper: every POST/PUT is followed by a GET
that verifies the data we sent was actually persisted, routes that
compute derived state (``/admin/stats``, ``/admin/users``, quota
enforcement) are exercised end-to-end.

Fresh daemon per run, a real admin login (the daemon's built-in
``admin/admin1234admin`` default), a real dev user registered for
scope-gating checks, real DeepSeek-backed session for turn enforcement.
STOP AT FIRST FAIL.
"""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[3]
AUDIT_DIR = Path(__file__).parent
APP_YAML = AUDIT_DIR / "apps" / "audit-conversation.yaml"


def _load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    p = ROOT / ".env"
    if not p.exists():
        return out
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# ── Test runner ───────────────────────────────────────────────────


@dataclass
class TestCase:
    name: str
    fn: Any  # callable(ctx) -> None, raises AssertionError on fail


class StopOnFail(Exception):
    pass


class Ctx:
    def __init__(self, base: str, admin_token: str, dev_token: str,
                 admin_id: str, dev_id: str, app_id: str, session_id: str,
                 correlation_id: str) -> None:
        self.base = base
        self.admin_token = admin_token
        self.dev_token = dev_token
        self.admin_id = admin_id
        self.dev_id = dev_id
        self.app_id = app_id
        self.session_id = session_id
        self.correlation_id = correlation_id
        self.client = httpx.Client(timeout=30.0)

    def hdr(self, admin: bool = False) -> dict:
        tok = self.admin_token if admin else self.dev_token
        return {"Authorization": f"Bearer {tok}"}

    def get(self, path: str, admin: bool = False, **kw) -> httpx.Response:
        return self.client.get(f"{self.base}{path}", headers=self.hdr(admin), **kw)

    def post(self, path: str, admin: bool = False, **kw) -> httpx.Response:
        return self.client.post(f"{self.base}{path}", headers=self.hdr(admin), **kw)

    def put(self, path: str, admin: bool = False, **kw) -> httpx.Response:
        return self.client.put(f"{self.base}{path}", headers=self.hdr(admin), **kw)

    def delete(self, path: str, admin: bool = False, **kw) -> httpx.Response:
        return self.client.delete(f"{self.base}{path}", headers=self.hdr(admin), **kw)


# ── Individual test cases ─────────────────────────────────────────


def test_admin_stats_shape(ctx: Ctx) -> None:
    r = ctx.get("/api/admin/stats", admin=True)
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    data = r.json().get("data", {})
    stats = data.get("stats", {})
    required = {
        "users", "apps", "packages", "system_packages",
        "credentials", "system_credentials", "mcp_servers",
        "active_sessions", "monthly_cost_usd",
    }
    missing = required - set(stats.keys())
    assert not missing, f"missing fields: {missing}; got keys: {set(stats.keys())}"
    # users >= 2 (admin + dev we just registered)
    assert stats["users"] >= 2, f"expected >=2 users, got {stats['users']}"
    # apps >= 1 (our audit app is deployed)
    assert stats["apps"] >= 1, f"expected >=1 apps, got {stats['apps']}"


def test_admin_roles_list(ctx: Ctx) -> None:
    r = ctx.get("/api/admin/roles", admin=True)
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    roles = (r.json().get("data") or {}).get("roles", [])
    names = {role["name"] for role in roles}
    assert "admin" in names, f"admin role missing; got: {names}"


def test_admin_users_list_includes_us(ctx: Ctx) -> None:
    r = ctx.get("/api/admin/users", admin=True)
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    users = (r.json().get("data") or {}).get("users", [])
    user_ids = {u.get("user_id") or u.get("id") for u in users}
    assert ctx.admin_id in user_ids, f"admin not in list; got {len(users)} users"
    assert ctx.dev_id in user_ids, f"dev user not in list"


def test_admin_audit_log_smoke(ctx: Ctx) -> None:
    # Trigger an auditable event (e.g. a user role patch). The daemon
    # records many ops as audit entries - we just need one to be there.
    r = ctx.get("/api/admin/audit-log?limit=20", admin=True)
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    entries = (r.json().get("data") or {}).get("entries", [])
    # Zero entries is acceptable on a brand-new daemon, but the shape
    # must be right. Schema check only.
    assert isinstance(entries, list), f"entries not a list: {type(entries)}"


def test_admin_quotas_roundtrip_rich(ctx: Ctx) -> None:
    # POST rich quota
    body = {
        "scope": "app",
        "app_id": ctx.app_id,
        "quota": {
            "requests": {"per_minute": {"limit": 100, "reset": "fixed"}},
            "tokens_total": {"per_day": {"limit": 50000, "reset": "fixed_daily"}},
        },
    }
    r = ctx.post("/api/admin/quotas", admin=True, json=body)
    assert r.status_code == 200, f"POST failed: {r.status_code} {r.text[:300]}"
    # GET should return the exact same definition
    r = ctx.get("/api/admin/quotas", admin=True)
    assert r.status_code == 200
    quotas = (r.json().get("data") or {}).get("quotas", [])
    our = [q for q in quotas if q.get("app_id") == ctx.app_id and q.get("scope") == "app"]
    assert len(our) == 1, f"expected 1 app quota, got {len(our)}; quotas: {quotas}"
    def_stored = our[0].get("quota", {})
    assert def_stored.get("requests", {}).get("per_minute", {}).get("limit") == 100
    assert def_stored.get("tokens_total", {}).get("per_day", {}).get("limit") == 50000


def test_admin_quotas_roundtrip_legacy(ctx: Ctx) -> None:
    body = {
        "scope_type": "user_app",
        "scope_id": ctx.dev_id,
        "app_id": ctx.app_id,
        "period": "month",
        "tokens_limit": 1000000,
    }
    r = ctx.post("/api/admin/quotas", admin=True, json=body)
    assert r.status_code == 200, f"legacy POST: {r.status_code} {r.text[:300]}"
    # Should be translated to rich format - look for user override
    r = ctx.get(f"/api/admin/quotas?app_id={ctx.app_id}&scope=user", admin=True)
    quotas = (r.json().get("data") or {}).get("quotas", [])
    our = [q for q in quotas if q.get("user_id") == ctx.dev_id]
    assert len(our) == 1, f"legacy user quota not found: {quotas}"
    d = our[0].get("quota", {})
    assert d.get("tokens_total", {}).get("per_month", {}).get("limit") == 1000000, \
        f"legacy→rich translation wrong: {d}"


def test_admin_quotas_delete(ctx: Ctx) -> None:
    # Delete the app quota we just set
    r = ctx.delete(f"/api/admin/quotas?app_id={ctx.app_id}", admin=True)
    assert r.status_code == 200, f"DELETE: {r.status_code} {r.text[:200]}"
    r = ctx.get("/api/admin/quotas", admin=True)
    quotas = (r.json().get("data") or {}).get("quotas", [])
    app_quotas = [q for q in quotas if q.get("app_id") == ctx.app_id and q.get("scope") == "app"]
    assert len(app_quotas) == 0, f"app quota still there after DELETE: {app_quotas}"


def test_admin_gate_denies_non_admin(ctx: Ctx) -> None:
    # Dev user should be REJECTED on admin routes
    for path in ("/api/admin/users", "/api/admin/quotas", "/api/admin/stats", "/api/admin/audit-log"):
        r = ctx.get(path, admin=False)
        assert r.status_code == 403, f"{path}: expected 403 for dev user, got {r.status_code}"


def test_apps_list_includes_deployed(ctx: Ctx) -> None:
    r = ctx.get("/api/apps", admin=False)
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    apps = (r.json().get("data") or [])
    ids = {a.get("app_id") for a in apps}
    assert ctx.app_id in ids, f"deployed app missing from list: {ids}"


def test_app_detail(ctx: Ctx) -> None:
    r = ctx.get(f"/api/apps/{ctx.app_id}", admin=False)
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    d = r.json().get("data", {})
    assert d.get("app_id") == ctx.app_id, f"app_id mismatch: {d}"


def test_sessions_list_includes_ours(ctx: Ctx) -> None:
    r = ctx.get(f"/api/apps/{ctx.app_id}/sessions", admin=False)
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    sessions = (r.json().get("data") or {}).get("sessions", []) \
        if isinstance(r.json().get("data"), dict) else r.json().get("data", [])
    ids = {s.get("session_id") for s in sessions if isinstance(s, dict)}
    assert ctx.session_id in ids, f"our session missing; got {len(sessions)} sessions"


def test_session_history_contains_ping(ctx: Ctx) -> None:
    r = ctx.get(
        f"/api/apps/{ctx.app_id}/sessions/{ctx.session_id}/history",
        admin=False,
    )
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    data = r.json().get("data", {})
    messages = data.get("messages", [])
    texts = [(m.get("role"), (m.get("content") or "")[:50]) for m in messages]
    has_user_ping = any(role == "user" and "PING" in c for role, c in texts)
    assert has_user_ping, f"user PING message missing from history: {texts}"


def test_quota_enforcement_live(ctx: Ctx) -> None:
    """The real end-to-end test: admin sets limit → user's 2nd message
    is blocked BEFORE the LLM is called. This is the billing-safety
    contract the whole project rests on."""
    # Clear any prior quota + counters for this app
    ctx.delete(f"/api/admin/quotas?app_id={ctx.app_id}", admin=True)
    time.sleep(0.3)
    # 1 message per 10s rolling - easy trigger
    r = ctx.post("/api/admin/quotas", admin=True, json={
        "scope": "app", "app_id": ctx.app_id,
        "quota": {"messages": {"custom": {
            "10s": {"limit": 1, "reset": "rolling_from_first"},
        }}},
    })
    assert r.status_code == 200, f"quota set failed: {r.text[:200]}"

    # Dev user sends 2 messages rapidly - 2nd must be blocked.
    # POST /messages itself replies 202 "accepted" FAST (before the LLM
    # call); the session row is created synchronously in the handler,
    # so a 10s client timeout is plenty. The LLM call that follows can
    # take longer - we don't care, we only inspect history.
    sid1 = f"pq-{uuid.uuid4().hex[:6]}"
    sid2 = f"pq-{uuid.uuid4().hex[:6]}"
    r1 = ctx.post(
        f"/api/apps/{ctx.app_id}/sessions/{sid1}/messages",
        admin=False, json={"message": "x"}, timeout=15,
    )
    assert r1.status_code in (200, 202), f"msg1 unexpected status: {r1.status_code}"
    time.sleep(0.4)
    r2 = ctx.post(
        f"/api/apps/{ctx.app_id}/sessions/{sid2}/messages",
        admin=False, json={"message": "x"}, timeout=15,
    )
    assert r2.status_code in (200, 202), f"msg2 unexpected status: {r2.status_code}"
    # Let both turns finalise (quota pre-check takes ~ms, LLM may take
    # several seconds; we wait up to 10 s for the turn-done event to
    # land in history).
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        time.sleep(0.5)
        s = ctx.get(
            f"/api/apps/{ctx.app_id}/sessions/{sid2}", admin=False,
        )
        if s.status_code == 200 and s.json().get("data", {}).get("is_active") is False:
            break

    # Inspect history of sid2 - an error event with code=quota_exceeded
    # MUST be present. This proves enforcement is live on this path.
    r = ctx.get(
        f"/api/apps/{ctx.app_id}/sessions/{sid2}/history", admin=False,
    )
    assert r.status_code == 200, f"history fetch: {r.text[:200]}"
    events = (r.json().get("data") or {}).get("events", [])
    quota_events = []
    for e in events:
        if e.get("type") in ("error", "quota_exceeded"):
            # Payload may be at top level or in ``data``/``payload``
            p = e.get("payload") or e.get("data") or {}
            if isinstance(p, dict) and p.get("code") == "quota_exceeded":
                quota_events.append(p)
    assert quota_events, (
        f"NO quota_exceeded event for session {sid2}! Events seen: "
        f"{[e.get('type') for e in events]}"
    )
    p = quota_events[0]
    # Structured fields the Flutter client keys on:
    for fld in ("code", "category", "retry_after_seconds", "limit", "current"):
        assert fld in p, f"missing field {fld!r} in quota event: {p}"
    assert p["category"] == "quota", f"category wrong: {p['category']}"
    assert p["limit"] == 1.0 and p["current"] >= 2.0


def test_credentials_list_shape(ctx: Ctx) -> None:
    r = ctx.get("/api/credentials", admin=False)
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    d = r.json().get("data")
    # Accept both {"credentials": [...]} and [...] shapes
    creds = d.get("credentials") if isinstance(d, dict) else d
    assert isinstance(creds, list), f"credentials not a list: {type(creds)}"


def test_mcp_pool_health(ctx: Ctx) -> None:
    r = ctx.get("/api/mcp/pool/health", admin=False)
    assert r.status_code == 200
    d = r.json().get("data", {})
    # Shape check - mcp_pool reports whatever it reports, but a dict is required
    assert isinstance(d, dict), f"mcp pool health not a dict: {type(d)}"


def test_packages_list(ctx: Ctx) -> None:
    r = ctx.get("/api/apps/packages", admin=False)
    assert r.status_code in (200, 404), f"{r.status_code}"


def test_health(ctx: Ctx) -> None:
    r = httpx.get(f"{ctx.base}/health", timeout=3)
    assert r.status_code == 200
    d = r.json()
    assert d.get("status") == "ok"


# ── Main ──────────────────────────────────────────────────────────


TESTS: list[TestCase] = [
    TestCase("health responds 200", test_health),
    TestCase("admin/stats has all 9 fields", test_admin_stats_shape),
    TestCase("admin/roles includes 'admin'", test_admin_roles_list),
    TestCase("admin/users includes admin + dev", test_admin_users_list_includes_us),
    TestCase("admin/audit-log returns list", test_admin_audit_log_smoke),
    TestCase("admin/quotas POST-rich → GET roundtrip", test_admin_quotas_roundtrip_rich),
    TestCase("admin/quotas POST-legacy → GET roundtrip", test_admin_quotas_roundtrip_legacy),
    TestCase("admin/quotas DELETE removes the row", test_admin_quotas_delete),
    TestCase("admin routes 403 for non-admin", test_admin_gate_denies_non_admin),
    TestCase("apps list includes deployed", test_apps_list_includes_deployed),
    TestCase("app detail by id", test_app_detail),
    TestCase("sessions list includes ours", test_sessions_list_includes_ours),
    TestCase("session history contains PING", test_session_history_contains_ping),
    TestCase("quota ENFORCED live end-to-end", test_quota_enforcement_live),
    TestCase("credentials list shape", test_credentials_list_shape),
    TestCase("mcp/pool/health responds", test_mcp_pool_health),
    TestCase("packages list", test_packages_list),
]


def main() -> int:
    env_vars = _load_env()
    if not env_vars.get("DEEPSEEK_API_KEY"):
        print("FAIL: DEEPSEEK_API_KEY missing from .env", file=sys.stderr)
        return 1

    port = 8287
    data_dir = tempfile.mkdtemp(prefix="dg-phase2-")
    env = dict(os.environ)
    env["DIGITORN_HOME"] = data_dir
    env["DIGITORN_DISCOVERY__SKIP_EMBEDDINGS"] = "1"
    env["DEEPSEEK_API_KEY"] = env_vars["DEEPSEEK_API_KEY"]

    base = f"http://127.0.0.1:{port}"
    print(f"[boot] data_dir={data_dir} port={port}")
    log_path = Path(tempfile.gettempdir()) / f"dg-phase2-{port}.log"
    proc = subprocess.Popen(
        [sys.executable, "-m", "digitorn.core.server", "start",
         "--port", str(port), "--no-sandbox"],
        env=env, cwd=str(ROOT),
        stdout=open(log_path, "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    print(f"[boot] daemon log → {log_path}")
    try:
        # Wait for ready
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base}/health", timeout=3).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            print("FAIL: daemon not ready in 180s", file=sys.stderr)
            return 1
        print(f"[boot] ready at {base}")

        with httpx.Client(timeout=30.0) as c:
            # Admin login (built-in admin from _ensure_default_admin)
            r = c.post(f"{base}/auth/login", json={
                "username": "admin", "password": "admin1234admin",
            }, timeout=20)
            assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text[:200]}"
            admin_tok = r.json()["access_token"]
            admin_id = r.json().get("user_id") or "admin"
            print(f"[auth] admin logged in: {admin_id}")

            # Dev user (fresh registration - becomes 'developer' role)
            dev_uname = f"dev{uuid.uuid4().hex[:8]}"
            dev_email = f"{dev_uname}@test.local"
            r = c.post(f"{base}/auth/register", json={
                "username": dev_uname, "email": dev_email,
                "password": "DevPass1234!test",
            }, timeout=20)
            assert r.status_code == 200, f"register: {r.text[:200]}"
            dev_tok = r.json()["access_token"]
            dev_id = r.json().get("user_id") or dev_uname
            print(f"[auth] dev user registered: {dev_id}")

            # Deploy app (with dev token, so it's scope=user owned by dev)
            yaml_path = str(APP_YAML.resolve())
            r = c.post(
                f"{base}/api/apps/deploy",
                headers={"Authorization": f"Bearer {dev_tok}"},
                json={"yaml_path": yaml_path, "force": True},
                timeout=30,
            )
            assert r.status_code == 200 and r.json().get("success"), \
                f"deploy: {r.status_code} {r.text[:300]}"
            app_id = (r.json().get("data") or {}).get("app_id")
            print(f"[fix] app deployed: {app_id}")

            # Wait deploy complete
            for _ in range(60):
                s = c.get(
                    f"{base}/api/apps/{app_id}/deploy-status",
                    headers={"Authorization": f"Bearer {dev_tok}"}, timeout=10,
                )
                if (s.json().get("data") or {}).get("deployed"):
                    break
                time.sleep(1)
            # Wait warm
            for _ in range(30):
                if not c.get(f"{base}/health").json().get("warming_up"):
                    break
                time.sleep(1)

            # Create a real session (PING/PONG)
            sid = f"audit-{uuid.uuid4().hex[:8]}"
            r = c.post(
                f"{base}/api/apps/{app_id}/sessions/{sid}/messages",
                headers={"Authorization": f"Bearer {dev_tok}"},
                json={"message": "PING"}, timeout=30,
            )
            assert r.status_code in (200, 202), f"POST /messages: {r.text[:200]}"
            cid = (r.json().get("data") or {}).get("correlation_id") or ""
            # Wait for turn to finish (so history contains message_done)
            for _ in range(60):
                sd = c.get(
                    f"{base}/api/apps/{app_id}/sessions/{sid}",
                    headers={"Authorization": f"Bearer {dev_tok}"}, timeout=10,
                ).json().get("data", {})
                if sd.get("is_active") is False:
                    break
                time.sleep(1)
            print(f"[fix] session alive: {sid}")

            ctx = Ctx(
                base=base, admin_token=admin_tok, dev_token=dev_tok,
                admin_id=admin_id, dev_id=dev_id,
                app_id=app_id, session_id=sid, correlation_id=cid,
            )

            # Run the tests
            passed = 0
            for i, tc in enumerate(TESTS, 1):
                try:
                    tc.fn(ctx)
                    print(f"[{i:>2}/{len(TESTS)}] ✓ {tc.name}")
                    passed += 1
                except AssertionError as exc:
                    print(f"[{i:>2}/{len(TESTS)}] ✗ {tc.name}")
                    print(f"\n── STOP: first failure ──")
                    print(f"  test: {tc.name}")
                    print(f"  assertion: {exc}")
                    return 2
                except Exception as exc:
                    print(f"[{i:>2}/{len(TESTS)}] ✗ {tc.name} (EXCEPTION)")
                    print(f"\n── STOP: unexpected exception ──")
                    import traceback
                    traceback.print_exc()
                    return 3

            print(f"\n=> {passed}/{len(TESTS)} semantic tests passed")
            return 0
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try: proc.kill()
            except Exception: pass


if __name__ == "__main__":
    sys.exit(main())

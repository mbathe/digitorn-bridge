"""Unit test for ``GET /api/admin/stats`` (admin Overview dashboard).

Exercises ``admin_get_stats`` without spinning up the full FastAPI app:
a fake request, a fake session factory that returns canned counts, and
we check every field of the response + the cache behaviour + fail-soft
path when the DB or MCP pool misbehaves.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from fastapi import HTTPException  # noqa: E402

from digitorn.core.api.user import admin_get_stats  # noqa: E402
from digitorn.core.api import user as user_module  # noqa: E402


def _fake_request(perms: list[str], mcp_pool: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(permissions=perms),
        app=SimpleNamespace(state=SimpleNamespace(mcp_pool=mcp_pool)),
    )


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeDb:
    def __init__(self, counts: list[int | float]):
        # Pop in FIFO order. One pop per execute() call.
        self._counts = list(counts)

    async def execute(self, stmt):
        if not self._counts:
            return _FakeResult(0)
        return _FakeResult(self._counts.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_session_factory(counts: list[int | float]) -> None:
    def factory():
        return _FakeDb(counts)

    def _get_session_factory():
        return factory

    # We monkey-patch the module-level import done inside admin_get_stats.
    import digitorn.core.database as _db
    _db.get_session_factory = _get_session_factory
    # Flush cache
    user_module._admin_stats_cache["ts"] = 0.0
    user_module._admin_stats_cache["data"] = None


async def run() -> int:
    failures: list[str] = []

    # ── 1. Non-admin → 403 ────────────────────────────────────────
    try:
        await admin_get_stats(_fake_request(perms=["read:self"]))
    except HTTPException as exc:
        if exc.status_code != 403:
            failures.append(f"non-admin: expected 403 got {exc.status_code}")
    else:
        failures.append("non-admin: should raise 403")

    # ── 2. Admin with 9 known counts → matches schema ─────────────
    counts = [
        42,     # User.count
        12,     # Application.count
        8,      # InstalledPackage scope=user
        3,      # InstalledPackage scope=system
        15,     # Credential owner_type=user
        4,      # Credential owner_type=system
        23,     # UserSession last_active_at >= cutoff
        1234.5678,  # SUM(cost_usd)
    ]
    _patch_session_factory(counts)

    # Fake MCP pool reporting 5 connected servers
    mcp_pool = SimpleNamespace(list_connected=lambda: ["srv1", "srv2", "srv3", "srv4", "srv5"])
    req = _fake_request(perms=["admin"], mcp_pool=mcp_pool)

    resp = await admin_get_stats(req)
    body = resp.data
    if not isinstance(body, dict) or "stats" not in body:
        failures.append(f"response: missing 'stats' key, got {list(body or {})}")
    else:
        stats = body["stats"]
        expected = {
            "users": 42,
            "apps": 12,
            "packages": 8,
            "system_packages": 3,
            "credentials": 15,
            "system_credentials": 4,
            "active_sessions": 23,
            "monthly_cost_usd": 1234.5678,
            "mcp_servers": 5,
        }
        for k, v in expected.items():
            if stats.get(k) != v:
                failures.append(
                    f"field {k!r}: expected {v}, got {stats.get(k)!r}"
                )

    # ── 3. Second call within TTL → cache hit (no extra DB queries) ──
    # Reset counts to [] so a cache miss would crash (all 0).
    # Keep cache populated from call #2.
    import digitorn.core.database as _db
    def _empty_inner():
        return _FakeDb([])  # empty → all scalars default to 0
    def _empty_factory():
        return _empty_inner
    _db.get_session_factory = _empty_factory
    resp2 = await admin_get_stats(req)
    if resp2.data["stats"].get("users") != 42:
        failures.append(
            f"cache: expected cached users=42, got {resp2.data['stats'].get('users')}"
        )

    # ── 4. Cache expiry (TTL=0 forces refresh) → picks up new factory ──
    user_module._admin_stats_cache["ts"] = 0.0  # expire
    user_module._admin_stats_cache["data"] = None
    # Now empty factory is used — every field = 0
    resp3 = await admin_get_stats(req)
    if resp3.data["stats"].get("users") != 0:
        failures.append(
            f"cache expiry: expected refreshed users=0, got {resp3.data['stats'].get('users')}"
        )

    # ── 5. Fail-soft: MCP pool raises → mcp_servers = 0, no crash ──
    _patch_session_factory(counts)  # refill
    broken_pool = SimpleNamespace(list_connected=lambda: (_ for _ in ()).throw(RuntimeError("pool down")))
    req2 = _fake_request(perms=["admin"], mcp_pool=broken_pool)
    try:
        resp4 = await admin_get_stats(req2)
        if resp4.data["stats"].get("mcp_servers") != 0:
            failures.append(
                f"fail-soft mcp: expected 0 got {resp4.data['stats'].get('mcp_servers')}"
            )
    except Exception as exc:
        failures.append(f"fail-soft mcp: crashed instead of returning 0: {exc}")

    # ── 6. Fail-soft: no session factory (DB not initialised) ──────
    user_module._admin_stats_cache["ts"] = 0.0
    user_module._admin_stats_cache["data"] = None
    def _get_null_factory():
        return None
    _db.get_session_factory = _get_null_factory
    req3 = _fake_request(perms=["admin"], mcp_pool=None)
    try:
        resp5 = await admin_get_stats(req3)
        stats5 = resp5.data["stats"]
        if any(stats5.get(k, -1) != 0 for k in (
            "users", "apps", "packages", "system_packages",
            "credentials", "system_credentials",
            "active_sessions", "mcp_servers",
        )):
            failures.append(
                f"fail-soft no-db: expected all 0, got {stats5}"
            )
        if stats5.get("monthly_cost_usd") != 0.0:
            failures.append(
                f"fail-soft no-db: monthly_cost_usd expected 0.0, got {stats5.get('monthly_cost_usd')}"
            )
    except Exception as exc:
        failures.append(f"fail-soft no-db: crashed: {exc}")

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: admin stats endpoint — auth, schema, cache, fail-soft all green")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))

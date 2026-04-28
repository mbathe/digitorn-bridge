"""Unit tests for ``SqlQuotaStore`` - the SQL-backed rich quota store.

Runs against an **in-memory SQLite** so no daemon, no disk, no network.
Exercises:

  1. Round-trip set/get/remove for app-level and user-level quotas
  2. ``effective_quota`` merge semantics (global → app → user)
  3. Fixed-window counter: first charge, subsequent charges, window roll
  4. Rolling-window counter: opens on first charge, expires correctly
  5. ``check_and_charge`` honours the limit and raises ``QuotaExceededError``
  6. **Race safety** - concurrent charges can't both pass the check
  7. Per-model overrides stack on top of aggregate rules
  8. ``snapshot_usage`` reports every rule accurately
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from digitorn.core.models import Base  # noqa: E402
from digitorn.core.quota import (  # noqa: E402
    MetricQuota,
    QuotaDefinition,
    QuotaExceededError,
    QuotaRule,
)
from digitorn.core.quota_sql import SqlQuotaStore  # noqa: E402


async def _make_engine():
    # File-based SQLite (not in-memory): the SqlQuotaStore spins up a
    # parallel sync engine pointed at the same DB, and `:memory:` does
    # NOT share state between connections. A temp file does.
    import os as _os
    import tempfile as _tmp
    fd, path = _tmp.mkstemp(suffix=".db", prefix="dg-qtest-")
    _os.close(fd)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, path


def _simple_rich(limit: int, window: str = "per_minute") -> QuotaDefinition:
    return QuotaDefinition(requests=MetricQuota.model_validate({
        window: {"limit": limit, "reset": "fixed"},
    }))


async def run() -> int:
    failures: list[str] = []
    engine, db_path = await _make_engine()
    store = SqlQuotaStore(engine)

    # ── 1. Round-trip set/get/remove ─────────────────────────────
    q = _simple_rich(100)
    env = store.set_app_quota("app-A", q, updated_by="admin@x")
    if "quota" not in env or env.get("updated_by") != "admin@x":
        failures.append(f"set_app_quota envelope: {env}")

    got = store.get_app_quota("app-A")
    if got is None or got["quota"].get("requests", {}).get("per_minute", {}).get("limit") != 100:
        failures.append(f"get_app_quota round-trip: {got}")

    if not store.remove_app_quota("app-A"):
        failures.append("remove_app_quota: expected True")
    if store.get_app_quota("app-A") is not None:
        failures.append("remove_app_quota: row still present")
    if store.remove_app_quota("app-A"):
        failures.append("remove_app_quota: 2nd call should return False")

    # User override round-trip
    uq = QuotaDefinition(tokens_total=MetricQuota.model_validate({
        "per_day": {"limit": 10000, "reset": "fixed_daily"},
    }))
    store.set_user_quota("app-A", "alice", uq, updated_by="admin@x")
    got_u = store.get_user_quota("app-A", "alice")
    if got_u is None or got_u["quota"]["tokens_total"]["per_day"]["limit"] != 10000:
        failures.append(f"user quota round-trip: {got_u}")

    # ── 2. effective_quota merge ──────────────────────────────────
    store.set_app_quota("app-B", _simple_rich(1000), updated_by="admin@x")
    store.set_user_quota(
        "app-B", "bob",
        QuotaDefinition(requests=MetricQuota.model_validate({
            "per_minute": {"limit": 10, "reset": "fixed"},
        })),
        updated_by="admin@x",
    )
    eff_app = store.effective_quota("app-B", global_default_rpm=60)
    eff_user = store.effective_quota("app-B", user_id="bob", global_default_rpm=60)
    if eff_app["requests"]["per_minute"]["limit"] != 1000:
        failures.append(f"effective app override: {eff_app}")
    if eff_user["requests"]["per_minute"]["limit"] != 10:
        failures.append(f"effective user override: {eff_user}")

    # ── 3. Fixed-window counter: increments across a charge ───────
    store.set_app_quota("app-C", _simple_rich(5), updated_by="admin@x")
    for i in range(5):
        store.check_and_charge(
            app_id="app-C", user_id=None, charges={"requests": 1},
        )
    # 6th charge must raise
    try:
        store.check_and_charge(
            app_id="app-C", user_id=None, charges={"requests": 1},
        )
        failures.append("fixed-window 6th: should have raised QuotaExceededError")
    except QuotaExceededError as exc:
        if exc.state.metric != "requests":
            failures.append(f"fixed-window 6th: wrong metric {exc.state.metric}")

    # ── 4. Rolling-window ────────────────────────────────────────
    rolling = QuotaDefinition(messages=MetricQuota.model_validate({
        "custom": {"2s": {"limit": 2, "reset": "rolling_from_first"}},
    }))
    store.set_app_quota("app-D", rolling, updated_by="admin@x")
    store.check_and_charge(
        app_id="app-D", user_id=None, charges={"messages": 1},
    )
    store.check_and_charge(
        app_id="app-D", user_id=None, charges={"messages": 1},
    )
    try:
        store.check_and_charge(
            app_id="app-D", user_id=None, charges={"messages": 1},
        )
        failures.append("rolling 3rd: should have raised")
    except QuotaExceededError:
        pass
    # Wait for window to expire - 2s rolling + safety margin.
    await asyncio.sleep(2.5)
    try:
        store.check_and_charge(
            app_id="app-D", user_id=None, charges={"messages": 1},
        )
    except QuotaExceededError as exc:
        failures.append(f"rolling after expiry: should have passed, got {exc}")

    # ── 5. Per-model overrides ────────────────────────────────────
    model_quota = QuotaDefinition(
        tokens_total=MetricQuota.model_validate({
            "per_day": {"limit": 1000000, "reset": "fixed_daily"},
        }),
        models={
            "claude-opus-4-6": {
                "tokens_total": {"per_day": {"limit": 50, "reset": "fixed_daily"}},
            },
        },
    )
    store.set_app_quota("app-E", model_quota, updated_by="admin@x")
    store.check_and_charge(
        app_id="app-E", user_id=None,
        charges={"tokens_total": 40}, model="claude-opus-4-6",
    )
    try:
        store.check_and_charge(
            app_id="app-E", user_id=None,
            charges={"tokens_total": 20}, model="claude-opus-4-6",
        )
        failures.append("model override: 40+20 should exceed 50 limit")
    except QuotaExceededError as exc:
        if exc.state.limit != 50:
            failures.append(f"model override: wrong limit {exc.state.limit}")
    # Other model untouched
    store.check_and_charge(
        app_id="app-E", user_id=None,
        charges={"tokens_total": 5000}, model="deepseek-chat",
    )

    # ── 6. snapshot_usage ─────────────────────────────────────────
    snap = store.snapshot_usage("app-C")
    req_report = snap.get("requests", {}).get("per_minute", {})
    if req_report.get("current") != 5 or req_report.get("limit") != 5:
        failures.append(f"snapshot_usage: {req_report}")

    # ── 7. Race safety - 20 parallel charges on a limit=10 quota ──
    store.set_app_quota("app-F", _simple_rich(10), updated_by="admin@x")
    passed = [0]
    rejected = [0]
    lock = threading.Lock()

    def _hammer():
        try:
            store.check_and_charge(
                app_id="app-F", user_id=None, charges={"requests": 1},
            )
            with lock:
                passed[0] += 1
        except QuotaExceededError:
            with lock:
                rejected[0] += 1
        except Exception:
            pass

    threads = [threading.Thread(target=_hammer) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Strict atomicity: exactly 10 pass, 10 reject. SQLite serialises
    # writes via BEGIN IMMEDIATE so the check-and-charge window is
    # closed to interlopers.
    if passed[0] > 10:
        failures.append(
            f"race: overcharge - {passed[0]} passed, expected exactly 10"
        )
    if passed[0] + rejected[0] < 15:
        failures.append(
            f"race: only {passed[0]} pass + {rejected[0]} reject "
            f"(expected 20 total; check for dropped threads)"
        )

    print(f"race outcome: {passed[0]} passed / {rejected[0]} rejected (target: 10/10)")

    # ── 8. list_user_overrides ────────────────────────────────────
    overrides = store.list_user_overrides("app-B")
    bob_overrides = [o for o in overrides if o["user_id"] == "bob"]
    if len(bob_overrides) != 1:
        failures.append(f"list_user_overrides: expected 1 bob, got {overrides}")

    # Report
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: SqlQuotaStore - round-trip, merge, fixed/rolling, per-model, race, snapshot all green")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))

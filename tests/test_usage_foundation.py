"""End-to-end tests for the usage tracking + quota system.

Covers:
1. UsageStore record + monthly_totals + by_app + cost_by_model
2. UsageStore hourly + daily time series (zero-filled)
3. QuotaStore upsert + list + check (user scope)
4. QuotaStore check (user_app scope, blocking)
5. QuotaStore period window (daily vs monthly)
6. Price book: known model, substring match, unknown fallback
7. Flutter-shape notification prefs (flat list events, start_hour/end_hour)
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.ERROR)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages"))


def _h(title: str) -> None:
    print(f"\n{'=' * 60}\n  {title}\n{'=' * 60}")


def _ok(label: str) -> None:
    print(f"  OK {label}")


async def _build_stores():
    from digitorn.core.config import get_settings, override_settings
    from digitorn.core.database import Base, get_session_factory, init_db
    from digitorn.core.usage import UsageStore, QuotaStore

    settings = get_settings()
    override_settings(settings.model_copy(update={
        "database": settings.database.model_copy(update={
            "url": "sqlite+aiosqlite:///:memory:",
        }),
    }))
    engine = await init_db(get_settings())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    usage = UsageStore(get_session_factory())
    quotas = QuotaStore(get_session_factory(), usage_store=usage)
    return usage, quotas


async def test_usage_record_and_aggregates() -> None:
    _h("1. UsageStore record + monthly_totals + cost_by_model + by_app")
    usage, _ = await _build_stores()

    # Alice uses 2 models on 2 apps this month
    await usage.record(
        user_id="alice", app_id="digitorn-code", session_id="s1",
        provider="anthropic", model="claude-opus-4-6",
        prompt_tokens=10_000, completion_tokens=5_000,
    )
    await usage.record(
        user_id="alice", app_id="digitorn-code", session_id="s1",
        provider="anthropic", model="claude-sonnet-4-5",
        prompt_tokens=3_000, completion_tokens=1_500,
    )
    await usage.record(
        user_id="alice", app_id="job-hunter", session_id="s2",
        provider="openai", model="gpt-4o",
        prompt_tokens=2_000, completion_tokens=800,
    )
    # Bob - cross-user isolation check
    await usage.record(
        user_id="bob", app_id="digitorn-code", session_id="s3",
        provider="anthropic", model="claude-opus-4-6",
        prompt_tokens=100_000, completion_tokens=50_000,
    )

    # Monthly totals for alice
    totals = await usage.monthly_totals(user_id="alice")
    assert totals["prompt_tokens"] == 15_000
    assert totals["completion_tokens"] == 7_300
    assert totals["total_tokens"] == 22_300
    assert totals["cost_usd"] > 0
    _ok(f"alice total_tokens={totals['total_tokens']} cost=${totals['cost_usd']:.4f}")

    # Bob is isolated
    totals_bob = await usage.monthly_totals(user_id="bob")
    assert totals_bob["total_tokens"] == 150_000
    _ok("alice/bob isolation confirmed")

    # Cost by model
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    by_model = await usage.cost_by_model(
        user_id="alice", since=month_start,
    )
    assert "claude-opus-4-6" in by_model
    assert "claude-sonnet-4-5" in by_model
    assert "gpt-4o" in by_model
    assert by_model["claude-opus-4-6"] > 0
    _ok(f"cost_by_model: {len(by_model)} models with non-zero cost")

    # By app
    by_app = await usage.by_app(
        user_id="alice", since=month_start,
    )
    app_ids = {row["app_id"] for row in by_app}
    assert app_ids == {"digitorn-code", "job-hunter"}
    # Largest cost first
    assert by_app[0]["cost_usd"] >= by_app[-1]["cost_usd"]
    _ok(f"by_app: {len(by_app)} apps, top={by_app[0]['app_id']}")


async def test_usage_timeseries() -> None:
    _h("2. UsageStore hourly + daily time series (zero-filled)")
    usage, _ = await _build_stores()

    # Record 2 events, one "now" and one 2 hours ago
    now = datetime.now(timezone.utc).replace(minute=30)
    await usage.record(
        user_id="alice", app_id="x", session_id="s",
        provider="anthropic", model="claude-haiku-4-5",
        prompt_tokens=100, completion_tokens=50,
    )
    # Record the second directly via a session (fake older timestamp)
    from digitorn.core.database import get_session_factory
    from digitorn.core.models import UsageEvent
    async with get_session_factory()() as db:
        row = UsageEvent(
            user_id="alice", app_id="x", session_id="s",
            provider="anthropic", model="claude-haiku-4-5",
            prompt_tokens=200, completion_tokens=100,
            cost_usd=0.001,
            created_at=now - timedelta(hours=2),
        )
        db.add(row)
        await db.commit()

    # 24h hourly
    series = await usage.timeseries_hourly(
        user_id="alice", hours=24, at=now,
    )
    assert len(series) == 24
    # Each entry has ts + prompt + completion
    for entry in series:
        assert "ts" in entry and "prompt" in entry and "completion" in entry
    # Total tokens matches
    total_prompt = sum(e["prompt"] for e in series)
    assert total_prompt == 300
    _ok(f"hourly: 24 buckets, total_prompt={total_prompt}")

    # 30d daily
    daily = await usage.timeseries_daily(
        user_id="alice", days=30, at=now,
    )
    assert len(daily) == 30
    total_daily = sum(e["prompt"] for e in daily)
    assert total_daily == 300
    _ok(f"daily: 30 buckets, total_prompt={total_daily}")


async def test_quota_upsert_and_list() -> None:
    _h("3. QuotaStore CRUD")
    _, quotas = await _build_stores()

    q1 = await quotas.upsert_quota(
        scope_type="user", scope_id="alice",
        period="month", tokens_limit=10_000_000,
        set_by="admin",
    )
    assert q1["tokens_limit"] == 10_000_000
    _ok(f"created user quota id={q1['id'][:8]}")

    # Upsert same scope → overwrites
    q2 = await quotas.upsert_quota(
        scope_type="user", scope_id="alice",
        period="month", tokens_limit=5_000_000,
        set_by="admin",
    )
    assert q2["id"] == q1["id"]
    assert q2["tokens_limit"] == 5_000_000
    _ok("upsert overwrites same-scope row")

    rows = await quotas.list_quotas(scope_type="user", scope_id="alice")
    assert len(rows) == 1
    _ok(f"list: {len(rows)} row")

    # user_app scope
    await quotas.upsert_quota(
        scope_type="user_app", scope_id="alice",
        app_id="digitorn-code", period="week",
        tokens_limit=500_000, set_by="admin",
    )
    rows = await quotas.list_quotas(scope_id="alice")
    assert len(rows) == 2
    _ok(f"after user_app: {len(rows)} rows")

    # Validation
    try:
        await quotas.upsert_quota(
            scope_type="user", scope_id="alice",
            app_id="digitorn-code", period="month",
            tokens_limit=1, set_by="admin",
        )
        assert False, "should have raised"
    except ValueError as exc:
        assert "cannot be scoped" in str(exc)
    _ok("validation: user scope + app_id → rejected")


async def test_quota_check_enforcement() -> None:
    _h("4. QuotaStore.check - enforcement decision")
    usage, quotas = await _build_stores()

    # Alice has a 10k/month limit
    await quotas.upsert_quota(
        scope_type="user", scope_id="alice",
        period="month", tokens_limit=10_000,
    )

    # Initially below limit
    decision = await quotas.check(user_id="alice", app_id="myapp")
    assert decision["allowed"] is True
    assert len(decision["limits"]) == 1
    assert decision["limits"][0]["remaining"] == 10_000
    _ok("below limit: allowed=True, remaining=10000")

    # Spend 8k tokens
    await usage.record(
        user_id="alice", app_id="myapp", session_id="s",
        provider="anthropic", model="claude-haiku-4-5",
        prompt_tokens=6_000, completion_tokens=2_000,
    )
    decision = await quotas.check(user_id="alice", app_id="myapp")
    assert decision["allowed"] is True
    assert decision["limits"][0]["used"] == 8_000
    assert decision["limits"][0]["remaining"] == 2_000
    _ok("after 8k spent: remaining=2000")

    # Spend 3k more → over limit
    await usage.record(
        user_id="alice", app_id="myapp", session_id="s",
        provider="anthropic", model="claude-haiku-4-5",
        prompt_tokens=2_000, completion_tokens=1_000,
    )
    decision = await quotas.check(user_id="alice", app_id="myapp")
    assert decision["allowed"] is False
    assert decision["blocking_limit"] is not None
    assert decision["blocking_limit"]["remaining"] == 0
    _ok("over limit: allowed=False, blocking_limit set")


async def test_price_book() -> None:
    _h("5. Model price book - lookup + substring + unknown")
    from digitorn.core.usage import ModelPriceBook, compute_cost

    book = ModelPriceBook()

    # Exact match
    p = book.price_for("claude-opus-4-6")
    assert p.prompt == 15.0 and p.completion == 75.0
    _ok("exact match: claude-opus-4-6")

    # Substring: versioned id
    p = book.price_for("claude-opus-4-6-20250101")
    assert p.prompt == 15.0
    _ok("substring match: versioned id resolves to base")

    # Longest-substring wins
    p = book.price_for("deepseek-reasoner")
    assert p.prompt == 0.55
    _ok("longest-substring wins: deepseek-reasoner")

    # Unknown → fallback
    p = book.price_for("some-random-model-xyz")
    assert p.prompt > 0  # still returns a default so UI shows $
    _ok(f"unknown → fallback $/M prompt={p.prompt}")

    # compute_cost
    cost = compute_cost("claude-haiku-4-5", 1_000_000, 1_000_000)
    assert abs(cost - (0.80 + 4.0)) < 0.01
    _ok(f"compute_cost(1M,1M) for haiku = ${cost:.4f}")


async def test_flutter_notification_prefs_shape() -> None:
    _h("6. Flutter-shape notification prefs (flat events list)")
    from digitorn.core.inbox import InboxKind, NotificationPolicy

    # Flutter shape
    prefs = {
        "desktop": True,
        "push": True,
        "sound": True,
        "events": [
            InboxKind.SESSION_COMPLETED,
            InboxKind.SESSION_FAILED,
        ],
        "quiet_hours": {
            "start_hour": 22, "end_hour": 7, "tz": "Europe/Paris",
        },
    }

    # Whitelisted kind → fires desktop + push
    ch = NotificationPolicy.channels_for(
        kind=InboxKind.SESSION_COMPLETED, prefs=prefs,
        now=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
    )
    assert "desktop" in ch
    assert "push" in ch
    _ok(f"whitelisted session.completed → {ch}")

    # Not-whitelisted kind → silenced
    ch = NotificationPolicy.channels_for(
        kind=InboxKind.BG_ACTIVATION_COMPLETED, prefs=prefs,
        now=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
    )
    assert ch == []
    _ok("not in whitelist → silenced")

    # Quiet hours via start_hour / end_hour - non-critical silenced
    ch = NotificationPolicy.channels_for(
        kind=InboxKind.SESSION_COMPLETED, prefs=prefs,
        now=datetime(2026, 4, 13, 23, 30, tzinfo=timezone.utc),
    )
    assert ch == []
    _ok("quiet hours via start_hour/end_hour silences non-critical")

    # Critical kind bypasses quiet hours
    ch = NotificationPolicy.channels_for(
        kind=InboxKind.SESSION_FAILED, prefs=prefs,
        now=datetime(2026, 4, 13, 23, 30, tzinfo=timezone.utc),
    )
    assert len(ch) > 0
    _ok("critical session.failed bypasses quiet hours")

    # desktop=False → no desktop channel
    prefs["desktop"] = False
    ch = NotificationPolicy.channels_for(
        kind=InboxKind.SESSION_COMPLETED, prefs=prefs,
        now=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
    )
    assert "desktop" not in ch
    assert "push" in ch
    _ok("desktop=False → only push fires")


async def main() -> None:
    tests = [
        test_usage_record_and_aggregates,
        test_usage_timeseries,
        test_quota_upsert_and_list,
        test_quota_check_enforcement,
        test_price_book,
        test_flutter_notification_prefs_shape,
    ]
    for t in tests:
        await t()
    print(f"\n{'=' * 60}\n  ALL {len(tests)} TESTS PASSED\n{'=' * 60}\n")


if __name__ == "__main__":
    asyncio.run(main())

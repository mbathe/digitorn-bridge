"""Tests for cron_native._parse_when + _validate_action_name.

Covers the bounds/safety rules introduced for:
- BUG-CRON-01: cron field count must be exactly 5 (rejects 6/7 → per-second DoS)
- BUG-CRON-02: min delay 1s (rejects 'in 0s', 'in 0m', ...)
- BUG-CRON-03: max delay ~10 years
- BUG-CRON-04: ISO timestamp in the past is rejected
- BUG-CRON-05: relative unit is case-sensitive (no uppercase M=months confusion)
- BUG-CRON-09: action format validation (both halves non-empty)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

croniter = pytest.importorskip("croniter")

from digitorn.modules.cron_native.module import (
    _MAX_DELAY_SECONDS,
    _MIN_DELAY_SECONDS,
    _parse_when,
    _validate_action_name,
)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)


# ── _parse_when — cron field count ────────────────────────────────


def test_cron_5_fields_ok(now: datetime) -> None:
    kind, next_run, expr = _parse_when("0 9 * * *", now)
    assert kind == "cron"
    assert expr == "0 9 * * *"
    assert next_run is not None


def test_cron_6_fields_rejected(now: datetime) -> None:
    """6 fields (seconds) would enable per-second DoS. Must be rejected."""
    with pytest.raises(ValueError, match="exactly 5 fields"):
        _parse_when("* * * * * *", now)


def test_cron_7_fields_rejected(now: datetime) -> None:
    with pytest.raises(ValueError, match="exactly 5 fields"):
        _parse_when("* * * * * * *", now)


def test_cron_4_fields_rejected(now: datetime) -> None:
    with pytest.raises(ValueError, match="exactly 5 fields"):
        _parse_when("0 9 * *", now)


def test_cron_alias_daily_allowed(now: datetime) -> None:
    kind, next_run, expr = _parse_when("@daily", now)
    assert kind == "cron"
    assert expr == "@daily"


def test_cron_alias_hourly_allowed(now: datetime) -> None:
    kind, _, expr = _parse_when("@hourly", now)
    assert kind == "cron"
    assert expr == "@hourly"


# ── _parse_when — relative bounds ─────────────────────────────────


def test_relative_in_0s_rejected(now: datetime) -> None:
    with pytest.raises(ValueError, match="too short"):
        _parse_when("in 0s", now)


def test_relative_in_0m_rejected(now: datetime) -> None:
    with pytest.raises(ValueError, match="too short"):
        _parse_when("in 0m", now)


def test_relative_in_1s_allowed(now: datetime) -> None:
    kind, next_run, _ = _parse_when("in 1s", now)
    assert kind == "once"
    target = datetime.fromisoformat(next_run)
    assert abs((target - now).total_seconds() - 1) < 0.01


def test_relative_over_10y_rejected(now: datetime) -> None:
    with pytest.raises(ValueError, match="too large"):
        _parse_when("in 99999d", now)


def test_relative_exactly_max_allowed(now: datetime) -> None:
    kind, _, _ = _parse_when(f"in {_MAX_DELAY_SECONDS}s", now)
    assert kind == "once"


def test_relative_uppercase_unit_rejected(now: datetime) -> None:
    """Uppercase M should NOT be interpreted as 'minutes' — ambiguous with months."""
    with pytest.raises(ValueError):
        _parse_when("in 5M", now)


def test_relative_space_allowed(now: datetime) -> None:
    # Regex allows spaces, intentional tolerance.
    kind, _, _ = _parse_when("in 5 m", now)
    assert kind == "once"


# ── _parse_when — ISO 8601 ────────────────────────────────────────


def test_iso_past_rejected(now: datetime) -> None:
    with pytest.raises(ValueError, match="past"):
        _parse_when("2020-01-01T00:00:00Z", now)


def test_iso_far_future_rejected(now: datetime) -> None:
    with pytest.raises(ValueError, match="too far"):
        _parse_when("2099-01-01T00:00:00Z", now)


def test_iso_reasonable_future_allowed(now: datetime) -> None:
    kind, _, _ = _parse_when("2028-01-01T00:00:00Z", now)
    assert kind == "once"


def test_iso_naive_treated_as_utc(now: datetime) -> None:
    kind, next_run, _ = _parse_when("2028-01-01T00:00:00", now)
    assert kind == "once"
    assert "+00:00" in next_run


def test_iso_within_grace_period_allowed(now: datetime) -> None:
    """A timestamp 2s in the past is within grace (5s) → allowed."""
    past_ts = (now - timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
    kind, _, _ = _parse_when(past_ts, now)
    assert kind == "once"


# ── _parse_when — empty/invalid ───────────────────────────────────


def test_empty_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        _parse_when("", datetime.now(timezone.utc))


def test_whitespace_only_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        _parse_when("   ", datetime.now(timezone.utc))


# ── _validate_action_name ─────────────────────────────────────────


@pytest.mark.parametrize("name", [
    "shell.bash",
    "http.get",
    "channels.send_message",
    "rag.query",
    "a.b",
])
def test_action_name_valid(name: str) -> None:
    _validate_action_name(name)  # no exception


@pytest.mark.parametrize("name", [
    "",
    ".",
    ".x",
    "x.",
    "noDot",
    "...",
])
def test_action_name_invalid(name: str) -> None:
    with pytest.raises(ValueError):
        _validate_action_name(name)

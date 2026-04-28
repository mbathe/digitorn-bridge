"""Tests for RateLimiter - per-app request throttling."""

from __future__ import annotations

from pathlib import Path

import pytest

from digitorn.core.rate_limiter import RateLimiter, RateLimitExceeded


@pytest.fixture
def limiter(tmp_path: Path) -> RateLimiter:
    """Create a rate limiter with a temp directory."""
    return RateLimiter(directory=tmp_path / "rate_limits", default_rpm=5, window=60.0)


class TestRateLimiter:
    def test_allows_within_limit(self, limiter: RateLimiter) -> None:
        for _ in range(5):
            limiter.check("app-1")  # should not raise

    def test_blocks_over_limit(self, limiter: RateLimiter) -> None:
        for _ in range(5):
            limiter.check("app-1")

        with pytest.raises(RateLimitExceeded) as exc_info:
            limiter.check("app-1")

        assert exc_info.value.app_id == "app-1"
        assert exc_info.value.limit == 5
        assert exc_info.value.retry_after > 0

    def test_separate_apps_independent(self, limiter: RateLimiter) -> None:
        for _ in range(5):
            limiter.check("app-1")

        # app-2 is unaffected
        limiter.check("app-2")

    def test_custom_quota(self, limiter: RateLimiter) -> None:
        limiter.set_quota("vip-app", 100)

        for _ in range(10):
            limiter.check("vip-app")  # well within 100

    def test_remove_quota_reverts_to_default(self, limiter: RateLimiter) -> None:
        limiter.set_quota("app-1", 100)
        limiter.remove_quota("app-1")

        # Back to default of 5
        for _ in range(5):
            limiter.check("app-1")

        with pytest.raises(RateLimitExceeded):
            limiter.check("app-1")

    def test_get_usage(self, limiter: RateLimiter) -> None:
        limiter.check("app-1")
        limiter.check("app-1")

        usage = limiter.get_usage("app-1")
        assert usage["current"] == 2
        assert usage["limit"] == 5
        assert usage["remaining"] == 3
        assert usage["app_id"] == "app-1"

    def test_get_usage_empty(self, limiter: RateLimiter) -> None:
        usage = limiter.get_usage("new-app")
        assert usage["current"] == 0
        assert usage["remaining"] == 5

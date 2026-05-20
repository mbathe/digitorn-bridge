"""Rate limiter - per-app and per-user request throttling.

Supports DiskCache (default) and Redis (multi-host) via the
`KeyValueBackend` abstraction.

Implements a sliding window counter algorithm:
    - Each window is `window_seconds` long (default: 60s)
    - Requests are counted in the current window
    - When the window rolls over, the counter resets
    - Supports per-app and per-user overrides on top of global defaults

Usage::

    limiter = RateLimiter()
    limiter = RateLimiter(backend_url="redis://localhost")

    limiter.check("my-app")
    limiter.check("my-app", user_id="user-123")
    limiter.set_quota("my-app", 200)
    limiter.set_user_quota("my-app", "user-123", 30)
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from digitorn.core.kv import KeyValueBackend, create_backend


_DEFAULT_DIR = Path.home() / ".digitorn" / "rate_limits"
# Effectively off by default - the framework has per-bucket specific
# caps (auth, admin, deploy) in server.py which do the real work. The
# old 60 RPM default punished legitimate UI polling and multi-tab
# sessions. 100k RPM = 1666 RPS, well above any real client.
_DEFAULT_RPM = 100_000
_DEFAULT_WINDOW = 60.0


class RateLimitExceeded(Exception):
    """Raised when a tenant or user exceeds its rate limit."""

    def __init__(
        self,
        app_id: str,
        limit: int,
        window: float,
        retry_after: float,
        user_id: str | None = None,
    ):
        self.app_id = app_id
        self.user_id = user_id
        self.limit = limit
        self.window = window
        self.retry_after = retry_after
        scope = f"user '{user_id}' on '{app_id}'" if user_id else f"'{app_id}'"
        super().__init__(
            f"Rate limit exceeded for {scope}: "
            f"{limit} requests per {window}s (retry after {retry_after:.1f}s)"
        )


class RateLimiter:
    """Per-app sliding window rate limiter.

    Backed by any `KeyValueBackend` (DiskCache or Redis).
    Thread-safe and cross-process safe.

    Args:
        default_rpm: Default requests per minute for all apps.
        window: Window size in seconds.
        backend_url: Backend URL - `redis://...` for Redis, or None for DiskCache.
        directory: DiskCache directory (ignored if backend_url is Redis).
        backend: Pre-constructed backend instance (overrides url/directory).
    """

    def __init__(
        self,
        default_rpm: int = _DEFAULT_RPM,
        window: float = _DEFAULT_WINDOW,
        backend_url: str | None = None,
        directory: str | Path | None = None,
        backend: KeyValueBackend | None = None,
    ) -> None:
        self._default_rpm = default_rpm
        self._window = window
        if backend is not None:
            self._backend = backend
        elif backend_url:
            self._backend = create_backend(backend_url)
        else:
            self._backend = create_backend(
                directory=directory or _DEFAULT_DIR,
                size_limit=2**26,
            )

    def check(self, app_id: str, user_id: str | None = None) -> None:
        """Check rate limits (app-level, then user-level if user_id given).

        Increments counters and raises `RateLimitExceeded`
        if either limit would be exceeded.
        """
        now = time.time()

        app_key = self._window_key(app_id, now)
        app_limit = self._get_quota(app_id)
        # Safe from TOCTOU: backend.incr() is atomic on both DiskCache
        # (SQLite transact()) and Redis (native INCR).  The returned
        # count already includes this request, so the > check is exact.
        count = self._backend.incr(app_key, expire=self._window * 2)
        if count > app_limit:
            retry_after = self._retry_after(now)
            raise RateLimitExceeded(
                app_id=app_id, limit=app_limit,
                window=self._window, retry_after=retry_after,
            )

        if user_id:
            user_key = self._window_key(f"{app_id}:u:{user_id}", now)
            user_limit = self._get_user_quota(app_id, user_id)
            user_count = self._backend.incr(user_key, expire=self._window * 2)
            if user_count > user_limit:
                retry_after = self._retry_after(now)
                raise RateLimitExceeded(
                    app_id=app_id, user_id=user_id, limit=user_limit,
                    window=self._window, retry_after=retry_after,
                )

    def get_usage(self, app_id: str, user_id: str | None = None) -> dict[str, Any]:
        """Return current usage stats for an app (and optionally a user)."""
        now = time.time()
        app_key = self._window_key(app_id, now)
        app_count = self._backend.get(app_key, 0)
        app_limit = self._get_quota(app_id)
        result: dict[str, Any] = {
            "app_id": app_id,
            "current": app_count,
            "limit": app_limit,
            "remaining": max(0, app_limit - app_count),
            "window_seconds": self._window,
        }
        if user_id:
            user_key = self._window_key(f"{app_id}:u:{user_id}", now)
            user_count = self._backend.get(user_key, 0)
            user_limit = self._get_user_quota(app_id, user_id)
            result["user"] = {
                "user_id": user_id,
                "current": user_count,
                "limit": user_limit,
                "remaining": max(0, user_limit - user_count),
            }
        return result

    def set_quota(self, app_id: str, rpm: int) -> None:
        """Set a custom rate limit for a specific app."""
        self._backend.set(f"__quota__:{app_id}", rpm)

    def set_user_quota(self, app_id: str, user_id: str, rpm: int) -> None:
        """Set a custom rate limit for a specific user on an app."""
        self._backend.set(f"__uquota__:{app_id}:{user_id}", rpm)

    def set_default_user_rpm(self, app_id: str, rpm: int) -> None:
        """Set the default per-user RPM for an app."""
        self._backend.set(f"__uquota_default__:{app_id}", rpm)

    def remove_quota(self, app_id: str) -> None:
        """Remove custom quota for an app (reverts to default)."""
        self._backend.delete(f"__quota__:{app_id}")

    def remove_user_quota(self, app_id: str, user_id: str) -> None:
        """Remove custom quota for a user on an app."""
        self._backend.delete(f"__uquota__:{app_id}:{user_id}")

    def close(self) -> None:
        """Close the underlying backend."""
        self._backend.close()

    def _retry_after(self, now: float) -> float:
        window_start = int(now / self._window) * self._window
        return (window_start + self._window) - now

    def _window_key(self, scope: str, now: float) -> str:
        window_id = int(now / self._window)
        return f"rl:{scope}:{window_id}"

    def _get_quota(self, app_id: str) -> int:
        custom = self._backend.get(f"__quota__:{app_id}")
        if custom is not None:
            return int(custom)
        return self._default_rpm

    def _get_user_quota(self, app_id: str, user_id: str) -> int:
        custom = self._backend.get(f"__uquota__:{app_id}:{user_id}")
        if custom is not None:
            return int(custom)
        default = self._backend.get(f"__uquota_default__:{app_id}")
        if default is not None:
            return int(default)
        return max(self._default_rpm // 3, 10)

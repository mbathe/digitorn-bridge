"""Security enforcer - per-action rate limiting, data classification, temporal scopes.

Complements security_gate with runtime enforcement that requires mutable state.
SecurityProfile is immutable (frozen dataclass), so stateful checks live here.
"""

from __future__ import annotations

import time
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class ActionRateLimiter:
    """Sliding window rate limiter per module:action.

    Tracks call counts in 1-minute windows. Configurable per-action limits.

    YAML config:
        capabilities:
          rate_limits:
            "shell.run": 30
            "filesystem.write": 60
            "*": 120
    """

    def __init__(self, limits: dict[str, int] | None = None):
        self._limits: dict[str, int] = limits or {}
        self._default_limit: int = self._limits.get("*", 0)
        from collections import deque
        self._windows: dict[str, deque] = defaultdict(deque)

    def check(self, module_id: str, action: str) -> str | None:
        """Check if the action is within rate limits.

        Returns None if OK, or an error message if rate-limited.
        """
        if not self._limits:
            return None

        key = f"{module_id}.{action}"
        limit = self._limits.get(key) or self._default_limit
        if limit <= 0:
            return None

        now = time.monotonic()
        window = self._windows[key]

        cutoff = now - 60.0
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= limit:
            return (
                f"Rate limit exceeded for '{key}': {limit} calls/minute. "
                f"Current: {len(window)} calls in the last 60 seconds. "
                f"Wait {60 - (now - window[0]):.0f}s before retrying."
            )

        window.append(now)
        return None

    def stats(self) -> dict[str, Any]:
        """Return current rate limit stats."""
        now = time.monotonic()
        cutoff = now - 60.0
        result = {}
        for key, window in self._windows.items():
            active = [t for t in window if t >= cutoff]
            limit = self._limits.get(key) or self._default_limit
            result[key] = {
                "calls_last_60s": len(active),
                "limit": limit,
                "remaining": max(0, limit - len(active)) if limit > 0 else "unlimited",
            }
        return result


_CLASSIFICATION_RANK = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}


def check_data_classification(
    action_classification: str,
    max_classification: str,
) -> str | None:
    """Check if the action's data classification is within the allowed level.

    Returns None if OK, or an error message if the classification is too high.

    YAML config:
        capabilities:
          max_data_classification: "internal"
    """
    if not action_classification or not max_classification:
        return None

    action_rank = _CLASSIFICATION_RANK.get(action_classification, 1)
    max_rank = _CLASSIFICATION_RANK.get(max_classification, 3)

    if action_rank > max_rank:
        return (
            f"Data classification '{action_classification}' exceeds maximum "
            f"'{max_classification}' for this application."
        )
    return None


@dataclass
class TemporalGrant:
    """A time-limited or session-scoped permission grant."""

    module_id: str
    action: str
    scope: str
    granted_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    session_id: str = ""
    granted_by: str = ""

    def is_valid(self, current_session: str = "") -> bool:
        """Check if this grant is still valid."""
        if self.scope == "session" and self.session_id:
            return self.session_id == current_session
        if self.scope == "timed" and self.expires_at > 0:
            return time.time() < self.expires_at
        return True


class TemporalGrantStore:
    """Manages temporary permission grants.

    YAML config:
        capabilities:
          temporal_grants:
            - module: shell
              action: run
              scope: session
            - module: git
              action: push
              scope: timed
              duration: 3600
    """

    def __init__(self):
        self._grants: list[TemporalGrant] = []

    def add(
        self,
        module_id: str,
        action: str,
        scope: str = "session",
        duration: float = 0.0,
        session_id: str = "",
        granted_by: str = "",
    ) -> TemporalGrant:
        """Add a temporal grant."""
        grant = TemporalGrant(
            module_id=module_id,
            action=action,
            scope=scope,
            expires_at=time.time() + duration if duration > 0 else 0.0,
            session_id=session_id,
            granted_by=granted_by,
        )
        self._grants.append(grant)
        logger.info(
            "temporal_grant_added module=%s action=%s scope=%s",
            module_id, action, scope,
        )
        return grant

    def has_grant(self, module_id: str, action: str, session_id: str = "") -> bool:
        """Check if a valid temporal grant exists."""
        now = time.time()
        self._grants = [g for g in self._grants if g.is_valid(session_id)]
        return any(
            g.module_id == module_id and g.action == action
            and g.is_valid(session_id)
            for g in self._grants
        )

    def revoke(self, module_id: str, action: str) -> bool:
        """Revoke a temporal grant."""
        before = len(self._grants)
        self._grants = [
            g for g in self._grants
            if not (g.module_id == module_id and g.action == action)
        ]
        return len(self._grants) < before

    def list_grants(self, session_id: str = "") -> list[dict[str, Any]]:
        """List all active grants."""
        self._grants = [g for g in self._grants if g.is_valid(session_id)]
        return [
            {
                "module": g.module_id,
                "action": g.action,
                "scope": g.scope,
                "granted_at": g.granted_at,
                "expires_at": g.expires_at if g.expires_at > 0 else None,
                "granted_by": g.granted_by,
            }
            for g in self._grants
        ]

    def cleanup(self) -> int:
        """Remove expired grants. Returns count removed."""
        before = len(self._grants)
        self._grants = [g for g in self._grants if g.is_valid()]
        return before - len(self._grants)

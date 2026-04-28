"""Security audit log - persistent, structured trace of all security decisions.

Every action that passes through the security gate is logged:
- What was attempted (module, action, params)
- What decision was made (allowed, denied, approval_required, approved, denied_by_user)
- Why (which gate, which rule)
- Who (app_id, agent_id, session_id)
- When (timestamp)

The audit log is stored via the KeyValueBackend (DiskCache or Redis),
making it persistent across daemon restarts and queryable via API.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SecurityEvent:
    """A single security audit event."""

    timestamp: float
    app_id: str
    agent_id: str
    session_id: str

    module_id: str
    action: str
    risk_level: str
    params_summary: dict[str, Any]  # sanitized - no secrets

    decision: str  # "allowed" | "denied" | "approval_required" | "approved" | "denied_by_user"
    gate: str  # "gate0_inactive" | "gate1_module" | "gate2_risk" | "gate3_permissions" | "gate4_policy" | "approval"
    reason: str  # human-readable explanation

    policy_resolved: str = ""  # "auto" | "approve" | "block"
    approved_by: str = ""  # who approved (user_id or "cli" or "api")
    approval_duration_ms: float = 0.0  # how long user took to decide

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


_SENSITIVE_KEYS = frozenset({
    "password", "secret", "token", "api_key", "apikey",
    "credential", "auth", "authorization", "cookie",
    "private_key", "access_key", "secret_key",
})


def _sanitize_params(params: dict[str, Any]) -> dict[str, Any]:
    """Remove sensitive values from params for audit logging."""
    if not params:
        return {}
    sanitized = {}
    for key, value in params.items():
        if key.startswith("_"):
            continue  # skip internal keys
        key_lower = key.lower()
        if any(s in key_lower for s in _SENSITIVE_KEYS):
            sanitized[key] = "***REDACTED***"
        elif isinstance(value, str) and len(value) > 200:
            sanitized[key] = value[:200] + "...(truncated)"
        elif isinstance(value, (list, dict)) and len(str(value)) > 500:
            sanitized[key] = f"<{type(value).__name__} len={len(value)}>"
        else:
            sanitized[key] = value
    return sanitized


class SecurityAuditLog:
    """Persistent security audit log backed by KeyValueBackend.

    Events are stored as a rolling list per app. Older events
    are evicted when max_events is reached.
    """

    def __init__(
        self,
        backend: Any = None,
        max_events: int = 10000,
    ):
        self._backend = backend  # KeyValueBackend (DiskCache or Redis)
        self._buffer: list[SecurityEvent] = []  # in-memory if no backend
        self._max_events = max_events

    def _key(self, app_id: str) -> str:
        return f"_audit:{app_id}"

    def log(self, event: SecurityEvent) -> None:
        """Log a security event."""
        logger.debug(
            "security_audit app=%s action=%s.%s decision=%s gate=%s",
            event.app_id, event.module_id, event.action,
            event.decision, event.gate,
        )

        if self._backend is not None:
            key = self._key(event.app_id)
            events = self._backend.get(key, [])
            events.append(event.to_dict())
            if len(events) > self._max_events:
                events = events[-self._max_events:]
            self._backend.set(key, events)
        else:
            self._buffer.append(event)
            if len(self._buffer) > self._max_events:
                self._buffer = self._buffer[-self._max_events:]

    def query(
        self,
        app_id: str,
        *,
        decision: str | None = None,
        module_id: str | None = None,
        action: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query audit events with filters."""
        if self._backend is not None:
            events = self._backend.get(self._key(app_id), [])
        else:
            events = [e.to_dict() for e in self._buffer if e.app_id == app_id]

        filtered = []
        for e in reversed(events):  # newest first
            if decision and e.get("decision") != decision:
                continue
            if module_id and e.get("module_id") != module_id:
                continue
            if action and e.get("action") != action:
                continue
            if since and e.get("timestamp", 0) < since:
                continue
            filtered.append(e)
            if len(filtered) >= limit:
                break

        return filtered

    def stats(self, app_id: str) -> dict[str, Any]:
        """Get audit statistics for an app."""
        if self._backend is not None:
            events = self._backend.get(self._key(app_id), [])
        else:
            events = [e.to_dict() for e in self._buffer if e.app_id == app_id]

        if not events:
            return {"total": 0}

        decisions = {}
        modules = {}
        for e in events:
            d = e.get("decision", "unknown")
            m = e.get("module_id", "unknown")
            decisions[d] = decisions.get(d, 0) + 1
            modules[m] = modules.get(m, 0) + 1

        return {
            "total": len(events),
            "decisions": decisions,
            "modules": modules,
            "oldest": events[0].get("timestamp"),
            "newest": events[-1].get("timestamp"),
        }

    def clear(self, app_id: str) -> int:
        """Clear all audit events for an app. Returns count cleared."""
        if self._backend is not None:
            key = self._key(app_id)
            events = self._backend.get(key, [])
            count = len(events)
            self._backend.delete(key)
            return count
        else:
            before = len(self._buffer)
            self._buffer = [e for e in self._buffer if e.app_id != app_id]
            return before - len(self._buffer)


_audit_log: SecurityAuditLog | None = None


def get_audit_log() -> SecurityAuditLog:
    """Get or create the global audit log instance."""
    global _audit_log
    if _audit_log is None:
        _audit_log = SecurityAuditLog()
    return _audit_log


def set_audit_backend(backend: Any) -> None:
    """Set the backend for the global audit log (called at daemon startup)."""
    global _audit_log
    _audit_log = SecurityAuditLog(backend=backend)


def log_security_event(
    *,
    app_id: str,
    agent_id: str = "",
    session_id: str = "",
    module_id: str,
    action: str,
    risk_level: str = "low",
    params: dict[str, Any] | None = None,
    decision: str,
    gate: str,
    reason: str,
    policy_resolved: str = "",
    approved_by: str = "",
    approval_duration_ms: float = 0.0,
) -> None:
    """Convenience function to log a security event."""
    event = SecurityEvent(
        timestamp=time.time(),
        app_id=app_id,
        agent_id=agent_id,
        session_id=session_id,
        module_id=module_id,
        action=action,
        risk_level=risk_level,
        params_summary=_sanitize_params(params or {}),
        decision=decision,
        gate=gate,
        reason=reason,
        policy_resolved=policy_resolved,
        approved_by=approved_by,
        approval_duration_ms=approval_duration_ms,
    )
    get_audit_log().log(event)

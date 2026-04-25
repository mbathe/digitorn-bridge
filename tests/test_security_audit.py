"""E2E tests — SecurityAuditLog: event logging, sanitization, query, stats.

Covers:
- SecurityEvent creation and serialization
- Parameter sanitization (sensitive keys, long values, internal keys)
- In-memory audit log (no backend)
- Backend-backed audit log
- Query with filters (decision, module, action, since, limit)
- Stats aggregation
- Rolling eviction at max_events
- Clear per-app
- Convenience function log_security_event()
"""
from __future__ import annotations

import time

from digitorn.core.security_audit import (
    SecurityAuditLog,
    SecurityEvent,
    _sanitize_params,
    log_security_event,
    get_audit_log,
    set_audit_backend,
)


# ── SecurityEvent ────────────────────────────────────────────────────────


class TestSecurityEvent:
    def test_to_dict(self):
        ev = SecurityEvent(
            timestamp=1000.0,
            app_id="myapp",
            agent_id="agent1",
            session_id="s1",
            module_id="filesystem",
            action="write",
            risk_level="high",
            params_summary={"path": "/tmp/x"},
            decision="allowed",
            gate="gate3_permissions",
            reason="granted by policy",
        )
        d = ev.to_dict()
        assert d["app_id"] == "myapp"
        assert d["decision"] == "allowed"
        assert d["gate"] == "gate3_permissions"
        assert d["module_id"] == "filesystem"
        assert d["risk_level"] == "high"

    def test_to_json(self):
        ev = SecurityEvent(
            timestamp=1000.0,
            app_id="app",
            agent_id="a",
            session_id="s",
            module_id="mcp",
            action="call",
            risk_level="low",
            params_summary={},
            decision="denied",
            gate="gate2_risk",
            reason="too risky",
        )
        j = ev.to_json()
        assert '"decision": "denied"' in j
        assert '"gate": "gate2_risk"' in j

    def test_optional_fields(self):
        ev = SecurityEvent(
            timestamp=1.0, app_id="a", agent_id="", session_id="",
            module_id="m", action="a", risk_level="low",
            params_summary={}, decision="allowed", gate="gate0_inactive",
            reason="no security profile",
            policy_resolved="auto", approved_by="cli",
            approval_duration_ms=1500.0,
        )
        d = ev.to_dict()
        assert d["policy_resolved"] == "auto"
        assert d["approved_by"] == "cli"
        assert d["approval_duration_ms"] == 1500.0


# ── _sanitize_params ─────────────────────────────────────────────────────


class TestSanitizeParams:
    def test_empty_params(self):
        assert _sanitize_params({}) == {}
        assert _sanitize_params(None) == {}

    def test_normal_params_pass_through(self):
        result = _sanitize_params({"query": "select *", "limit": 10})
        assert result == {"query": "select *", "limit": 10}

    def test_sensitive_keys_redacted(self):
        params = {
            "password": "secret123",
            "api_key": "sk-abc",
            "Authorization": "Bearer xyz",
            "name": "safe",
        }
        result = _sanitize_params(params)
        assert result["password"] == "***REDACTED***"
        assert result["api_key"] == "***REDACTED***"
        assert result["Authorization"] == "***REDACTED***"
        assert result["name"] == "safe"

    def test_internal_keys_skipped(self):
        params = {"_internal": "hidden", "visible": "ok"}
        result = _sanitize_params(params)
        assert "_internal" not in result
        assert result["visible"] == "ok"

    def test_long_strings_truncated(self):
        params = {"content": "x" * 300}
        result = _sanitize_params(params)
        assert result["content"].endswith("...(truncated)")
        assert len(result["content"]) < 300

    def test_large_collections_summarized(self):
        params = {"data": list(range(200))}
        result = _sanitize_params(params)
        assert "<list len=200>" == result["data"]

    def test_case_insensitive_matching(self):
        params = {"SECRET_KEY": "nope", "Token": "nope", "safe": "ok"}
        result = _sanitize_params(params)
        assert result["SECRET_KEY"] == "***REDACTED***"
        assert result["Token"] == "***REDACTED***"


# ── SecurityAuditLog — in-memory (no backend) ───────────────────────────


class TestAuditLogInMemory:
    def _make_event(self, app_id="app1", decision="allowed", module_id="fs",
                    action="read", risk_level="low") -> SecurityEvent:
        return SecurityEvent(
            timestamp=time.time(),
            app_id=app_id, agent_id="agent", session_id="sess",
            module_id=module_id, action=action, risk_level=risk_level,
            params_summary={}, decision=decision,
            gate="gate3_permissions", reason="test",
        )

    def test_log_and_query(self):
        log = SecurityAuditLog()
        log.log(self._make_event())
        log.log(self._make_event())
        events = log.query("app1")
        assert len(events) == 2

    def test_query_filter_by_decision(self):
        log = SecurityAuditLog()
        log.log(self._make_event(decision="allowed"))
        log.log(self._make_event(decision="denied"))
        log.log(self._make_event(decision="allowed"))

        allowed = log.query("app1", decision="allowed")
        assert len(allowed) == 2
        denied = log.query("app1", decision="denied")
        assert len(denied) == 1

    def test_query_filter_by_module(self):
        log = SecurityAuditLog()
        log.log(self._make_event(module_id="filesystem"))
        log.log(self._make_event(module_id="database"))
        log.log(self._make_event(module_id="filesystem"))

        fs = log.query("app1", module_id="filesystem")
        assert len(fs) == 2

    def test_query_filter_by_action(self):
        log = SecurityAuditLog()
        log.log(self._make_event(action="read"))
        log.log(self._make_event(action="write"))
        results = log.query("app1", action="write")
        assert len(results) == 1

    def test_query_filter_by_since(self):
        log = SecurityAuditLog()
        old = self._make_event()
        old.timestamp = time.time() - 3600
        log.log(old)
        recent = self._make_event()
        log.log(recent)

        since_cutoff = time.time() - 60
        results = log.query("app1", since=since_cutoff)
        assert len(results) == 1

    def test_query_limit(self):
        log = SecurityAuditLog()
        for _ in range(10):
            log.log(self._make_event())
        results = log.query("app1", limit=3)
        assert len(results) == 3

    def test_query_returns_newest_first(self):
        log = SecurityAuditLog()
        e1 = self._make_event(action="first")
        e1.timestamp = 1000.0
        e2 = self._make_event(action="second")
        e2.timestamp = 2000.0
        log.log(e1)
        log.log(e2)

        results = log.query("app1")
        assert results[0]["action"] == "second"
        assert results[1]["action"] == "first"

    def test_query_wrong_app(self):
        log = SecurityAuditLog()
        log.log(self._make_event(app_id="app1"))
        results = log.query("app2")
        assert len(results) == 0

    def test_stats(self):
        log = SecurityAuditLog()
        log.log(self._make_event(decision="allowed", module_id="fs"))
        log.log(self._make_event(decision="allowed", module_id="db"))
        log.log(self._make_event(decision="denied", module_id="fs"))

        stats = log.stats("app1")
        assert stats["total"] == 3
        assert stats["decisions"]["allowed"] == 2
        assert stats["decisions"]["denied"] == 1
        assert stats["modules"]["fs"] == 2
        assert stats["modules"]["db"] == 1
        assert "oldest" in stats
        assert "newest" in stats

    def test_stats_empty(self):
        log = SecurityAuditLog()
        assert log.stats("empty") == {"total": 0}

    def test_clear(self):
        log = SecurityAuditLog()
        log.log(self._make_event(app_id="app1"))
        log.log(self._make_event(app_id="app1"))
        log.log(self._make_event(app_id="app2"))

        cleared = log.clear("app1")
        assert cleared == 2
        assert len(log.query("app1")) == 0
        assert len(log.query("app2")) == 1

    def test_clear_empty(self):
        log = SecurityAuditLog()
        assert log.clear("nope") == 0

    def test_rolling_eviction(self):
        log = SecurityAuditLog(max_events=5)
        for i in range(10):
            ev = self._make_event()
            ev.timestamp = float(i)
            log.log(ev)

        events = log.query("app1", limit=100)
        assert len(events) == 5
        # Oldest events evicted, newest kept
        assert events[0]["timestamp"] == 9.0


# ── SecurityAuditLog — with backend ─────────────────────────────────────


class _FakeBackend:
    """Minimal KeyValueBackend for testing."""
    def __init__(self):
        self._store: dict = {}

    def get(self, key, default=None):
        return self._store.get(key, default)

    def set(self, key, value, **kwargs):
        self._store[key] = value

    def delete(self, key):
        return self._store.pop(key, None) is not None


class TestAuditLogWithBackend:
    def _make_event(self, app_id="app1", decision="allowed") -> SecurityEvent:
        return SecurityEvent(
            timestamp=time.time(), app_id=app_id, agent_id="a", session_id="s",
            module_id="m", action="act", risk_level="low",
            params_summary={}, decision=decision,
            gate="gate1", reason="test",
        )

    def test_log_persists_to_backend(self):
        backend = _FakeBackend()
        log = SecurityAuditLog(backend=backend)
        log.log(self._make_event())
        assert "_audit:app1" in backend._store
        assert len(backend._store["_audit:app1"]) == 1

    def test_query_reads_from_backend(self):
        backend = _FakeBackend()
        log = SecurityAuditLog(backend=backend)
        log.log(self._make_event())
        log.log(self._make_event(decision="denied"))

        results = log.query("app1")
        assert len(results) == 2

    def test_stats_from_backend(self):
        backend = _FakeBackend()
        log = SecurityAuditLog(backend=backend)
        log.log(self._make_event(decision="allowed"))
        log.log(self._make_event(decision="denied"))

        stats = log.stats("app1")
        assert stats["total"] == 2

    def test_clear_removes_from_backend(self):
        backend = _FakeBackend()
        log = SecurityAuditLog(backend=backend)
        log.log(self._make_event())
        cleared = log.clear("app1")
        assert cleared == 1
        assert "_audit:app1" not in backend._store

    def test_rolling_eviction_with_backend(self):
        backend = _FakeBackend()
        log = SecurityAuditLog(backend=backend, max_events=3)
        for i in range(5):
            log.log(self._make_event())
        assert len(backend._store["_audit:app1"]) == 3


# ── Convenience function ─────────────────────────────────────────────────


class TestConvenienceFunction:
    def test_log_security_event_uses_global(self):
        # Reset global instance
        set_audit_backend(None)
        audit = get_audit_log()
        audit._buffer.clear()

        log_security_event(
            app_id="test",
            module_id="fs",
            action="write",
            decision="denied",
            gate="gate2_risk",
            reason="high risk",
            params={"password": "secret"},
        )

        events = audit.query("test")
        assert len(events) == 1
        assert events[0]["decision"] == "denied"
        # Params should be sanitized
        assert events[0]["params_summary"]["password"] == "***REDACTED***"

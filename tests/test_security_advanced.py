"""Advanced security tests — audit log, rate limiting, data classification, temporal scopes."""

import time
import pytest

from digitorn.core.security_audit import (
    SecurityAuditLog, SecurityEvent, log_security_event,
    get_audit_log, _sanitize_params,
)
from digitorn.core.security_enforcer import (
    ActionRateLimiter,
    check_data_classification,
    TemporalGrantStore,
    TemporalGrant,
)
from digitorn.core.security import (
    SecurityProfile, ModuleGrant, security_gate,
    PermissionDeniedError, ApprovalRequiredError,
)


# ── Audit Log ─────────────────────────────────────────────────────


class TestSecurityAuditLog:
    def test_log_and_query(self):
        log = SecurityAuditLog()
        event = SecurityEvent(
            timestamp=time.time(), app_id="test", agent_id="main",
            session_id="s1", module_id="filesystem", action="rm",
            risk_level="high", params_summary={"path": "/tmp/x"},
            decision="denied", gate="gate4_policy", reason="Block policy",
        )
        log.log(event)
        events = log.query("test")
        assert len(events) == 1
        assert events[0]["decision"] == "denied"
        assert events[0]["module_id"] == "filesystem"

    def test_query_with_filters(self):
        log = SecurityAuditLog()
        for i, (mod, dec) in enumerate([
            ("filesystem", "allowed"), ("filesystem", "denied"),
            ("git", "allowed"), ("shell", "denied"),
        ]):
            log.log(SecurityEvent(
                timestamp=time.time() + i, app_id="test", agent_id="main",
                session_id="s1", module_id=mod, action="test",
                risk_level="low", params_summary={},
                decision=dec, gate="test", reason="test",
            ))

        # Filter by decision
        denied = log.query("test", decision="denied")
        assert len(denied) == 2

        # Filter by module
        fs_events = log.query("test", module_id="filesystem")
        assert len(fs_events) == 2

        # Filter by both
        fs_denied = log.query("test", decision="denied", module_id="filesystem")
        assert len(fs_denied) == 1

    def test_query_limit(self):
        log = SecurityAuditLog()
        for i in range(20):
            log.log(SecurityEvent(
                timestamp=time.time() + i, app_id="test", agent_id="",
                session_id="", module_id="fs", action="read",
                risk_level="low", params_summary={},
                decision="allowed", gate="ok", reason="ok",
            ))
        events = log.query("test", limit=5)
        assert len(events) == 5

    def test_eviction_at_max(self):
        log = SecurityAuditLog(max_events=10)
        for i in range(20):
            log.log(SecurityEvent(
                timestamp=time.time() + i, app_id="test", agent_id="",
                session_id="", module_id="fs", action="read",
                risk_level="low", params_summary={},
                decision="allowed", gate="ok", reason="ok",
            ))
        events = log.query("test", limit=100)
        assert len(events) == 10

    def test_stats(self):
        log = SecurityAuditLog()
        for dec in ["allowed", "allowed", "denied"]:
            log.log(SecurityEvent(
                timestamp=time.time(), app_id="test", agent_id="",
                session_id="", module_id="fs", action="read",
                risk_level="low", params_summary={},
                decision=dec, gate="ok", reason="ok",
            ))
        stats = log.stats("test")
        assert stats["total"] == 3
        assert stats["decisions"]["allowed"] == 2
        assert stats["decisions"]["denied"] == 1

    def test_clear(self):
        log = SecurityAuditLog()
        log.log(SecurityEvent(
            timestamp=time.time(), app_id="test", agent_id="",
            session_id="", module_id="fs", action="read",
            risk_level="low", params_summary={},
            decision="allowed", gate="ok", reason="ok",
        ))
        cleared = log.clear("test")
        assert cleared == 1
        assert log.query("test") == []


class TestParamSanitizer:
    def test_redacts_secrets(self):
        result = _sanitize_params({
            "path": "/tmp/x",
            "api_key": "sk-secret-123",
            "password": "hunter2",
            "name": "test",
        })
        assert result["path"] == "/tmp/x"
        assert result["api_key"] == "***REDACTED***"
        assert result["password"] == "***REDACTED***"
        assert result["name"] == "test"

    def test_truncates_long_strings(self):
        result = _sanitize_params({"content": "x" * 500})
        assert len(result["content"]) < 300
        assert "truncated" in result["content"]

    def test_skips_internal_keys(self):
        result = _sanitize_params({
            "_approved": True,
            "_agent_id": "main",
            "path": "/tmp",
        })
        assert "_approved" not in result
        assert "path" in result

    def test_handles_large_collections(self):
        result = _sanitize_params({"data": list(range(200))})
        assert isinstance(result["data"], str)
        assert "list" in result["data"].lower()


# ── Rate Limiter ──────────────────────────────────────────────────


class TestActionRateLimiter:
    def test_no_limits_passes(self):
        rl = ActionRateLimiter()
        assert rl.check("filesystem", "read") is None

    def test_under_limit_passes(self):
        rl = ActionRateLimiter({"filesystem.read": 10})
        for _ in range(9):
            assert rl.check("filesystem", "read") is None

    def test_at_limit_blocked(self):
        rl = ActionRateLimiter({"filesystem.read": 5})
        for _ in range(5):
            rl.check("filesystem", "read")
        err = rl.check("filesystem", "read")
        assert err is not None
        assert "rate limit" in err.lower()

    def test_different_actions_independent(self):
        rl = ActionRateLimiter({"filesystem.read": 2, "filesystem.write": 2})
        rl.check("filesystem", "read")
        rl.check("filesystem", "read")
        # read is at limit
        assert rl.check("filesystem", "read") is not None
        # write should still be fine
        assert rl.check("filesystem", "write") is None

    def test_default_limit(self):
        rl = ActionRateLimiter({"*": 3})
        for _ in range(3):
            rl.check("any", "action")
        assert rl.check("any", "action") is not None

    def test_stats(self):
        rl = ActionRateLimiter({"filesystem.read": 10})
        rl.check("filesystem", "read")
        rl.check("filesystem", "read")
        stats = rl.stats()
        assert stats["filesystem.read"]["calls_last_60s"] == 2
        assert stats["filesystem.read"]["remaining"] == 8


# ── Data Classification ──────────────────────────────────────────


class TestDataClassification:
    def test_no_classification_passes(self):
        assert check_data_classification("", "") is None

    def test_public_within_internal(self):
        assert check_data_classification("public", "internal") is None

    def test_internal_within_internal(self):
        assert check_data_classification("internal", "internal") is None

    def test_confidential_exceeds_internal(self):
        err = check_data_classification("confidential", "internal")
        assert err is not None
        assert "confidential" in err

    def test_restricted_exceeds_confidential(self):
        err = check_data_classification("restricted", "confidential")
        assert err is not None

    def test_restricted_within_restricted(self):
        assert check_data_classification("restricted", "restricted") is None


# ── Temporal Scopes ───────────────────────────────────────────────


class TestTemporalGrants:
    def test_add_and_check_session_grant(self):
        store = TemporalGrantStore()
        store.add("shell", "run", scope="session", session_id="s1")
        assert store.has_grant("shell", "run", session_id="s1") is True
        assert store.has_grant("shell", "run", session_id="s2") is False

    def test_timed_grant_valid(self):
        store = TemporalGrantStore()
        store.add("git", "push", scope="timed", duration=3600)
        assert store.has_grant("git", "push") is True

    def test_timed_grant_expired(self):
        store = TemporalGrantStore()
        grant = store.add("git", "push", scope="timed", duration=0.001)
        time.sleep(0.01)
        assert store.has_grant("git", "push") is False

    def test_revoke(self):
        store = TemporalGrantStore()
        store.add("shell", "run", scope="session")
        assert store.has_grant("shell", "run") is True
        store.revoke("shell", "run")
        assert store.has_grant("shell", "run") is False

    def test_list_grants(self):
        store = TemporalGrantStore()
        store.add("shell", "run", scope="session", granted_by="admin")
        store.add("git", "push", scope="timed", duration=60, granted_by="cli")
        grants = store.list_grants()
        assert len(grants) == 2
        assert grants[0]["granted_by"] == "admin"
        assert grants[1]["scope"] == "timed"

    def test_cleanup_expired(self):
        store = TemporalGrantStore()
        store.add("a", "b", scope="timed", duration=0.001)
        store.add("c", "d", scope="session")
        time.sleep(0.01)
        removed = store.cleanup()
        assert removed == 1
        assert len(store.list_grants()) == 1


# ── Integration: Audit + Security Gate ────────────────────────────


class TestAuditIntegration:
    """Verify that security_gate logs events to the audit log."""

    def _profile(self, **kwargs):
        defaults = dict(
            app_id="audit-test",
            default_policy="auto",
            max_risk_level="medium",
        )
        defaults.update(kwargs)
        return SecurityProfile(**defaults)

    def test_allowed_action_logged(self):
        log = SecurityAuditLog()
        from digitorn.core import security_audit
        old = security_audit._audit_log
        security_audit._audit_log = log

        try:
            mg = ModuleGrant(module_id="filesystem", default_action_policy="auto")
            p = self._profile(module_grants={"filesystem": mg})
            security_gate(
                profile=p, module_id="filesystem", action="read",
                required_permissions=[], risk_level="low",
                irreversible=False, params={},
            )
            events = log.query("audit-test")
            assert len(events) == 1
            assert events[0]["decision"] == "allowed"
            assert events[0]["gate"] == "all_gates_passed"
        finally:
            security_audit._audit_log = old

    def test_denied_action_logged(self):
        log = SecurityAuditLog()
        from digitorn.core import security_audit
        old = security_audit._audit_log
        security_audit._audit_log = log

        try:
            p = self._profile(is_active=False)
            with pytest.raises(PermissionDeniedError):
                security_gate(
                    profile=p, module_id="filesystem", action="read",
                    required_permissions=[], risk_level="low",
                    irreversible=False, params={},
                )
            events = log.query("audit-test")
            assert len(events) == 1
            assert events[0]["decision"] == "denied"
            assert events[0]["gate"] == "gate0_inactive"
        finally:
            security_audit._audit_log = old

    def test_approval_required_logged(self):
        log = SecurityAuditLog()
        from digitorn.core import security_audit
        old = security_audit._audit_log
        security_audit._audit_log = log

        try:
            mg = ModuleGrant(module_id="filesystem", action_overrides={"rm": "approve"})
            p = self._profile(
                granted_permissions=frozenset({"filesystem:rm"}),
                module_grants={"filesystem": mg},
            )
            with pytest.raises(ApprovalRequiredError):
                security_gate(
                    profile=p, module_id="filesystem", action="rm",
                    required_permissions=[], risk_level="high",
                    irreversible=False, params={},
                )
            events = log.query("audit-test")
            assert len(events) == 1
            assert events[0]["decision"] == "approval_required"
        finally:
            security_audit._audit_log = old

    def test_rate_limit_logged(self):
        log = SecurityAuditLog()
        from digitorn.core import security_audit
        old = security_audit._audit_log
        security_audit._audit_log = log

        try:
            mg = ModuleGrant(module_id="filesystem", default_action_policy="auto")
            p = self._profile(module_grants={"filesystem": mg})

            rl = ActionRateLimiter({"filesystem.read": 1})
            # First call passes
            security_gate(
                profile=p, module_id="filesystem", action="read",
                required_permissions=[], risk_level="low",
                irreversible=False, params={"_rate_limiter": rl},
            )
            # Second call blocked by rate limit
            with pytest.raises(PermissionDeniedError):
                security_gate(
                    profile=p, module_id="filesystem", action="read",
                    required_permissions=[], risk_level="low",
                    irreversible=False, params={"_rate_limiter": rl},
                )
            events = log.query("audit-test")
            assert len(events) == 2
            assert events[0]["decision"] == "denied"  # newest first
            assert events[0]["gate"] == "gate6_rate_limit"
            assert events[1]["decision"] == "allowed"
        finally:
            security_audit._audit_log = old

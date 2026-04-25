"""Security profile + sandbox integration tests for the channels module.

Tests that:
1. Compiler builds correct SecurityProfile from channels capabilities YAML.
2. Sandbox builder grants network access when channels module is present.
3. Security gate enforces grant/deny/approve on channel actions.
4. Capability resolution follows the correct priority chain.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from digitorn.core.app import AppYAMLCompiler
from digitorn.core.security import (
    ModuleGrant,
    SecurityProfile,
    resolve_action_policy,
    security_gate,
)
from digitorn.core.sandbox.builder import build_sandbox_profile
from digitorn.core.sandbox.profile import SandboxProfile
from digitorn.modules.exceptions import ApprovalRequiredError, PermissionDeniedError
from digitorn.modules.registry import ModuleRegistry
from digitorn.core.loader import load_modules


EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "channels"

_DUMMY_SECRETS = {
    "DEEPSEEK_API_KEY": "sk-test-dummy",
    "GH_WEBHOOK_SECRET": "test-secret",
    "SLACK_WEBHOOK_URL": "https://hooks.slack.com/test",
    "SLACK_OPS_WEBHOOK": "https://hooks.slack.com/test",
    "TWILIO_WA_SECRET": "test-twilio",
    "TWILIO_AUTH_BASE64": "dGVzdA==",
    "EMAIL_APP_PASSWORD": "test-password",
    "DB_URL": "sqlite:///test.db",
    "EMAIL_PWD": "test-pwd",
}


@pytest.fixture(scope="module")
def compiler() -> AppYAMLCompiler:
    registry = ModuleRegistry()
    load_modules(registry)
    return AppYAMLCompiler(registry)


@pytest.fixture(autouse=True)
def _inject_env():
    saved = {}
    for k, v in {"DEEPSEEK_API_KEY": "sk-test", "WEBHOOK_SECRET": "test"}.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ── Compiler → SecurityProfile ──────────────────────────────────────


class TestCapabilitiesToProfile:
    def test_grant_creates_auto_policy(self, compiler: AppYAMLCompiler):
        """grant: [{module: channels, actions: [send_message]}] → auto policy."""
        compiled = compiler.compile_file(
            EXAMPLES_DIR / "08-webhook-outbound-slack.yaml",
            secrets=_DUMMY_SECRETS,
        )
        profile = compiled.security_profile
        assert profile is not None

        grant = profile.module_grants.get("channels")
        assert grant is not None
        assert grant.action_policy("send_message") == "auto"

    def test_deny_creates_block_policy(self, compiler: AppYAMLCompiler):
        """deny: [{module: channels, actions: [broadcast]}] → block policy."""
        compiled = compiler.compile_file(
            EXAMPLES_DIR / "31-cron-capabilities.yaml",
            secrets=_DUMMY_SECRETS,
        )
        profile = compiled.security_profile
        assert profile is not None

        grant = profile.module_grants.get("channels")
        assert grant is not None
        # 31 has deny entries — check one is blocked
        overrides = grant.action_overrides
        blocked = [a for a, p in overrides.items() if p == "block"]
        assert len(blocked) > 0, "Expected at least one blocked action"

    def test_grant_adds_permissions(self, compiler: AppYAMLCompiler):
        """Granted actions appear in granted_permissions set."""
        compiled = compiler.compile_file(
            EXAMPLES_DIR / "08-webhook-outbound-slack.yaml",
            secrets=_DUMMY_SECRETS,
        )
        profile = compiled.security_profile
        assert profile is not None
        assert profile.has_permission("channels:send_message")

    def test_context_builder_always_auto(self, compiler: AppYAMLCompiler):
        """context_builder is always auto-granted regardless of capabilities."""
        compiled = compiler.compile_file(
            EXAMPLES_DIR / "31-cron-capabilities.yaml",
            secrets=_DUMMY_SECRETS,
        )
        profile = compiled.security_profile
        assert profile is not None

        cb_grant = profile.module_grants.get("context_builder")
        assert cb_grant is not None
        assert cb_grant.default_action_policy == "auto"

    def test_no_capabilities_no_profile(self, compiler: AppYAMLCompiler):
        """An app without capabilities: block has no security_profile."""
        compiled = compiler.compile_file(
            EXAMPLES_DIR / "01-cron-hello.yaml",
            secrets=_DUMMY_SECRETS,
        )
        # No capabilities block → no security restrictions
        assert compiled.security_profile is None

    def test_multiple_grants_merge(self, compiler: AppYAMLCompiler):
        """Multiple grant entries for channels merge into one ModuleGrant."""
        compiled = compiler.compile_file(
            EXAMPLES_DIR / "24-multi-provider-send.yaml",
            secrets=_DUMMY_SECRETS,
        )
        profile = compiled.security_profile
        assert profile is not None

        grant = profile.module_grants.get("channels")
        assert grant is not None
        # Should have multiple auto actions
        auto_actions = [a for a, p in grant.action_overrides.items() if p == "auto"]
        assert len(auto_actions) >= 2


# ── Sandbox Builder ─────────────────────────────────────────────────


class TestSandboxBuilder:
    def test_channels_enables_network(self, compiler: AppYAMLCompiler):
        """Channels module should enable network in sandbox (for webhooks)."""
        compiled = compiler.compile_file(
            EXAMPLES_DIR / "08-webhook-outbound-slack.yaml",
            secrets=_DUMMY_SECRETS,
        )
        profile = build_sandbox_profile(compiled)
        # Channels needs network for webhook HTTP in/out
        assert profile.allow_network is True

    def test_no_security_profile_unrestricted(self, compiler: AppYAMLCompiler):
        """No security profile → everything unrestricted."""
        compiled = compiler.compile_file(
            EXAMPLES_DIR / "01-cron-hello.yaml",
            secrets=_DUMMY_SECRETS,
        )
        assert compiled.security_profile is None
        profile = build_sandbox_profile(compiled)
        assert profile.fs_unrestricted is True
        assert profile.allow_network is True
        assert profile.allow_exec is True

    def test_filesystem_module_adds_paths(self, compiler: AppYAMLCompiler):
        """Filesystem module constraints → writable_paths in sandbox."""
        compiled = compiler.compile_file(
            EXAMPLES_DIR / "34-monitoring-station.yaml",
            secrets=_DUMMY_SECRETS,
        )
        profile = build_sandbox_profile(compiled)
        # 34 has filesystem with paths constraint
        paths_str = " ".join(profile.writable_paths)
        assert "channels-test" in paths_str or profile.fs_unrestricted


# ── Security Gate Enforcement ───────────────────────────────────────


class TestSecurityGateChannels:
    def _profile_with_channels_grant(
        self,
        grant_actions: list[str] | None = None,
        deny_actions: list[str] | None = None,
        default_policy: str = "approve",
    ) -> SecurityProfile:
        """Build a SecurityProfile with specific channels actions."""
        overrides = {}
        permissions = set()
        for a in (grant_actions or []):
            overrides[a] = "auto"
            permissions.add(f"channels:{a}")
        for a in (deny_actions or []):
            overrides[a] = "block"

        return SecurityProfile(
            app_id="test-app",
            is_active=True,
            default_policy=default_policy,
            granted_permissions=frozenset(permissions),
            module_grants={
                "channels": ModuleGrant(
                    module_id="channels",
                    visibility="full",
                    default_action_policy="approve",
                    action_overrides=overrides,
                ),
            },
        )

    def test_granted_action_passes(self):
        """Granted action goes through security gate without error."""
        profile = self._profile_with_channels_grant(
            grant_actions=["send_message"],
        )
        # Should not raise
        security_gate(
            profile=profile,
            module_id="channels",
            action="send_message",
            required_permissions=[],
            risk_level="medium",
            irreversible=False,
            params={"provider": "slack", "message": "test"},
        )

    def test_blocked_action_raises(self):
        """Blocked action raises PermissionDeniedError."""
        profile = self._profile_with_channels_grant(
            grant_actions=["send_message"],
            deny_actions=["broadcast"],
        )
        with pytest.raises(PermissionDeniedError):
            security_gate(
                profile=profile,
                module_id="channels",
                action="broadcast",
                required_permissions=[],
                risk_level="medium",
                irreversible=False,
                params={"message": "test"},
            )

    def test_unapproved_action_requires_approval(self):
        """Action with approve policy raises ApprovalRequiredError."""
        profile = self._profile_with_channels_grant(
            grant_actions=["send_message"],
            # broadcast not granted, not denied → falls to default "approve"
        )
        with pytest.raises(ApprovalRequiredError):
            security_gate(
                profile=profile,
                module_id="channels",
                action="broadcast",
                required_permissions=[],
                risk_level="medium",
                irreversible=False,
                params={"message": "test"},
            )

    def test_approved_action_passes(self):
        """Action with _approved=True passes the approve gate."""
        profile = self._profile_with_channels_grant(
            grant_actions=["send_message"],
        )
        # broadcast falls to "approve" policy
        # with _approved=True it should pass
        security_gate(
            profile=profile,
            module_id="channels",
            action="broadcast",
            required_permissions=[],
            risk_level="medium",
            irreversible=False,
            params={"message": "test", "_approved": True},
        )

    def test_hidden_module_not_visible_but_executable(self):
        """Hidden module is invisible to agent but still executable if called.

        visibility=hidden controls tool index visibility, not execution.
        Execution is gated by action policy resolution:
          action_overrides → risk_approval_rules → module default → app default
        """
        profile = SecurityProfile(
            app_id="test-app",
            is_active=True,
            default_policy="auto",
            risk_approval_rules={},  # No risk rules → falls to module default
            module_grants={
                "channels": ModuleGrant(
                    module_id="channels",
                    visibility="hidden",
                    default_action_policy="block",
                ),
            },
        )
        assert profile.is_module_visible("channels") is False
        # Execution blocked because: no action override, no risk rule,
        # module default_action_policy="block"
        with pytest.raises(PermissionDeniedError):
            security_gate(
                profile=profile,
                module_id="channels",
                action="send_message",
                required_permissions=[],
                risk_level="low",
                irreversible=False,
                params={},
            )

    def test_inactive_app_denied(self):
        """Inactive app → all actions denied."""
        profile = SecurityProfile(
            app_id="test-app",
            is_active=False,
            default_policy="auto",
        )
        with pytest.raises(PermissionDeniedError):
            security_gate(
                profile=profile,
                module_id="channels",
                action="send_message",
                required_permissions=[],
                risk_level="low",
                irreversible=False,
                params={},
            )


# ── Policy Resolution ───────────────────────────────────────────────


class TestPolicyResolution:
    def test_action_override_wins(self):
        """Explicit action override takes priority over everything."""
        profile = SecurityProfile(
            app_id="test",
            default_policy="block",
            module_grants={
                "channels": ModuleGrant(
                    module_id="channels",
                    default_action_policy="block",
                    action_overrides={"send_message": "auto"},
                ),
            },
        )
        assert resolve_action_policy(profile, "channels", "send_message", "high") == "auto"

    def test_module_default_used_when_no_override(self):
        """Module grant default_action_policy used when no action override."""
        profile = SecurityProfile(
            app_id="test",
            default_policy="auto",
            module_grants={
                "channels": ModuleGrant(
                    module_id="channels",
                    default_action_policy="approve",
                    action_overrides={},
                ),
            },
        )
        assert resolve_action_policy(profile, "channels", "unknown_action", "medium") == "approve"

    def test_app_default_used_when_no_grant(self):
        """App-level default_policy used when module has no grant and no risk rule."""
        profile = SecurityProfile(
            app_id="test",
            default_policy="block",
            risk_approval_rules={},  # No risk rules → fall through to default
        )
        assert resolve_action_policy(profile, "channels", "send_message", "low") == "block"

    def test_admin_always_auto(self):
        """Admin profile → always auto."""
        profile = SecurityProfile(
            app_id="test",
            is_admin=True,
            default_policy="block",
        )
        assert resolve_action_policy(profile, "channels", "anything", "high") == "auto"

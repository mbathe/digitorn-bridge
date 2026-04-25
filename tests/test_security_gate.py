"""Tests for security_gate — the core security enforcement layer.

Tests our modification: explicit YAML grants (via action_overrides + granted_permissions)
override symbolic permissions and risk level checks. Block always wins.
"""

import pytest

from digitorn.core.security import (
    SecurityProfile,
    ModuleGrant,
    security_gate,
    PermissionDeniedError,
    ApprovalRequiredError,
)


# ── Helpers ───────────────────────────────────────────────────────


def _profile(
    *,
    granted: set[str] | None = None,
    modules: dict[str, ModuleGrant] | None = None,
    max_risk: str = "medium",
    active: bool = True,
    default_policy: str = "auto",
) -> SecurityProfile:
    return SecurityProfile(
        app_id="test",
        is_active=active,
        default_policy=default_policy,
        granted_permissions=frozenset(granted or set()),
        module_grants=modules or {},
        max_risk_level=max_risk,
    )


def _gate(
    profile: SecurityProfile,
    module: str = "filesystem",
    action: str = "read",
    perms: list[str] | None = None,
    risk: str = "low",
    params: dict | None = None,
):
    security_gate(
        profile=profile,
        module_id=module,
        action=action,
        required_permissions=perms or [],
        risk_level=risk,
        irreversible=False,
        params=params or {},
    )


# ── Gate 0: App active ───────────────────────────────────────────


class TestAppActive:
    def test_active_passes(self):
        _gate(_profile())

    def test_inactive_blocked(self):
        with pytest.raises(PermissionDeniedError):
            _gate(_profile(active=False))


# ── Gate 2: Risk level ───────────────────────────────────────────


class TestRiskLevel:
    def test_low_risk_passes(self):
        _gate(_profile(max_risk="medium"), risk="low")

    def test_medium_risk_passes(self):
        mg = ModuleGrant(module_id="filesystem", default_action_policy="auto", action_overrides={"read": "auto"})
        _gate(_profile(max_risk="medium", modules={"filesystem": mg}), risk="medium")

    def test_high_risk_blocked_by_cap(self):
        with pytest.raises(PermissionDeniedError):
            _gate(_profile(max_risk="medium"), action="rm", risk="high")

    def test_high_risk_passes_when_granted(self):
        """Explicit grant overrides risk cap — passes without approval."""
        mg = ModuleGrant(module_id="filesystem", action_overrides={"rm": "auto"})
        p = _profile(
            max_risk="medium",
            granted={"filesystem:rm"},
            modules={"filesystem": mg},
        )
        _gate(p, action="rm", risk="high")

    def test_high_risk_passes_with_policy_override(self):
        """Action override (approve) overrides risk cap."""
        mg = ModuleGrant(module_id="filesystem", action_overrides={"rm": "approve"})
        p = _profile(max_risk="medium", modules={"filesystem": mg})
        with pytest.raises(ApprovalRequiredError):
            _gate(p, action="rm", risk="high")


# ── Gate 3: Symbolic permissions ─────────────────────────────────


class TestPermissions:
    def test_no_perms_passes(self):
        _gate(_profile(), perms=[])

    def test_missing_perm_blocked(self):
        with pytest.raises(PermissionDeniedError):
            _gate(_profile(), perms=["fs.read"])

    def test_granted_perm_passes(self):
        _gate(_profile(granted={"fs.read"}), perms=["fs.read"])

    def test_action_grant_overrides_missing_perms(self):
        """filesystem:edit grant overrides missing fs.read + fs.write."""
        mg = ModuleGrant(module_id="filesystem", action_overrides={"edit": "auto"})
        p = _profile(granted={"filesystem:edit"}, modules={"filesystem": mg})
        _gate(p, action="edit", risk="medium", perms=["fs.read", "fs.write"])

    def test_policy_override_overrides_missing_perms(self):
        """approve policy overrides missing permissions."""
        mg = ModuleGrant(module_id="filesystem", action_overrides={"edit": "approve"})
        p = _profile(modules={"filesystem": mg})
        with pytest.raises(ApprovalRequiredError):
            _gate(p, action="edit", risk="medium", perms=["fs.read", "fs.write"])


# ── Gate 4: Action policy ────────────────────────────────────────


class TestActionPolicy:
    def _mg(self, overrides: dict) -> ModuleGrant:
        return ModuleGrant(module_id="filesystem", action_overrides=overrides)

    def test_auto_passes(self):
        p = _profile(modules={"filesystem": self._mg({"read": "auto"})})
        _gate(p, action="read")

    def test_block_denied(self):
        p = _profile(modules={"filesystem": self._mg({"rm": "block"})})
        with pytest.raises(PermissionDeniedError):
            _gate(p, action="rm", risk="high")

    def test_approve_without_flag(self):
        p = _profile(modules={"filesystem": self._mg({"rm": "approve"})})
        with pytest.raises(ApprovalRequiredError):
            _gate(p, action="rm", risk="high")

    def test_approve_with_flag(self):
        p = _profile(modules={"filesystem": self._mg({"rm": "approve"})})
        _gate(p, action="rm", risk="high", params={"_approved": True})

    def test_block_wins_over_grant(self):
        """Block always wins, even if action is in granted_permissions."""
        mg = self._mg({"rm": "block"})
        p = _profile(granted={"filesystem:rm"}, modules={"filesystem": mg})
        with pytest.raises(PermissionDeniedError):
            _gate(p, action="rm", risk="high")


# ── Integration: Claude Code YAML ────────────────────────────────


class TestClaudeCodePattern:
    """Reproduce the exact security profile from claude-code.yaml."""

    @pytest.fixture
    def profile(self):
        fs = ModuleGrant(
            module_id="filesystem",
            action_overrides={
                "read": "auto", "ls": "auto", "find": "auto",
                "grep": "auto", "file_stat": "auto",
                "write": "auto", "edit": "auto", "insert": "auto",
                "mkdir": "auto", "cp": "auto", "mv": "auto",
                "undo": "auto", "list_checkpoints": "auto",
                "diff_checkpoint": "auto",
                "rm": "approve",
            },
        )
        git = ModuleGrant(
            module_id="git",
            action_overrides={
                "status": "auto", "diff": "auto", "log": "auto",
                "blame": "auto", "show": "auto", "branch_list": "auto",
                "add": "auto", "commit": "auto",
                "push": "approve", "reset": "approve", "merge": "approve",
            },
        )
        shell = ModuleGrant(
            module_id="shell",
            action_overrides={
                "run": "auto", "bash": "auto", "which": "auto", "env": "auto",
            },
        )

        # Build granted_permissions like compiler does
        granted = set()
        for mid, mg in [("filesystem", fs), ("git", git), ("shell", shell)]:
            for a in mg.action_overrides:
                granted.add(f"{mid}:{a}")

        return SecurityProfile(
            app_id="claude-code",
            granted_permissions=frozenset(granted),
            module_grants={"filesystem": fs, "git": git, "shell": shell},
            max_risk_level="medium",
        )

    def test_fs_read(self, profile):
        _gate(profile, module="filesystem", action="read",
              risk="low", perms=["fs.read"])

    def test_fs_ls(self, profile):
        _gate(profile, module="filesystem", action="ls",
              risk="low", perms=["fs.list"])

    def test_fs_edit(self, profile):
        _gate(profile, module="filesystem", action="edit",
              risk="medium", perms=["fs.read", "fs.write"])

    def test_fs_write(self, profile):
        _gate(profile, module="filesystem", action="write",
              risk="medium", perms=["fs.write"])

    def test_fs_rm_needs_approval(self, profile):
        with pytest.raises(ApprovalRequiredError):
            _gate(profile, module="filesystem", action="rm",
                  risk="high", perms=["fs.delete"])

    def test_fs_rm_approved(self, profile):
        _gate(profile, module="filesystem", action="rm",
              risk="high", perms=["fs.delete"],
              params={"_approved": True})

    def test_git_status(self, profile):
        _gate(profile, module="git", action="status", risk="low")

    def test_git_commit(self, profile):
        _gate(profile, module="git", action="commit", risk="medium")

    def test_git_push_needs_approval(self, profile):
        with pytest.raises(ApprovalRequiredError):
            _gate(profile, module="git", action="push", risk="high")

    def test_shell_run(self, profile):
        """shell.run is high risk but explicitly granted — passes."""
        _gate(profile, module="shell", action="run", risk="high")

    def test_shell_bash(self, profile):
        _gate(profile, module="shell", action="bash", risk="high")

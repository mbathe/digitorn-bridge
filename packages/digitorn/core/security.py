"""Digitorn - Security profile, action policy resolution, and enforcement gate.

Each application has its own SecurityProfile built from DB records:
    - AppProfile: global app settings (default policy, risk rules, permissions)
    - AppModuleGrant: per-module visibility and per-action policy overrides

The security system operates at two levels:
    1. VISIBILITY - what the agent sees (GET /api/modules)
    2. EXECUTION - what the agent can do (POST /api/modules/{id}/execute)

Action policies (per action, per app):
    auto    - execute immediately, no confirmation needed
    approve - agent must request user confirmation before execution
    block   - forbidden, even with confirmation

Policy resolution order (first match wins):
    1. action_overrides[action_name]          (explicit per-action override)
    2. risk_approval_rules[action.risk_level] (risk-based rule)
    3. module_grant.default_action_policy     (module-level default)
    4. profile.default_policy                 (app-level default)

Design principles:
    - Every application has its own profile. Nothing is global.
    - If it's declared, it's enforced. No metadata-only decorators.
    - Enforcement lives IN execute(), not beside it.
    - No context = no gate = everything passes (dev/test mode).
    - Errors are structured - the agent knows WHY it was denied.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

from digitorn.modules.exceptions import (
    ApprovalRequiredError,
    PermissionDeniedError,
)

# Ordered from least to most dangerous.
RISK_LEVELS = ("low", "medium", "high")

# Valid action policies.
POLICIES = ("auto", "approve", "block")


def _risk_rank(level: str) -> int:
    """Return numeric rank for a risk level string. Unknown = highest."""
    try:
        return RISK_LEVELS.index(level)
    except ValueError:
        return len(RISK_LEVELS)


# Module grant - per-module permissions for an application


@dataclass(frozen=True)
class ModuleGrant:
    """Per-module security configuration for an application.

    Attributes:
        module_id:             The module this grant applies to.
        visibility:            "full" (agent sees the module) or
                               "hidden" (module is invisible to the agent).
        default_action_policy: Default policy for actions not in overrides.
        action_overrides:      Per-action policy overrides.
                               {"delete_file": "block", "read_file": "auto"}
    """

    module_id: str
    visibility: str = "full"  # "full" | "hidden"
    default_action_policy: str = "approve"  # "auto" | "approve" | "block"
    action_overrides: dict[str, str] = field(default_factory=dict)
    hidden_actions: frozenset[str] = field(default_factory=frozenset)
    # Non-empty set = allow-list: anything outside it falls back to
    # `default_action_policy`. Empty set = no action-level restriction.
    allowed_actions: frozenset[str] = field(default_factory=frozenset)

    def is_visible(self) -> bool:
        return self.visibility != "hidden"

    def is_action_hidden(self, action: str) -> bool:
        """Check if an action is hidden from the agent's tool index."""
        return action in self.hidden_actions

    system_module: bool = False

    def action_policy(self, action: str) -> str | None:
        """Return the explicit policy for an action, or None to fall through."""
        if self.system_module:
            return "auto"
        return self.action_overrides.get(action)


# Security profile - built from DB records, travels in ExecutionContext


@dataclass(frozen=True)
class SecurityProfile:
    """Immutable security profile for an application.

    Built from DB records (AppProfile + AppModuleGrant rows) at request time.
    Travels inside ExecutionContext so every check has full context.

    Attributes:
        app_id:               The application this profile belongs to.
        is_active:            Whether the app is active. Inactive = deny all.
        default_policy:       App-level default action policy.
        granted_permissions:  Explicit permission grants. Supports globs.
        max_risk_level:       Highest risk level the app can handle.
        risk_approval_rules:  Per-risk-level policy mapping.
        module_grants:        Per-module configuration (visibility, action policies).
        is_admin:             Bypass all checks (daemon-internal / CLI).
    """

    app_id: str = ""
    is_active: bool = True
    default_policy: str = "approve"  # "auto" | "approve" | "block"
    granted_permissions: frozenset[str] = field(default_factory=frozenset)
    max_risk_level: str = "medium"
    risk_approval_rules: dict[str, str] = field(default_factory=dict)
    module_grants: dict[str, ModuleGrant] = field(default_factory=dict)
    is_admin: bool = False
    approval_timeout: float = 300.0


    def has_permission(self, permission: str) -> bool:
        """Check if this profile grants the given permission (glob support)."""
        if self.is_admin:
            return True
        return any(
            fnmatch.fnmatch(permission, pattern)
            for pattern in self.granted_permissions
        )

    def can_access_module(self, module_id: str) -> bool:
        """Check if the module can be executed (always True for system modules)."""
        if self.is_admin:
            return True
        if not self.is_active:
            return False
        grant = self.module_grants.get(module_id)
        if grant is not None and not grant.is_visible():
            return False
        return True

    def is_module_visible(self, module_id: str) -> bool:
        """Check if the module should appear in the agent's tool index."""
        grant = self.module_grants.get(module_id)
        if grant is not None:
            return grant.is_visible()
        return True

    def can_handle_risk(self, risk_level: str) -> bool:
        """Check if the action's risk level is within the profile's max."""
        if self.is_admin:
            return True
        return _risk_rank(risk_level) <= _risk_rank(self.max_risk_level)


    def visible_modules(self, all_module_ids: list[str]) -> list[str]:
        """Filter module IDs to only those visible to this app."""
        if self.is_admin:
            return all_module_ids
        return [mid for mid in all_module_ids if self.is_module_visible(mid)]

    def visible_actions(self, module_id: str, all_actions: list[str]) -> list[str]:
        """Filter action names to only those the agent should see."""
        if self.is_admin:
            return all_actions
        if not self.is_module_visible(module_id):
            return []
        grant = self.module_grants.get(module_id)
        if grant is None:
            return all_actions if self.default_policy != "block" else []
        result = []
        for action in all_actions:
            if grant.is_action_hidden(action):
                continue
            policy = grant.action_policy(action)
            if policy == "block":
                continue
            if policy is None and self.default_policy == "block":
                continue
            result.append(action)
        return result

    def action_policy_info(
        self, module_id: str, action: str, risk_level: str
    ) -> str:
        """Resolve the effective policy for an action (for visibility endpoint).

        Returns "auto", "approve", or "block".
        """
        if self.is_admin:
            return "auto"
        return resolve_action_policy(self, module_id, action, risk_level)




def resolve_action_policy(
    profile: SecurityProfile | None,
    module_id: str,
    action: str,
    risk_level: str,
) -> str:
    """Resolve the effective policy for a specific action.

    Resolution order (first match wins):
        1. Explicit action_overrides in the module grant (auto/approve/block)
        2. max_risk_level hard gate - if the action's risk > the profile's
           max_risk_level AND the action was not explicitly granted, block it
        3. If the module grant has an explicit action_overrides list, any
           action NOT in that list uses the module's default_action_policy
           (treating the grant list as an allow-list)
        4. risk_approval_rules based on the action's risk level
        5. Module grant's default_action_policy
        6. App profile's default_policy

    Returns:
        "auto", "approve", or "block"
    """
    # `security_profile = None` is the dev/test default; every action
    # runs `auto`.
    if profile is None:
        return "auto"

    if profile.is_admin:
        return "auto"

    grant = profile.module_grants.get(module_id)

    # System modules (context_builder, llm_provider, index) are ALWAYS auto -
    # they must never require approval or be blocked by any policy.
    if grant is not None and grant.system_module:
        return "auto"

    if grant is not None:
        override = grant.action_policy(action)
        if override is not None:
            return override

    # Hard ceiling: actions above `max_risk_level` are blocked unless
    # the override branch above granted them.
    max_risk = getattr(profile, "max_risk_level", "high") or "high"
    if _risk_rank(risk_level or "medium") > _risk_rank(max_risk):
        return "block"

    # Non-empty `allowed_actions` is an allow-list: anything outside
    # it falls to `grant.default_action_policy`.
    if grant is not None and grant.allowed_actions and action not in grant.allowed_actions:
        return grant.default_action_policy

    effective_risk = risk_level or "medium"
    risk_policy = profile.risk_approval_rules.get(effective_risk)
    if risk_policy is not None:
        return risk_policy

    if grant is not None:
        return grant.default_action_policy

    return profile.default_policy


def security_gate(
    *,
    profile: SecurityProfile,
    module_id: str,
    action: str,
    required_permissions: list[str],
    risk_level: str,
    irreversible: bool,
    params: dict[str, Any],
    agent_id: str = "",
    session_id: str = "",
) -> None:
    """Enforce all security checks before action execution.

    Called from BaseModule.execute(). Raises on first violation.
    Every decision is logged to the security audit log.

    Raises:
        PermissionDeniedError: App inactive, module hidden, permission missing,
                               risk level too high, or action policy is "block".
        ApprovalRequiredError: Action policy is "approve" and the caller
                               hasn't sent _approved=True.
    """
    # Infrastructure meta-actions on context_builder are always allowed.
    # They are the tool dispatch layer - the security check applies to the
    # TARGET tool inside execute_tool, not to the dispatcher itself.
    _INFRA_ACTIONS = frozenset({
        "execute_tool", "search_tools", "get_tool", "list_categories",
        "browse_category", "run_parallel",
    })
    if module_id == "context_builder" and action in _INFRA_ACTIONS:
        return

    from digitorn.core.security_audit import log_security_event

    effective_risk = risk_level or "medium"
    _audit_base = dict(
        app_id=profile.app_id, agent_id=agent_id, session_id=session_id,
        module_id=module_id, action=action, risk_level=effective_risk,
        params=params,
    )

    if not profile.is_active and not profile.is_admin:
        log_security_event(**_audit_base, decision="denied", gate="gate0_inactive",
                           reason="App is inactive")
        raise PermissionDeniedError(action=action, module=module_id, profile=profile.app_id)

    _grant = profile.module_grants.get(module_id)

    # System modules (context_builder, llm_provider, index) are ALWAYS allowed -
    # they are internal infrastructure, not user-facing tools.
    _SYSTEM_MODULES = {"context_builder", "llm_provider", "index"}
    if module_id in _SYSTEM_MODULES:
        return
    if _grant is not None and _grant.system_module:
        return

    if not profile.can_access_module(module_id):
        log_security_event(**_audit_base, decision="denied", gate="gate1_module",
                           reason=f"Module '{module_id}' is not accessible")
        raise PermissionDeniedError(action=action, module=module_id, profile=profile.app_id)
    if _grant is not None and _grant.is_action_hidden(action):
        log_security_event(**_audit_base, decision="denied", gate="gate1_hidden",
                           reason=f"Action '{action}' is hidden")
        raise PermissionDeniedError(action=action, module=module_id, profile=profile.app_id)

    action_granted = profile.has_permission(f"{module_id}:{action}")
    action_has_policy = _grant is not None and _grant.action_policy(action) is not None

    if not action_granted and not action_has_policy and not profile.can_handle_risk(effective_risk):
        log_security_event(**_audit_base, decision="denied", gate="gate2_risk",
                           reason=f"Risk '{effective_risk}' exceeds max '{profile.max_risk_level}'")
        raise PermissionDeniedError(action=action, module=module_id, profile=profile.app_id)

    if required_permissions and not action_granted and not action_has_policy:
        missing = [p for p in required_permissions if not profile.has_permission(p)]
        if missing:
            log_security_event(**_audit_base, decision="denied", gate="gate3_permissions",
                               reason=f"Missing permissions: {missing}")
            raise PermissionDeniedError(action=action, module=module_id, profile=profile.app_id)

    policy = resolve_action_policy(profile, module_id, action, effective_risk)

    if policy == "block":
        log_security_event(**_audit_base, decision="denied", gate="gate4_policy",
                           reason="Action policy is 'block'", policy_resolved=policy)
        raise PermissionDeniedError(action=action, module=module_id, profile=profile.app_id)

    if policy == "approve" and not (params or {}).get("_approved"):
        log_security_event(**_audit_base, decision="approval_required", gate="gate4_policy",
                           reason="Action requires user approval", policy_resolved=policy)
        raise ApprovalRequiredError(
            action_id=f"{module_id}.{action}",
            plan_id=(params or {}).get("_plan_id", "unknown"),
        )

    data_classification = params.pop("_data_classification", "") if params else ""
    max_classification = params.pop("_max_data_classification", "") if params else ""
    if data_classification and max_classification:
        from digitorn.core.security_enforcer import check_data_classification
        class_err = check_data_classification(data_classification, max_classification)
        if class_err:
            log_security_event(**_audit_base, decision="denied", gate="gate5_classification",
                               reason=class_err, policy_resolved=policy)
            raise PermissionDeniedError(action=action, module=module_id, profile=profile.app_id)


    rate_limiter = params.pop("_rate_limiter", None) if params else None
    if rate_limiter is not None:
        rate_err = rate_limiter.check(module_id, action)
        if rate_err:
            log_security_event(**_audit_base, decision="denied", gate="gate6_rate_limit",
                               reason=rate_err, policy_resolved=policy)
            raise PermissionDeniedError(action=action, module=module_id, profile=profile.app_id)

    _p = params or {}
    decision = "approved" if _p.get("_approved") else "allowed"
    gate = "gate4_policy" if _p.get("_approved") else "all_gates_passed"
    reason = "User approved" if _p.get("_approved") else f"Policy '{policy}' allows execution"
    log_security_event(**_audit_base, decision=decision, gate=gate,
                       reason=reason, policy_resolved=policy)


def build_profile_from_db(
    app_profile: Any,
    module_grant_rows: list[Any],
) -> SecurityProfile:
    """Build an immutable SecurityProfile from ORM objects.

    Args:
        app_profile:       An AppProfile ORM instance.
        module_grant_rows: List of AppModuleGrant ORM instances.

    Returns:
        A frozen SecurityProfile ready for ExecutionContext.
    """
    grants: dict[str, ModuleGrant] = {}
    for row in module_grant_rows:
        grants[row.module_id] = ModuleGrant(
            module_id=row.module_id,
            visibility=row.visibility or "full",
            default_action_policy=row.default_action_policy or "approve",
            action_overrides=dict(row.action_overrides or {}),
        )

    return SecurityProfile(
        app_id=app_profile.app_id,
        is_active=app_profile.is_active,
        default_policy=app_profile.default_policy or "approve",
        granted_permissions=frozenset(app_profile.granted_permissions or []),
        max_risk_level=app_profile.max_risk_level or "medium",
        risk_approval_rules=dict(
            app_profile.risk_approval_rules
            or {"low": "auto", "medium": "approve", "high": "approve"}
        ),
        module_grants=grants,
    )


async def load_security_profile(app_id: str) -> SecurityProfile | None:
    """Load a SecurityProfile from the database for the given app_id.

    Returns None if no profile exists (app not registered or no profile configured).
    """
    try:
        from digitorn.core.database import _session_factory
        from digitorn.core.models import AppModuleGrant, AppProfile

        if _session_factory is None:
            return None

        from sqlalchemy import select

        async with _session_factory() as session:
            result = await session.execute(
                select(AppProfile).where(AppProfile.app_id == app_id)
            )
            app_profile = result.scalar_one_or_none()
            if app_profile is None:
                return None

            grants_result = await session.execute(
                select(AppModuleGrant).where(
                    AppModuleGrant.profile_id == app_profile.id
                )
            )
            grant_rows = list(grants_result.scalars().all())

        return build_profile_from_db(app_profile, grant_rows)

    except Exception as exc:
        logger.warning("Security profile load failed: %s", exc)
        return None

"""RuntimeApp - pre-computed, immutable app state for zero-overhead execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from digitorn.core.app.compiler import CompiledApp
from digitorn.core.security import SecurityProfile, resolve_action_policy

if TYPE_CHECKING:
    from digitorn.modules.registry import ModuleRegistry


@dataclass(frozen=True)
class ActionPolicyEntry:
    """Pre-resolved policy for a single module:action pair."""

    module_id: str
    action: str
    policy: str
    risk_level: str
    permissions: list[str]
    irreversible: bool


@dataclass(frozen=True)
class RuntimeApp:
    """Immutable, pre-computed app state. Created once, read many times."""

    app_id: str
    security_profile: SecurityProfile

    _constraints: dict[str, dict[str, Any]] = field(default_factory=dict)

    _configs: dict[str, dict[str, Any]] = field(default_factory=dict)

    _action_policies: dict[tuple[str, str], ActionPolicyEntry] = field(
        default_factory=dict
    )

    _visible_actions: dict[str, list[str]] = field(default_factory=dict)

    module_ids: tuple[str, ...] = ()


    def action_policy(self, module_id: str, action: str) -> str:
        """Get the pre-resolved policy for a module:action pair."""
        entry = self._action_policies.get((module_id, action))
        if entry is not None:
            return entry.policy
        if self.security_profile is None:
            return "auto"
        return self.security_profile.default_policy

    def action_policy_entry(
        self, module_id: str, action: str
    ) -> ActionPolicyEntry | None:
        """Get the full pre-resolved entry (policy + risk + permissions)."""
        return self._action_policies.get((module_id, action))

    def module_constraints(self, module_id: str) -> dict[str, Any]:
        """Get runtime constraints for a module. Empty dict if none."""
        return self._constraints.get(module_id, {})

    def module_config(self, module_id: str) -> dict[str, Any]:
        """Get static config for a module. Empty dict if none."""
        return self._configs.get(module_id, {})

    def visible_actions_for(self, module_id: str) -> list[str]:
        """Get the list of visible actions for a module."""
        return self._visible_actions.get(module_id, [])

    def can_execute(self, module_id: str, action: str) -> bool:
        """Quick check: is this action NOT blocked?"""
        policy = self.action_policy(module_id, action)
        return policy != "block"

    def is_module_visible(self, module_id: str) -> bool:
        """Check if a module is visible to the agent."""
        if self.security_profile is None:
            return True
        return self.security_profile.is_module_visible(module_id)


def build_runtime_app(
    compiled: CompiledApp,
    registry: "ModuleRegistry",
) -> RuntimeApp:
    """Build a RuntimeApp by pre-computing all action policies."""
    action_policies: dict[tuple[str, str], ActionPolicyEntry] = {}
    visible_actions: dict[str, list[str]] = {}
    constraints: dict[str, dict[str, Any]] = {}
    configs: dict[str, dict[str, Any]] = {}

    profile = compiled.security_profile

    for module_id, module_config in compiled.modules.items():
        if module_config.constraints:
            constraints[module_id] = module_config.constraints
        if module_config.config:
            configs[module_id] = module_config.config

        try:
            module = registry.get(module_id)
        except Exception:
            continue

        action_registry = getattr(module, "_action_registry", {})
        module_visible: list[str] = []

        for action_name, entry in action_registry.items():
            spec = entry.spec
            risk_level = spec.risk_level or "low"
            permissions = list(spec.permissions) if spec.permissions else []
            irreversible = spec.irreversible

            policy = resolve_action_policy(
                profile, module_id, action_name, risk_level
            )

            action_policies[(module_id, action_name)] = ActionPolicyEntry(
                module_id=module_id,
                action=action_name,
                policy=policy,
                risk_level=risk_level,
                permissions=permissions,
                irreversible=irreversible,
            )

            if profile is None or profile.is_module_visible(module_id):
                grant = profile.module_grants.get(module_id) if profile else None
                if grant is not None and grant.is_action_hidden(action_name):
                    continue
                module_visible.append(action_name)

        visible_actions[module_id] = module_visible

    return RuntimeApp(
        app_id=compiled.app_id,
        security_profile=profile,
        _constraints=constraints,
        _configs=configs,
        _action_policies=action_policies,
        _visible_actions=visible_actions,
        module_ids=tuple(compiled.module_ids),
    )


class AppRuntimeStore:
    """In-memory store of active RuntimeApps. O(1) lookup by app_id."""

    def __init__(self, registry: "ModuleRegistry") -> None:
        self._registry = registry
        self._apps: dict[str, RuntimeApp] = {}

    def register(self, compiled: CompiledApp) -> RuntimeApp:
        """Build and store a RuntimeApp from a CompiledApp."""
        runtime = build_runtime_app(compiled, self._registry)
        self._apps[compiled.app_id] = runtime
        return runtime

    def get(self, app_id: str) -> RuntimeApp | None:
        """Get a RuntimeApp by app_id. O(1). Returns None if not registered."""
        return self._apps.get(app_id)

    def unregister(self, app_id: str) -> bool:
        """Remove a RuntimeApp. Returns True if it existed."""
        return self._apps.pop(app_id, None) is not None

    def list_apps(self) -> list[str]:
        """List all registered app IDs."""
        return list(self._apps.keys())

    @property
    def count(self) -> int:
        """Number of registered apps."""
        return len(self._apps)

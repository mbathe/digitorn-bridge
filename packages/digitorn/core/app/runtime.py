"""RuntimeApp — pre-computed, immutable app state for zero-overhead execution.

After the compiler validates and the bootstrapper executes, the RuntimeApp
captures everything the execution layer needs in a single, frozen object:

    - SecurityProfile          (already resolved, no DB query needed)
    - Per-module constraints   (already validated, ready to enforce)
    - Per-module config        (already applied, kept for reference)
    - Action policy cache      (pre-resolved for every module:action pair)
    - Module references        (direct pointers, no registry lookup)

At runtime, the execution path is::

    request(app_id, module_id, action, params)
        → store.get(app_id)
        → runtime_app.get_policy(mid, a) # O(1) dict lookup
        → runtime_app.get_module(mid)
        → module.execute(action, params) # direct call

Zero reflection. Zero DB queries. Zero lazy loading.

Usage::

    from digitorn.core.app.runtime import AppRuntimeStore

    store = AppRuntimeStore(registry)
    runtime = store.register(compiled_app)

    runtime = store.get("my-app")
    policy = runtime.action_policy("database", "execute_query")
    constraints = runtime.module_constraints("database")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from digitorn.core.app.compiler import CompiledApp
from digitorn.core.security import SecurityProfile, resolve_action_policy

if TYPE_CHECKING:
    from digitorn.modules.registry import ModuleRegistry


@dataclass(frozen=True)
class ActionPolicyEntry:
    """Pre-resolved policy for a single module:action pair.

    Computed once at registration time so the runtime never
    calls resolve_action_policy() on the hot path.
    """

    module_id: str
    action: str
    policy: str
    risk_level: str
    permissions: list[str]
    irreversible: bool


@dataclass(frozen=True)
class RuntimeApp:
    """Immutable, pre-computed app state. Created once, read many times.

    Everything the execution layer needs is here — no reflection,
    no DB queries, no lazy loading.
    """

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
        """Get the pre-resolved policy for a module:action pair.

        Returns "approve" as fallback if the pair wasn't pre-computed
        (shouldn't happen for well-formed apps).
        """
        entry = self._action_policies.get((module_id, action))
        return entry.policy if entry is not None else self.security_profile.default_policy

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
        return self.security_profile.is_module_visible(module_id)


def build_runtime_app(
    compiled: CompiledApp,
    registry: "ModuleRegistry",
) -> RuntimeApp:
    """Build a RuntimeApp by pre-computing all action policies.

    Walks every module and every action, resolves the policy once,
    and freezes the result. After this, the runtime never needs to
    call resolve_action_policy() again.
    """
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

            if profile.is_module_visible(module_id):
                grant = profile.module_grants.get(module_id)
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
    """In-memory store of active RuntimeApps. O(1) lookup by app_id.

    Singleton per daemon process. Holds all bootstrapped apps ready
    for zero-overhead execution.

    Usage::

        store = AppRuntimeStore(registry)
        store.register(compiled_app)

        runtime = store.get("my-app")
        policy = runtime.action_policy("database", "execute_query")
    """

    def __init__(self, registry: "ModuleRegistry") -> None:
        self._registry = registry
        self._apps: dict[str, RuntimeApp] = {}

    def register(self, compiled: CompiledApp) -> RuntimeApp:
        """Build and store a RuntimeApp from a CompiledApp.

        Replaces any existing entry for the same app_id (for re-sync).
        """
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

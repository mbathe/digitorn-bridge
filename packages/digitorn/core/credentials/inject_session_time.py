"""Session-time credential injection.

Counterpart to `inject_deploy_time` for **user-scoped** credentials
(`per_user`, `per_app_per_user`). Runs at the start of every chat
session for the activating user, resolves their refs from the vault,
and **mutates the live module/provider instances** so the agent turn
that follows uses the user's keys.

Why we mutate live instances instead of the compiled config:
  - Deploy-time injection already wrote system_wide + per_app_shared
    fields into `compiled.modules[mid].config`. Modules called
    `on_config_update(...)` once at deploy with that dict, then built
    their internal state (LLM clients, DB pools, etc.).
  - Re-calling `on_config_update` per session would tear down those
    pools and is racy against in-flight turns.
  - Instead, we hot-swap the credential fields on the live instances.
    The legacy `{{secret.X}}` path does the same trick via
    `_override_provider_fields`.

For LLM providers we delegate to the existing
`_override_provider_fields` helper in `session_resolver.py` so a
single mutation strategy is shared with the legacy path.

For other modules with declared slots (MCP servers, DB, channels,
http clients), we currently only update the compiled config dict and
log a warning - the in-place mutation hooks aren't defined yet.
That is fine for the milestone: those modules carry their own runtime
resolver paths.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from digitorn.core.credentials.injector import (
    CredentialInjectError,
    CredentialInjector,
)
from digitorn.core.credentials.schema_yaml import parse_credential_ref
from digitorn.core.credentials.slot import (
    CredentialSlot,
    collect_slots_from_modules,
)

if TYPE_CHECKING:
    from digitorn.core.app.compiler import CompiledApp
    from digitorn.core.credentials.store import CredentialStore

logger = logging.getLogger(__name__)


# Scopes resolved at session time - user-bound only.
SESSION_TIME_SCOPES: tuple[str, ...] = ("per_user", "per_app_per_user")


async def inject_session_time_credentials(
    *,
    compiled: "CompiledApp",
    modules: dict[str, Any],
    credential_store: "CredentialStore | None",
    user_id: str,
) -> dict[str, list[str]]:
    """Resolve every per-user `credential:` ref for the current
    session and apply the values to the live runtime objects.

    Returns a diagnostic dict:
        {"providers": [...resolved provider_ids...],
         "modules":   [...resolved module_ids...]}

    Raises:
        CredentialInjectError when a required user-scoped slot can
            not be resolved.
        CredentialAuthRequired when a grant flow is needed
            (propagated from the underlying store).
    """
    resolved_providers: list[str] = []
    resolved_modules: list[str] = []

    if credential_store is None:
        return {"providers": resolved_providers, "modules": resolved_modules}

    if not user_id:
        return {"providers": resolved_providers, "modules": resolved_modules}

    # Walk per-agent brains.
    for agent in compiled.agents:
        agent_id = getattr(agent, "agent_id", "") or "agent"
        brain = getattr(agent, "brain", None)
        if brain is None:
            continue
        cred_raw = getattr(brain, "credential", None)
        if cred_raw is None:
            continue

        try:
            ref = parse_credential_ref(cred_raw)
        except Exception:
            ref = None
        if ref is None or ref.scope not in SESSION_TIME_SCOPES:
            continue

        try:
            applied = await _apply_brain_credential(
                brain=brain,
                agent_id=agent_id,
                ref_raw=cred_raw,
                modules=modules,
                credential_store=credential_store,
                user_id=user_id,
                app_id=compiled.app_id,
            )
            if applied:
                resolved_providers.append(applied)
        except CredentialInjectError:
            # Required slot unresolved at session time - bubble up.
            raise

    # Walk module blocks.
    for mid, mod_cfg in compiled.modules.items():
        cred_raw = getattr(mod_cfg, "credential", None)
        if cred_raw is None:
            continue
        try:
            ref = parse_credential_ref(cred_raw)
        except Exception:
            ref = None
        if ref is None or ref.scope not in SESSION_TIME_SCOPES:
            continue

        try:
            applied = await _apply_module_credential(
                module_id=mid,
                module_instance=modules.get(mid),
                ref_raw=cred_raw,
                compiled_module_cfg=mod_cfg,
                credential_store=credential_store,
                user_id=user_id,
                app_id=compiled.app_id,
            )
            if applied:
                resolved_modules.append(mid)
        except CredentialInjectError:
            raise

    return {"providers": resolved_providers, "modules": resolved_modules}


# ── Internals ──────────────────────────────────────────────────────


async def _apply_brain_credential(
    *,
    brain: Any,
    agent_id: str,
    ref_raw: Any,
    modules: dict[str, Any],
    credential_store: "CredentialStore",
    user_id: str,
    app_id: str,
) -> str | None:
    """Resolve the brain's credential ref and override the live LLM
    provider instance. Returns the provider_id when successful, else None.
    """
    llm_module = modules.get("llm_provider")
    if llm_module is None:
        return None

    provider_id = getattr(brain, "provider_id", "")
    if not provider_id:
        return None

    # Build a synthetic 1-block view for the injector. We run the
    # injector against an isolated wrapper, then push the resolved
    # fields onto the live provider instance. The wrapper shape
    # `{"config": target_dict}` matches the injector's
    # `{block}.config.<field>` default templates.
    target_dict: dict[str, Any] = {}
    compiled_blocks = {f"agents.{agent_id}.brain": {"config": target_dict}}

    # Inline view: fake AppDefinition with a single agent carrying
    # the brain we're resolving for.
    app_view = _SingleBrainView(agent_id=agent_id, brain=brain, ref_raw=ref_raw)

    # Module slots: pull from the live llm_provider instance if it
    # declares any.
    slot_pairs = collect_slots_from_modules([llm_module])
    module_slots: dict[str, list[CredentialSlot]] = {}
    for mid, slot in slot_pairs:
        module_slots.setdefault(mid, []).append(slot)

    injector = CredentialInjector(
        store=credential_store,
        user_id=user_id,
        app_id=app_id,
        audit=None,  # session-time audits are recorded by the chat route
    )

    await injector.inject_app(
        app_def=app_view,
        module_slots=module_slots,
        compiled_blocks=compiled_blocks,
    )

    if not target_dict:
        return None

    # Push onto the live provider instance.
    live = getattr(llm_module, "_providers", {}).get(provider_id)
    if live is None:
        # Provider not yet spun up - ask the module to configure it.
        configure_fn = getattr(llm_module, "_configure_from_dict", None)
        if configure_fn is not None:
            try:
                await configure_fn(provider_id, target_dict)
                live = getattr(llm_module, "_providers", {}).get(provider_id)
            except Exception as exc:
                logger.warning(
                    "session_time_brain_configure_failed agent=%s provider=%s: %s",
                    agent_id, provider_id, exc,
                )

    if live is None:
        return None

    from digitorn.core.credentials.session_resolver import (
        _override_provider_fields,
    )
    _override_provider_fields(
        live,
        target_dict,
        canonical_provider=target_dict.get("provider")
        or getattr(brain, "provider_id", "") or None,
        agent_id=agent_id,
        provider_id=provider_id,
    )
    logger.info(
        "session_time_brain_credential_applied agent=%s provider=%s",
        agent_id, provider_id,
    )
    return provider_id


async def _apply_module_credential(
    *,
    module_id: str,
    module_instance: Any,
    ref_raw: Any,
    compiled_module_cfg: Any,
    credential_store: "CredentialStore",
    user_id: str,
    app_id: str,
) -> bool:
    """Resolve a non-brain module's credential ref. Currently we
    write the resolved fields into the compiled config dict and
    re-call `on_config_update` IF the module advertises support for
    hot reconfiguration via the `_supports_session_credential_reload`
    flag. Otherwise we log and skip - the legacy resolver paths or
    the module's own runtime lookup will pick up the credential.
    """
    if module_instance is None:
        return False

    # Synthetic 1-block view. Same wrapping pattern as for brains so
    # the injector's `{block}.config.<field>` templates land inside
    # `target_dict`.
    target_dict: dict[str, Any] = dict(compiled_module_cfg.config or {})
    compiled_blocks = {f"modules.{module_id}": {"config": target_dict}}
    app_view = _SingleModuleView(module_id=module_id, ref_raw=ref_raw)

    slot_pairs = collect_slots_from_modules([module_instance])
    module_slots: dict[str, list[CredentialSlot]] = {}
    for mid, slot in slot_pairs:
        module_slots.setdefault(mid, []).append(slot)

    injector = CredentialInjector(
        store=credential_store,
        user_id=user_id,
        app_id=app_id,
        audit=None,
    )
    await injector.inject_app(
        app_def=app_view,
        module_slots=module_slots,
        compiled_blocks=compiled_blocks,
    )

    if not target_dict or target_dict == (compiled_module_cfg.config or {}):
        return False

    if getattr(module_instance, "_supports_session_credential_reload", False):
        try:
            await module_instance.on_config_update(target_dict)
            logger.info(
                "session_time_module_credential_applied module=%s",
                module_id,
            )
            return True
        except Exception as exc:
            logger.warning(
                "session_time_module_reconfigure_failed module=%s: %s",
                module_id, exc,
            )
            return False

    # Module doesn't support hot reload - rely on its runtime path.
    logger.info(
        "session_time_module_credential_resolved_no_hotreload module=%s",
        module_id,
    )
    return False


# ── Tiny duck-typed views the injector consumes ────────────────────


class _SingleBrainView:
    def __init__(self, *, agent_id: str, brain: Any, ref_raw: Any) -> None:
        self._agent_id = agent_id
        self._brain = brain
        self._ref_raw = ref_raw

    @property
    def brain(self) -> Any:
        return None

    @property
    def agents(self) -> list[Any]:
        return [_SingleAgentView(self._agent_id, self._brain, self._ref_raw)]

    @property
    def modules(self) -> dict[str, Any]:
        return {}


class _SingleAgentView:
    def __init__(self, agent_id: str, brain: Any, ref_raw: Any) -> None:
        self.id = agent_id
        self._brain = brain
        self._ref_raw = ref_raw

    @property
    def brain(self) -> Any:
        return _BrainProxy(self._brain, credential=self._ref_raw)


class _BrainProxy:
    def __init__(self, inner: Any, credential: Any) -> None:
        self._inner = inner
        self.credential = credential

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _SingleModuleView:
    def __init__(self, *, module_id: str, ref_raw: Any) -> None:
        self._mid = module_id
        self._ref_raw = ref_raw

    @property
    def brain(self) -> Any:
        return None

    @property
    def agents(self) -> list[Any]:
        return []

    @property
    def modules(self) -> dict[str, Any]:
        return {self._mid: _ModuleBlockProxy(self._ref_raw)}


class _ModuleBlockProxy:
    def __init__(self, ref_raw: Any) -> None:
        self.credential = ref_raw

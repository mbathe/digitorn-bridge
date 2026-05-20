"""Session-time credential injection for user-scoped credentials (per_user, per_app_per_user)."""

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
    audit: Any = None,
    byok_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, list[str]]:
    """Resolve every per-user `credential:` ref for the current session and apply to live runtime objects."""
    overrides = byok_overrides or {}
    resolved_providers: list[str] = []
    resolved_modules: list[str] = []

    logger.info(
        "session_time_inject_start user=%s app=%s agents=%d",
        user_id, getattr(compiled, "app_id", "?"), len(compiled.agents),
    )
    if credential_store is None:
        return {"providers": resolved_providers, "modules": resolved_modules}

    try:
        from digitorn.core.config import get_settings as _get_settings
        if _get_settings().mode == "cloud":
            return {"providers": resolved_providers, "modules": resolved_modules}
    except Exception as exc:
        logger.debug("inject_session_time best-effort block failed: %s", exc)

    if not user_id:
        return {"providers": resolved_providers, "modules": resolved_modules}

    behavior = getattr(compiled, "behavior", None)
    if behavior is not None:
        bbrain = getattr(behavior, "brain", None)
        if bbrain is not None:
            cred_raw = getattr(bbrain, "credential", None)
            if cred_raw is None:
                cred_raw = overrides.get("behavior")
            if cred_raw is not None:
                try:
                    ref = parse_credential_ref(cred_raw)
                except Exception:
                    ref = None
                if ref is not None and ref.scope in SESSION_TIME_SCOPES:
                    try:
                        applied_n = await _apply_inline_provider_credential(
                            brain=bbrain,
                            block_path="behavior.brain",
                            ref_raw=cred_raw,
                            modules=modules,
                            credential_store=credential_store,
                            user_id=user_id,
                            app_id=compiled.app_id,
                            audit=audit,
                        )
                        if applied_n:
                            resolved_providers.append("behavior_brain")
                    except CredentialInjectError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "session_time_behavior_unexpected exc=%r", exc,
                        )

    # skip brain credential resolution when gateway routing will handle auth via JWT; module credentials still resolve.
    gateway_will_route = _gateway_will_route_for_brains(
        user_id=user_id,
        app_id=getattr(compiled, "app_id", ""),
        byok_overrides=overrides,
    )

    # Walk per-agent brains.
    for agent in compiled.agents:
        agent_id = getattr(agent, "agent_id", "") or "agent"
        brain = getattr(agent, "brain", None)
        if brain is None:
            continue
        if (
            gateway_will_route
            and agent_id not in overrides
            and not _brain_is_local(brain)
        ):
            continue
        cred_raw = getattr(brain, "credential", None)
        # BYOK is only a fallback - YAML-declared credential always wins.
        is_byok_synthetic = False
        if cred_raw is None and agent_id in overrides:
            cred_raw = overrides[agent_id]
            is_byok_synthetic = True
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
                audit=audit,
            )
            if applied:
                resolved_providers.append(applied)
            elif is_byok_synthetic:
                # BYOK on with no stored credential -> open the picker by raising CredentialAuthRequired.
                from digitorn.core.credentials.store import (
                    CredentialAuthRequired,
                )
                provider_name = (ref.provider or "").lower() or "unknown"
                raise CredentialAuthRequired(
                    provider=provider_name,
                    provider_type="api_key",
                    app_id=compiled.app_id,
                    user_id=user_id,
                    candidates=[],
                    field_spec={
                        "ref": ref.ref,
                        "scope": ref.scope,
                        "byok": True,
                        "agent_id": agent_id,
                        "model": getattr(brain, "model", "")
                        or (
                            (getattr(brain, "inline_config", {}) or {}).get(
                                "model"
                            )
                            if hasattr(brain, "inline_config") else ""
                        )
                        or "",
                    },
                )
        except CredentialInjectError:
            raise
        except Exception as exc:
            # re-raise CredentialAuthRequired so the route handler can transform it into the structured picker event.
            from digitorn.core.credentials.store import (
                CredentialAuthRequired,
            )
            if isinstance(exc, CredentialAuthRequired):
                raise
            logger.warning(
                "session_time_brain_unexpected agent=%s exc=%r",
                agent_id, exc,
            )

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
                audit=audit,
            )
            if applied:
                resolved_modules.append(mid)
        except CredentialInjectError:
            raise

    return {"providers": resolved_providers, "modules": resolved_modules}


def _gateway_will_route_for_brains(
    *, user_id: str, app_id: str, byok_overrides: dict[str, Any],
) -> bool:
    """Mirror of `gateway_resolver.resolve_session_provider` for the"""
    from digitorn.core.credentials.gateway_resolver import NON_USER_IDS
    if (user_id or "").strip().lower() in NON_USER_IDS:
        return False
    if byok_overrides:
        return False
    try:
        from digitorn.core.config import get_settings as _get_settings
        if not getattr(_get_settings().runtime, "gateway_enabled", True):
            return False
    except Exception:
        # Config unavailable in some test paths - safe default is to
        # KEEP resolving (legacy behaviour).
        return False
    return True


def _brain_is_local(brain: Any) -> bool:
    """True when the brain's provider is local-only (ollama, lm_studio,"""
    from digitorn.core.credentials.gateway_resolver import LOCAL_PROVIDERS
    name = ""
    direct = getattr(brain, "provider", None)
    if direct:
        name = str(direct).lower()
    else:
        inline = getattr(brain, "inline_config", None)
        if isinstance(inline, dict):
            name = str(inline.get("provider") or "").lower()
    return name in LOCAL_PROVIDERS


async def _apply_inline_provider_credential(
    *,
    brain: Any,
    block_path: str,
    ref_raw: Any,
    modules: dict[str, Any],
    credential_store: "CredentialStore",
    user_id: str,
    app_id: str,
    audit: Any = None,
) -> int:
    """Resolve the credential and override every live LLM provider"""
    llm_module = modules.get("llm_provider")
    if llm_module is None:
        return 0
    canonical = (
        getattr(brain, "provider", "")
        or getattr(brain, "provider_id", "")
        or ""
    )
    if not canonical:
        return 0

    # Resolve via the same path as agent brains (1-block view).
    target_dict: dict[str, Any] = {}
    compiled_blocks = {block_path: {"config": target_dict}}

    class _OneBrainView:
        def __init__(self) -> None:
            self.brain = None
            self.modules = {}
            self.agents = [self]  # so the walker iterates this entry
            self.id = "behavior"

        def __getattr__(self, name: str) -> Any:
            if name == "brain":
                return _BrainProxy(brain, credential=ref_raw)
            raise AttributeError(name)

    slot_pairs = collect_slots_from_modules([llm_module])
    module_slots: dict[str, list[CredentialSlot]] = {}
    for mid, slot in slot_pairs:
        module_slots.setdefault(mid, []).append(slot)

    injector = CredentialInjector(
        store=credential_store, user_id=user_id,
        app_id=app_id, audit=audit,
    )
    view = _SingleBrainView(
        agent_id="behavior", brain=brain, ref_raw=ref_raw,
    )
    compiled_blocks_remap = {
        "agents.behavior.brain": {"config": target_dict},
    }
    await injector.inject_app(
        app_def=view, module_slots=module_slots,
        compiled_blocks=compiled_blocks_remap,
    )

    if not target_dict:
        return 0

    overridden = 0
    providers = getattr(llm_module, "_providers", {}) or {}
    from digitorn.core.credentials.session_resolver import (
        _override_provider_fields,
    )
    for pid, prov in list(providers.items()):
        hint = getattr(prov, "provider_hint", "") or getattr(prov, "provider_id", "")
        if hint == canonical or pid.startswith("_inline_") or pid.endswith("_brain"):
            try:
                _override_provider_fields(
                    prov, target_dict,
                    canonical_provider=canonical,
                    agent_id=None,
                    provider_id=pid,
                )
                overridden += 1
            except Exception as exc:
                logger.debug("inject_session_time best-effort block failed: %s", exc)
    logger.info(
        "session_time_inline_credential_applied canonical=%s overridden=%d",
        canonical, overridden,
    )
    return overridden


async def _apply_brain_credential(
    *,
    brain: Any,
    agent_id: str,
    ref_raw: Any,
    modules: dict[str, Any],
    credential_store: "CredentialStore",
    user_id: str,
    app_id: str,
    audit: Any = None,
) -> str | None:
    """Resolve the brain's credential ref and override the live LLM"""
    llm_module = modules.get("llm_provider")
    if llm_module is None:
        return None

    provider_id = getattr(brain, "provider_id", "")
    if not provider_id:
        return None

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
        audit=audit,
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
    audit: Any = None,
) -> bool:
    """Resolve a non-brain module's credential ref. Currently we"""
    if module_instance is None:
        return False

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
        audit=audit,
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

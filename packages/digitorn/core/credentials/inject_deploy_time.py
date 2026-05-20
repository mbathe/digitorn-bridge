"""Deploy-time credential injection."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from digitorn.core.credentials.injector import (
    CredentialInjectError,
    CredentialInjector,
)
from digitorn.core.credentials.schema_yaml import parse_credential_ref
from digitorn.core.credentials.slot import CredentialSlot

if TYPE_CHECKING:
    from digitorn.core.app.compiler import CompiledApp
    from digitorn.core.credentials.audit import AuditLog
    from digitorn.core.credentials.store import CredentialStore

logger = logging.getLogger(__name__)


# Scopes that can be resolved without a live user session.
DEPLOY_TIME_SCOPES: tuple[str, ...] = ("system_wide", "per_app_shared")
# Scopes that can ONLY be resolved with a live user session.
SESSION_TIME_SCOPES: tuple[str, ...] = ("per_user", "per_app_per_user")


async def inject_deploy_time_credentials(
    compiled: "CompiledApp",
    *,
    store: "CredentialStore | None",
    scopes: tuple[str, ...] = DEPLOY_TIME_SCOPES,
    audit: "AuditLog | None" = None,
    module_slots: dict[str, list[CredentialSlot]] | None = None,
) -> list[dict[str, Any]]:
    """Resolve every deploy-visible `credential:` ref in the compiled"""
    if store is None:
        return []

    # Build the synthetic adapter the injector expects.
    app_view = _CompiledAppView(compiled)

    # Map block_path -> mutable config dict the injector should patch.
    compiled_blocks = _build_compiled_blocks(compiled)

    filtered_view = _ScopeFilteredView(app_view, allowed_scopes=set(scopes))

    injector = CredentialInjector(
        store=store,
        # No user context at deploy. The store lookup uses None for
        # `system_wide`, app_id-only for `per_app_shared`.
        user_id="",
        app_id=compiled.app_id,
        audit=audit,
    )

    try:
        return await injector.inject_app(
            app_def=filtered_view,
            module_slots=module_slots or {},
            compiled_blocks=compiled_blocks,
        )
    except CredentialInjectError:
        # Required slot couldn't resolve. Re-raise so the deploy
        # path can surface a structured error to the caller.
        raise
    except Exception as exc:
        # Soft-fail on unexpected errors. We don't want a vault
        # outage to take down all app deploys.
        logger.warning(
            "deploy_time_credential_inject_failed app=%s: %s",
            compiled.app_id, exc, exc_info=True,
        )
        return []


class _CompiledAppView:
    """Adapter that exposes a `CompiledApp` as the duck-typed shape"""

    def __init__(self, compiled: "CompiledApp") -> None:
        self._compiled = compiled

    @property
    def brain(self) -> Any:
        # Compiled apps never have a top-level brain; return None so
        # the injector's brain branch is a no-op.
        return None

    @property
    def agents(self) -> list[Any]:
        return [_CompiledAgentView(a) for a in self._compiled.agents]

    @property
    def modules(self) -> dict[str, Any]:
        return self._compiled.modules


class _CompiledAgentView:
    """Adapter exposing `CompiledAgent` as `{id, brain}`."""

    def __init__(self, compiled_agent: Any) -> None:
        self._a = compiled_agent

    @property
    def id(self) -> str:
        return getattr(self._a, "agent_id", "") or "agent"

    @property
    def brain(self) -> Any:
        return getattr(self._a, "brain", None)


class _ScopeFilteredView:
    """Wraps a view and strips `credential` refs whose declared scope"""

    def __init__(
        self,
        inner: _CompiledAppView,
        *,
        allowed_scopes: set[str],
    ) -> None:
        self._inner = inner
        self._allowed = allowed_scopes

    @property
    def brain(self) -> Any:
        b = self._inner.brain
        return _wrap_block_credential_filter(b, self._allowed)

    @property
    def agents(self) -> list[Any]:
        out: list[Any] = []
        for a in self._inner.agents:
            out.append(_FilteredAgent(a, self._allowed))
        return out

    @property
    def modules(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for mid, blk in self._inner.modules.items():
            out[mid] = _wrap_block_credential_filter(blk, self._allowed)
        return out


class _FilteredAgent:
    def __init__(self, inner: Any, allowed: set[str]) -> None:
        self._a = inner
        self._allowed = allowed

    @property
    def id(self) -> str:
        return getattr(self._a, "id", "") or "agent"

    @property
    def brain(self) -> Any:
        b = getattr(self._a, "brain", None)
        return _wrap_block_credential_filter(b, self._allowed)


class _BlockProxy:
    """Cheap wrapper that exposes a block's attributes plus an"""

    def __init__(self, inner: Any, credential: Any) -> None:
        self._inner = inner
        self.credential = credential

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _wrap_block_credential_filter(
    block: Any, allowed: set[str],
) -> Any:
    """Return a proxy where `.credential` is the original ref if its"""
    if block is None:
        return None
    raw = getattr(block, "credential", None)
    if raw is None:
        return block
    try:
        ref = parse_credential_ref(raw)
    except Exception:
        # Malformed credential YAML: drop silently here, the compiler
        # already validated structurally.
        ref = None
    if ref is None:
        return block
    if ref.scope not in allowed:
        return _BlockProxy(block, credential=None)
    # Allowed: keep raw value (str or dict). The injector re-parses.
    return block


def _build_compiled_blocks(
    compiled: "CompiledApp",
) -> dict[str, dict[str, Any]]:
    """Map every block_path the injector may target to a wrapper"""
    blocks: dict[str, dict[str, Any]] = {}

    # Modules: wrap so `{block}.config.<field>` lands inside .config.
    for mid, mod_cfg in compiled.modules.items():
        if mod_cfg.config is None:
            mod_cfg.config = {}
        blocks[f"modules.{mid}"] = {"config": mod_cfg.config}

    # Per-agent brains: same wrapping over the live provider config.
    llm_cfg = (
        compiled.modules.get("llm_provider").config
        if "llm_provider" in compiled.modules
        else None
    )
    providers_cfg = (
        llm_cfg.get("providers", {}) if isinstance(llm_cfg, dict) else {}
    )

    for agent in compiled.agents:
        agent_id = getattr(agent, "agent_id", "") or "agent"
        brain = getattr(agent, "brain", None)
        if brain is None:
            continue
        provider_id = getattr(brain, "provider_id", "")
        if not provider_id:
            continue
        if isinstance(providers_cfg, dict):
            target = providers_cfg.setdefault(provider_id, {})
            blocks[f"agents.{agent_id}.brain"] = {"config": target}

    return blocks

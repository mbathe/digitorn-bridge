"""Deploy-time credential injection.

Runs ONCE per app deploy, AFTER the compile and BEFORE bootstrap.
Walks the compiled app for `credential:` references at the
**deploy-visible scopes** (`system_wide`, `per_app_shared`),
resolves them against the vault, and mutates the compiled module +
inline-brain configs in place so the bootstrap layer sees fully
filled-in configs (no template strings, no missing api_keys).

Per-user scopes (`per_user`, `per_app_per_user`) are deliberately
**skipped here** - they have no user context at deploy. Those are
applied at session-start by `session_resolver` (parallel to the
legacy `{{secret.X}}` runtime resolver).

Failure policy:
  - **Required slot, missing/expired credential** -> raise
    `CredentialInjectError`. Activation aborts.
  - **Optional slot, missing credential** -> log a warning and skip.
    The module sees its YAML inline values (or empty fields) and
    decides whether to fail at `on_config_update` time.
  - **Store unavailable (None)** -> full no-op so dev paths without
    a configured store keep deploying legacy apps unchanged.

The injector itself is unchanged - this module is a thin orchestrator
that:
  1. builds the synthetic `app_def`-shaped view from the CompiledApp
     (so the existing `_collect_refs` walks the new dataclasses),
  2. assembles the `compiled_blocks` map pointing the injector at the
     LIVE config dicts to mutate (per-module config, inline-brain
     provider config),
  3. filters out refs whose declared scope isn't in the deploy-time
     set,
  4. delegates to `CredentialInjector.inject_app(...)`.
"""

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
    """Resolve every deploy-visible `credential:` ref in the compiled
    app, mutating the module + inline-brain config dicts in place.

    Returns a diagnostic list of injection records (one per resolved
    ref). Empty list when nothing to inject. Never carries plaintext.

    No-op when `store` is None.
    """
    if store is None:
        return []

    # Build the synthetic adapter the injector expects.
    app_view = _CompiledAppView(compiled)

    # Map block_path -> mutable config dict the injector should patch.
    compiled_blocks = _build_compiled_blocks(compiled)

    # Filter pass: only keep refs whose declared scope is in `scopes`.
    # We strip the rest BEFORE the injector sees them so the injector
    # never tries to vault-lookup a per_user credential at deploy.
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


# ── Adapters ───────────────────────────────────────────────────────


class _CompiledAppView:
    """Adapter that exposes a `CompiledApp` as the duck-typed shape
    the existing `CredentialInjector._collect_refs` expects.

    Notable shape differences vs the parsed `AppDefinition`:
      - `CompiledAgent.agent_id` (not `id`).
      - No top-level `brain` (compiled apps only have per-agent brains).
      - `compiled.modules[mid]` is a `CompiledModuleConfig` with
        `.credential` (added in the compiler patch); duck-types fine.
    """

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
    """Wraps a view and strips `credential` refs whose declared scope
    is not in the allowed set.

    Implementation: the wrapper replicates the underlying shape but
    with `.credential = None` on any block whose scope is filtered
    out. The injector then walks the trimmed view and never sees the
    out-of-scope refs.
    """

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
    """Cheap wrapper that exposes a block's attributes plus an
    overridden `.credential` field (None when filtered out)."""

    def __init__(self, inner: Any, credential: Any) -> None:
        self._inner = inner
        self.credential = credential

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _wrap_block_credential_filter(
    block: Any, allowed: set[str],
) -> Any:
    """Return a proxy where `.credential` is the original ref if its
    declared scope is allowed, else `None`. When the block has no
    credential, return it unchanged."""
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


# ── Block path -> config dict map ──────────────────────────────────


def _build_compiled_blocks(
    compiled: "CompiledApp",
) -> dict[str, dict[str, Any]]:
    """Map every block_path the injector may target to a wrapper
    dict whose `"config"` key points at the LIVE config dict to
    mutate.

    The wrapper is required because handler `inject_path_default`
    templates use the form `{block}.config.<field>` (so they can be
    used from YAML-level paths). With a wrapper of shape
    `{"config": <live_dict>}`, `_set_dotted("config.api_key", v)`
    lands inside `<live_dict>` directly, mutating the real config in
    place.

    Block paths produced by `CredentialInjector._collect_refs`:
      - `"brain"`              -> top-level brain (no-op for compiled apps)
      - `"agents.<id>.brain"`  -> wraps the live provider config in
                                  `compiled.modules[llm_provider]
                                  .config[providers][<provider_id>]`
      - `"modules.<id>"`       -> wraps `compiled.modules[id].config`
    """
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

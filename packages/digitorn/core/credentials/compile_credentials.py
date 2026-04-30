"""Compile-time credential validation + manifest generation.

Walks an `AppDefinition` after Pydantic parsing, extracts every
`credential:` reference, and validates them against:

  1. The module's `CredentialSlot` (when the consumer is a module).
  2. The handler's `allowed_scopes` (the scope must be permitted by
     the chosen handler_type).
  3. The provider catalog (when `credential.provider` is set, the
     name must exist in the catalog and its handler_type must match).

Produces a `CredentialsAppManifest` describing every credential need,
with the declared reference (if any) and the slot constraints. The
runtime injector + the per-app-config UI consume this manifest.

The compiler is **structural-only** at this stage: it doesn't query
the user's vault. Whether a referenced credential actually exists is
checked at activation time by the runtime, against the activating
user. That separation lets the same compiled app be deployed to many
users, each with their own vault.
"""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.credentials.catalog import default_catalog
from digitorn.core.credentials.handler import default_registry
from digitorn.core.credentials.schema_yaml import (
    CredentialReference,
    CredentialsAppManifest,
    CredentialsManifestEntry,
    parse_credential_ref,
)
from digitorn.core.credentials.slot import CredentialSlot

logger = logging.getLogger(__name__)


class CredentialCompileError(Exception):
    """Raised when an app.yaml's credential bindings are inconsistent.

    Examples that trigger this:
      - `credential.scope: system_wide` on an OAuth2 slot (handler
        only allows per_user).
      - `credential.provider: openai` but the slot whitelisted
        ['anthropic', 'deepseek'].
      - Reference to a provider name that doesn't exist in the catalog.
    """


def validate_app_credentials(
    app_def: Any,
    *,
    module_slots: dict[str, list[CredentialSlot]] | None = None,
) -> CredentialsAppManifest:
    """Top-level entry: validate every credential ref in the app and
    build the manifest.

    Args:
      app_def: parsed `AppDefinition` (the Pydantic model).
      module_slots: optional pre-collected `{module_id: [slots, ...]}`
        mapping. When None, the compiler tries to instantiate each
        module's class and read `credential_slots`. Pass an explicit
        map when calling from a context that already has the modules
        live (bootstrap, runtime).

    Returns:
      `CredentialsAppManifest` listing every credential need.

    Raises:
      `CredentialCompileError` on any unresolvable structural issue.
    """
    manifest = CredentialsAppManifest(app_id=getattr(app_def, "id", "") or "")

    # ── Walk every block that can carry a `credential:` ref ──────────
    seen_paths: set[str] = set()

    # Top-level brain (single-agent apps).
    brain = getattr(app_def, "brain", None)
    if brain is not None:
        _validate_block(
            block_path="brain",
            block=brain,
            module_id="llm_provider",
            module_slots=module_slots,
            manifest=manifest,
            seen=seen_paths,
        )

    # Per-agent brains. AppDefinition agents carry `.id`; CompiledAgent
    # carries `.agent_id`. Accept both.
    for agent in (getattr(app_def, "agents", None) or []):
        agent_id = (
            getattr(agent, "id", "") or getattr(agent, "agent_id", "")
            or "agent"
        )
        agent_brain = getattr(agent, "brain", None)
        if agent_brain is not None:
            _validate_block(
                block_path=f"agents.{agent_id}.brain",
                block=agent_brain,
                module_id="llm_provider",
                module_slots=module_slots,
                manifest=manifest,
                seen=seen_paths,
            )

    # Modules block - dict of {module_id: ModuleBlock}.
    modules = getattr(app_def, "modules", None) or {}
    for module_id, mod_block in (modules.items() if isinstance(modules, dict) else []):
        if mod_block is None:
            continue
        _validate_block(
            block_path=f"modules.{module_id}",
            block=mod_block,
            module_id=module_id,
            module_slots=module_slots,
            manifest=manifest,
            seen=seen_paths,
        )

    # ── Aggregate flags ──────────────────────────────────────────────
    missing = [
        e.block for e in manifest.entries
        if e.required and e.ref is None
    ]
    manifest.missing_required = missing
    manifest.all_required_resolved = not missing

    # ── Backward-compat warning ──────────────────────────────────────
    # Walk every block one more time looking for ``{{secret.X}}`` /
    # ``{{env.X}}`` templates that were NOT accompanied by a
    # ``credential:`` declaration. We don't reject (legacy still works
    # via the runtime resolver) but we tell the operator their YAML is
    # using the deprecated path so they can run the migration CLI.
    legacy_only_blocks = _detect_legacy_only_blocks(app_def)
    if legacy_only_blocks:
        logger.warning(
            "credential_yaml_legacy_only blocks=%s - "
            "consider running `digitorn yaml migrate-credentials <file>` "
            "to bind these to the new declarative `credential:` block.",
            legacy_only_blocks,
        )

    return manifest


def _detect_legacy_only_blocks(app_def: Any) -> list[str]:
    """Return paths of blocks that use ``{{secret.X}}`` / ``{{env.X}}``
    templates without an accompanying ``credential:`` ref.

    Walks brain (top-level + per-agent) and module configs. Detection
    is purely string-based on the inline config dict's values - cheap
    and covers the common cases (api_key, base_url, token, ...).
    """
    import re as _re
    pattern = _re.compile(r"\{\{\s*(?:env|secret)\.[A-Za-z0-9_.\-]+\s*\}\}")

    def _block_has_template(block: Any) -> bool:
        if block is None:
            return False
        cfg = getattr(block, "config", None)
        if not isinstance(cfg, dict):
            return False
        for v in cfg.values():
            if isinstance(v, str) and pattern.search(v):
                return True
        return False

    def _block_has_credential(block: Any) -> bool:
        return bool(getattr(block, "credential", None))

    out: list[str] = []
    brain = getattr(app_def, "brain", None)
    if brain is not None and _block_has_template(brain) and not _block_has_credential(brain):
        out.append("brain")
    for agent in (getattr(app_def, "agents", None) or []):
        ab = getattr(agent, "brain", None)
        if ab is None:
            continue
        if _block_has_template(ab) and not _block_has_credential(ab):
            out.append(f"agents.{getattr(agent, 'id', '?')}.brain")
    modules = getattr(app_def, "modules", None) or {}
    if isinstance(modules, dict):
        for mid, mb in modules.items():
            if mb is None:
                continue
            if _block_has_template(mb) and not _block_has_credential(mb):
                out.append(f"modules.{mid}")
    return out


def _validate_block(
    *,
    block_path: str,
    block: Any,
    module_id: str,
    module_slots: dict[str, list[CredentialSlot]] | None,
    manifest: CredentialsAppManifest,
    seen: set[str],
) -> None:
    """Validate one block's `credential` ref + add a manifest entry."""
    if block_path in seen:
        return
    seen.add(block_path)

    raw = getattr(block, "credential", None)
    try:
        ref = parse_credential_ref(raw)
    except Exception as exc:
        raise CredentialCompileError(
            f"{block_path}: invalid credential reference: {exc}",
        ) from exc

    # Look up the module's slots. We accept missing slots silently -
    # a module that doesn't declare any `credential_slots` simply
    # won't have an entry in the manifest, even if the YAML named one
    # (we'll surface that mismatch via warning rather than reject).
    slots = (module_slots or {}).get(module_id) or []
    if not slots:
        # No declared slot for this consumer. If the YAML still binds
        # a credential, it's likely targeting a legacy path (e.g.
        # llm_provider via `brain.config.api_key`). We accept and pass
        # the ref through to the runtime injector, which falls back
        # to `inject_path_default` from the handler's first field.
        slot = _make_implicit_slot(module_id, block_path)
    else:
        # Pick the first slot. Most modules expose a single slot per
        # block; multi-slot modules (rare, e.g. one for primary brain
        # and one for fallback) will need richer matching - future
        # iteration.
        slot = slots[0]

    entry = CredentialsManifestEntry(
        block=block_path,
        slot_id=slot.id,
        label=slot.label or slot.id,
        handler_types=list(slot.handler_types),
        providers_accepted=list(slot.providers),
        scopes_preferred=list(slot.scopes_preferred),
        scopes_allowed=(
            list(slot.scopes_allowed) if slot.scopes_allowed is not None else None
        ),
        required=slot.required,
        help=slot.help,
    )

    if ref is not None:
        _validate_ref_against_slot(ref, slot, block_path)
        entry.ref = ref.ref
        entry.ref_scope = ref.scope
        entry.ref_provider = ref.provider

    manifest.entries.append(entry)


def _make_implicit_slot(module_id: str, block_path: str) -> CredentialSlot:
    """Synthesise a permissive slot for blocks whose module doesn't
    declare one. Used as a graceful-degradation path so existing apps
    that bind a credential to e.g. `brain` keep working before we
    migrate every module to declare slots."""
    return CredentialSlot(
        id="default",
        label=f"{module_id} authentication",
        handler_types=[],     # accept any
        providers=[],         # accept any
        scopes_preferred=[],
        scopes_allowed=None,  # inherit from handler
        inject={},            # runtime falls back to handler's defaults
        required=False,       # implicit slots are non-blocking
        help="",
    )


def _validate_ref_against_slot(
    ref: CredentialReference,
    slot: CredentialSlot,
    block_path: str,
) -> None:
    """Structural validation of a ref against the slot constraints +
    the handler's allowed_scopes + the catalog."""
    # 1. Scope must be in the slot's whitelist (when set).
    if slot.scopes_allowed is not None and ref.scope not in slot.scopes_allowed:
        raise CredentialCompileError(
            f"{block_path}.credential.scope={ref.scope!r} is not allowed by "
            f"slot {slot.id!r}. Permitted: {slot.scopes_allowed}",
        )

    # 2. Provider must exist in catalog (when declared).
    if ref.provider:
        tmpl = default_catalog.get(ref.provider)
        if tmpl is None:
            raise CredentialCompileError(
                f"{block_path}.credential.provider={ref.provider!r} is not "
                f"in the catalog. Available: "
                f"{[t.name for t in default_catalog.list_all()]}",
            )
        # 2.b The provider's handler_type must be allowed by the slot
        #     (when the slot enumerates handler_types).
        if slot.handler_types and tmpl.handler_type not in slot.handler_types:
            raise CredentialCompileError(
                f"{block_path}.credential.provider={ref.provider!r} uses "
                f"handler_type={tmpl.handler_type!r}, which is not in the "
                f"slot's allow-list {slot.handler_types}",
            )
        # 2.c Slot can also restrict by provider name explicitly.
        if slot.providers and ref.provider not in slot.providers:
            raise CredentialCompileError(
                f"{block_path}.credential.provider={ref.provider!r} is not "
                f"in the slot's provider whitelist {slot.providers}",
            )
        # 2.d Scope must be in the handler's allowed_scopes.
        try:
            handler = default_registry.get(tmpl.handler_type)
        except KeyError:
            raise CredentialCompileError(
                f"{block_path}: provider {ref.provider!r} declares unknown "
                f"handler_type={tmpl.handler_type!r}",
            )
        if ref.scope not in handler.allowed_scopes:
            raise CredentialCompileError(
                f"{block_path}.credential.scope={ref.scope!r} is not "
                f"compatible with handler {tmpl.handler_type!r} "
                f"(allowed: {handler.allowed_scopes}). "
                f"OAuth tokens are personal - try `scope: per_user`.",
            )

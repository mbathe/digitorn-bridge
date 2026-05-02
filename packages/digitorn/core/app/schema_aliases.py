"""Pre-Pydantic alias pass: reshape legacy (flat) YAML into the canonical
nested shape that :class:`AppDefinition` validates.

The canonical schema (since v2) groups every field under one of seven
top-level blocks:

    app, runtime, agents, tools, security, ui, dev

To avoid breaking existing apps on disk this module rewrites legacy
flat YAMLs (``execution:``, ``modules:``, ``widgets:``, ... at the top
level) into the nested shape before Pydantic validation. The pass is:

  - **Idempotent**: applying twice is a no-op.
  - **Conservative**: when both flat and nested forms are present, the
    nested form wins (the user is telling us "use this exact value").
  - **Loud-on-error**: unknown keys under any block are preserved so
    Pydantic's ``extra: forbid`` surfaces them with their original
    location, not a misleading lifted location.

Every rewrite emits a deprecation warning into the optional
``deprecation_warnings`` list so the CLI can show "your YAML uses
legacy shape, run ``digitorn yaml migrate-v2`` to upgrade".
"""
from __future__ import annotations

from typing import Any


# ─── Field maps ─────────────────────────────────────────────────


# Legacy flat field name -> destination (nested_block, key_in_block).
# The compiler reads from the nested location after this pass runs.
#
# Two renames documented separately below: execution.workspace ->
# runtime.workdir and execution.workspace_mode -> runtime.workdir_mode.
_TOP_LEVEL_TO_NESTED: dict[str, tuple[str, str]] = {
    # → tools
    "modules":         ("tools", "modules"),
    "capabilities":    ("tools", "capabilities"),
    "channels":        ("tools", "channels"),
    # → security
    "behavior":        ("security", "behavior"),
    # → ui
    "widgets":         ("ui", "widgets"),
    "workspace":       ("ui", "workspace"),  # WorkspaceBlock renderer
    "preview":         ("ui", "preview"),
    "theme":           ("ui", "theme"),
    "features":        ("ui", "features"),
    "slash_commands":  ("ui", "slash_commands"),
    # → dev
    "skills":          ("dev", "skills"),
    "variables":       ("dev", "variables"),
    "include":         ("dev", "include"),
    # → runtime (lifted from top-level)
    "middleware":      ("runtime", "middleware"),
    "pipeline":        ("runtime", "pipeline"),
    # NOTE: ``flow`` is NOT in this map - since v2 it's a top-level
    # block of its own. Legacy ``runtime.flow`` is lifted by the
    # _migrate_runtime_flow_to_top_level helper below.
}


# Sub-fields of legacy `execution:` and where they go in the new shape.
# Most stay under `runtime:`; a few move to security or ui.
#
# The two renames (workspace -> workdir, workspace_mode -> workdir_mode)
# happen here so the legacy field name disappears entirely. Renaming
# avoids the `runtime.workspace` (path) vs `ui.workspace` (renderer)
# clash that would otherwise reintroduce the same confusion we set out
# to remove.
_EXECUTION_SUBFIELDS: dict[str, tuple[str, str]] = {
    # security moves
    "sandbox":             ("security", "sandbox"),
    "credentials_schema":  ("security", "credentials_schema"),
    # ui move (greeting is pure display)
    "greeting":            ("ui", "greeting"),
    # runtime renames
    "workspace":           ("runtime", "workdir"),
    "workspace_mode":      ("runtime", "workdir_mode"),
}


# Phase 9 `dependencies:` block destinations - split into the new
# nested locations.
_DEPENDENCIES_TO_NESTED: dict[str, tuple[str, str]] = {
    "variables":         ("dev",      "variables"),
    "channels":          ("tools",    "channels"),
    "credentials":       ("security", "credentials_schema"),
    "credentials_schema":("security", "credentials_schema"),
    "payload":           ("runtime",  "payload_schema"),
    "payload_schema":    ("runtime",  "payload_schema"),
}


# ─── Public entry point ─────────────────────────────────────────


def apply_schema_aliases(
    raw: dict[str, Any], *, deprecation_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Return ``raw`` reshaped into the canonical nested form.

    The canonical AppDefinition expects 7 top-level fields:
    ``app, runtime, agents, tools, security, ui, dev``. Anything else
    at the top level is legacy: this function lifts it into the right
    nested home.

    When ``deprecation_warnings`` is provided, each legacy form
    encountered appends a one-line migration hint suitable for CLI
    display.
    """
    if not isinstance(raw, dict):
        return raw

    out = dict(raw)
    warn = deprecation_warnings if deprecation_warnings is not None else None

    # 1. Phase 9 `runtime:` block was already used as a synonym of
    # ``execution:`` in the previous pass. The new canonical IS
    # ``runtime:`` so nothing to do for the renames - just merge any
    # legacy ``execution:`` block into runtime.
    _merge_execution_into_runtime(out, warn)

    # 1a. Lift legacy ``runtime.flow`` to top-level ``flow:`` (v2
    # promoted flow to its own block).
    _migrate_runtime_flow_to_top_level(out, warn)

    # 1b. Even when the user wrote a ``runtime:`` block directly (new
    # shape), they might still place fields where the legacy schema
    # expected them - e.g. `runtime.greeting` instead of `ui.greeting`,
    # or `runtime.sandbox` instead of `security.sandbox`. Lift those.
    _move_misplaced_runtime_fields(out, warn)

    # 2. Top-level legacy fields → nested blocks.
    for legacy_key, (block, target_key) in _TOP_LEVEL_TO_NESTED.items():
        if legacy_key not in out:
            continue
        value = out.pop(legacy_key)
        block_dict = out.setdefault(block, {})
        if not isinstance(block_dict, dict):
            # The user wrote `tools: [some, list]` - leave it alone so
            # Pydantic emits a clean type error.
            continue
        if target_key in block_dict:
            # Nested form wins - drop the legacy duplicate silently.
            if warn is not None:
                warn.append(
                    f"Legacy `{legacy_key}:` ignored - "
                    f"`{block}.{target_key}` already set with the canonical form."
                )
            continue
        block_dict[target_key] = value
        if warn is not None:
            warn.append(
                f"Legacy top-level `{legacy_key}:` lifted to `{block}.{target_key}`. "
                f"Migrate with `digitorn yaml migrate-v2`."
            )

    # 3. `dependencies:` block - split into runtime/tools/security/dev.
    deps = out.pop("dependencies", None)
    if isinstance(deps, dict):
        leftover: dict[str, Any] = {}
        for k, v in deps.items():
            dest = _DEPENDENCIES_TO_NESTED.get(k)
            if dest is None:
                leftover[k] = v
                continue
            block, target_key = dest
            block_dict = out.setdefault(block, {})
            if isinstance(block_dict, dict) and target_key not in block_dict:
                block_dict[target_key] = v
        if leftover:
            # Unknown keys retained at top level so Pydantic surfaces them.
            out["dependencies"] = leftover

    # 4. `app.features` / `app.theme` - deprecated nesting. Lift to ui.
    app_block = out.get("app")
    if isinstance(app_block, dict):
        for legacy_key in ("features", "theme"):
            if legacy_key in app_block:
                ui_block = out.setdefault("ui", {})
                if isinstance(ui_block, dict) and legacy_key not in ui_block:
                    ui_block[legacy_key] = app_block.pop(legacy_key)
                    if warn is not None:
                        warn.append(
                            f"`app.{legacy_key}` is deprecated. Moved to "
                            f"`ui.{legacy_key}`. Run `digitorn yaml migrate-v2`."
                        )

    # 5. Per-agent: keep coordination / instructions sub-block aliases.
    agents = out.get("agents")
    if isinstance(agents, list):
        out["agents"] = [
            _apply_agent_aliases(a) if isinstance(a, dict) else a
            for a in agents
        ]

    return out


# ─── Helpers ────────────────────────────────────────────────────


def _migrate_runtime_flow_to_top_level(
    out: dict[str, Any], warn: list[str] | None,
) -> None:
    """Lift ``runtime.flow`` to top-level ``flow:`` (v2 promotion).

    When BOTH ``runtime.flow`` AND top-level ``flow:`` are present,
    the top-level (canonical) form wins.
    """
    runtime = out.get("runtime")
    if not isinstance(runtime, dict) or "flow" not in runtime:
        return
    runtime_flow = runtime.pop("flow")
    if "flow" in out:
        # Canonical top-level wins; the runtime.flow shim is dropped.
        if warn is not None:
            warn.append(
                "Legacy ``runtime.flow`` ignored - top-level ``flow:`` "
                "already set with the canonical form."
            )
        return
    out["flow"] = runtime_flow
    if warn is not None:
        warn.append(
            "Legacy ``runtime.flow`` lifted to top-level ``flow:``. "
            "Run ``digitorn yaml migrate-v2`` for an automatic rewrite."
        )


def _merge_execution_into_runtime(
    out: dict[str, Any], warn: list[str] | None,
) -> None:
    """Move legacy ``execution:`` sub-fields into the canonical
    ``runtime:`` (most), ``security:`` (sandbox / credentials_schema)
    and ``ui:`` (greeting) blocks.

    When BOTH ``execution:`` and ``runtime:`` are present, ``runtime:``
    wins on conflict (canonical-first rule) and the legacy execution
    sub-field is dropped with a warning.
    """
    execution = out.pop("execution", None)
    if not isinstance(execution, dict):
        return

    if warn is not None:
        warn.append(
            "Legacy `execution:` block lifted into `runtime:`/`security:`/`ui:`. "
            "Run `digitorn yaml migrate-v2` for an automatic rewrite."
        )

    for key, value in execution.items():
        # Some sub-fields move to security or ui; the rest stay in runtime
        # (with possible rename for workspace -> workdir).
        dest = _EXECUTION_SUBFIELDS.get(key)
        if dest is None:
            target_block = "runtime"
            target_key = key
        else:
            target_block, target_key = dest

        block_dict = out.setdefault(target_block, {})
        if not isinstance(block_dict, dict):
            continue
        if target_key in block_dict:
            # Canonical (nested) form wins.
            if warn is not None:
                warn.append(
                    f"`execution.{key}` ignored - "
                    f"`{target_block}.{target_key}` already set."
                )
            continue
        block_dict[target_key] = value


def _move_misplaced_runtime_fields(
    out: dict[str, Any], warn: list[str] | None,
) -> None:
    """Lift fields placed under ``runtime:`` that actually belong in
    ``security:`` or ``ui:`` per the canonical schema.

    Handles the case where a YAML uses the new ``runtime:`` block but
    keeps legacy field placements inside it. Idempotent: when both the
    misplaced and canonical forms are set, the canonical wins.
    """
    runtime = out.get("runtime")
    if not isinstance(runtime, dict):
        return

    # Move sandbox + credentials_schema -> security
    for key, target_block in (
        ("sandbox", "security"),
        ("credentials_schema", "security"),
        ("greeting", "ui"),
    ):
        if key not in runtime:
            continue
        target = out.setdefault(target_block, {})
        if not isinstance(target, dict):
            continue
        if key in target:
            runtime.pop(key, None)
            if warn is not None:
                warn.append(
                    f"`runtime.{key}` ignored - "
                    f"`{target_block}.{key}` already set."
                )
            continue
        target[key] = runtime.pop(key)
        if warn is not None:
            warn.append(
                f"`runtime.{key}` lifted to `{target_block}.{key}`. "
                f"Run `digitorn yaml migrate-v2`."
            )

    # Rename runtime.workspace -> runtime.workdir for users who hand-
    # wrote the new shape but kept the legacy field name.
    if "workspace" in runtime and "workdir" not in runtime:
        runtime["workdir"] = runtime.pop("workspace")
        if warn is not None:
            warn.append(
                "`runtime.workspace` renamed to `runtime.workdir`."
            )
    if "workspace_mode" in runtime and "workdir_mode" not in runtime:
        runtime["workdir_mode"] = runtime.pop("workspace_mode")
        if warn is not None:
            warn.append(
                "`runtime.workspace_mode` renamed to `runtime.workdir_mode`."
            )


def _apply_agent_aliases(agent: dict[str, Any]) -> dict[str, Any]:
    """Lift ``coordination:`` and ``instructions:`` sub-blocks back into
    the legacy flat fields so the existing compiler logic keeps reading
    them. Legacy values win on conflict (allowing gradual migration)."""
    out = dict(agent)

    coord = out.pop("coordination", None)
    if isinstance(coord, dict):
        if "delegate_to" in coord and "delegate_to" not in out:
            out["delegate_to"] = coord["delegate_to"]
        if "pool" in coord and "pool" not in out:
            out["pool"] = coord["pool"]

    instr = out.pop("instructions", None)
    if isinstance(instr, dict):
        # `file:` is the user-facing name; legacy field is `skills`.
        if "file" in instr and "skills" not in out:
            out["skills"] = instr["file"]
        if "capabilities" in instr and "capabilities" not in out:
            out["capabilities"] = instr["capabilities"]
        if "specialty" in instr and "specialty" not in out:
            out["specialty"] = instr["specialty"]

    return out

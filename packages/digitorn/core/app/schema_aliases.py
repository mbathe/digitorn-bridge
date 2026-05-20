"""Pre-Pydantic alias pass: reshape legacy (flat) YAML into the canonical"""
from __future__ import annotations

from typing import Any


_TOP_LEVEL_TO_NESTED: dict[str, tuple[str, str]] = {
    "modules":         ("tools", "modules"),
    "capabilities":    ("tools", "capabilities"),
    "channels":        ("tools", "channels"),
    "behavior":        ("security", "behavior"),
    "widgets":         ("ui", "widgets"),
    "workspace":       ("ui", "workspace"),
    "preview":         ("ui", "preview"),
    "theme":           ("ui", "theme"),
    "features":        ("ui", "features"),
    "slash_commands":  ("ui", "slash_commands"),
    "skills":          ("dev", "skills"),
    "variables":       ("dev", "variables"),
    "include":         ("dev", "include"),
    "middleware":      ("runtime", "middleware"),
    "pipeline":        ("runtime", "pipeline"),
}


_EXECUTION_SUBFIELDS: dict[str, tuple[str, str]] = {
    "sandbox":             ("security", "sandbox"),
    "credentials_schema":  ("security", "credentials_schema"),
    "greeting":            ("ui", "greeting"),
    "workspace":           ("runtime", "workdir"),
    "workspace_mode":      ("runtime", "workdir_mode"),
}


_DEPENDENCIES_TO_NESTED: dict[str, tuple[str, str]] = {
    "variables":         ("dev",      "variables"),
    "channels":          ("tools",    "channels"),
    "credentials":       ("security", "credentials_schema"),
    "credentials_schema":("security", "credentials_schema"),
    "payload":           ("runtime",  "payload_schema"),
    "payload_schema":    ("runtime",  "payload_schema"),
}


def apply_schema_aliases(
    raw: dict[str, Any], *, deprecation_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Return `raw` reshaped into the canonical nested form."""
    if not isinstance(raw, dict):
        return raw

    out = dict(raw)
    warn = deprecation_warnings if deprecation_warnings is not None else None

    _merge_execution_into_runtime(out, warn)

    _migrate_runtime_flow_to_top_level(out, warn)

    _move_misplaced_runtime_fields(out, warn)

    for legacy_key, (block, target_key) in _TOP_LEVEL_TO_NESTED.items():
        if legacy_key not in out:
            continue
        value = out.pop(legacy_key)
        block_dict = out.setdefault(block, {})
        if not isinstance(block_dict, dict):
            continue
        if target_key in block_dict:
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
            out["dependencies"] = leftover

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

    agents = out.get("agents")
    if isinstance(agents, list):
        out["agents"] = [
            _apply_agent_aliases(a) if isinstance(a, dict) else a
            for a in agents
        ]

    return out


def _migrate_runtime_flow_to_top_level(
    out: dict[str, Any], warn: list[str] | None,
) -> None:
    """Lift `runtime.flow` to top-level `flow:` (v2 promotion)."""
    runtime = out.get("runtime")
    if not isinstance(runtime, dict) or "flow" not in runtime:
        return
    runtime_flow = runtime.pop("flow")
    if "flow" in out:
        if warn is not None:
            warn.append(
                "Legacy `runtime.flow` ignored - top-level `flow:` "
                "already set with the canonical form."
            )
        return
    out["flow"] = runtime_flow
    if warn is not None:
        warn.append(
            "Legacy `runtime.flow` lifted to top-level `flow:`. "
            "Run `digitorn yaml migrate-v2` for an automatic rewrite."
        )


def _merge_execution_into_runtime(
    out: dict[str, Any], warn: list[str] | None,
) -> None:
    """Move legacy `execution:` sub-fields into the canonical"""
    execution = out.pop("execution", None)
    if not isinstance(execution, dict):
        return

    if warn is not None:
        warn.append(
            "Legacy `execution:` block lifted into `runtime:`/`security:`/`ui:`. "
            "Run `digitorn yaml migrate-v2` for an automatic rewrite."
        )

    for key, value in execution.items():
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
    """Lift fields placed under `runtime:` that actually belong in"""
    runtime = out.get("runtime")
    if not isinstance(runtime, dict):
        return

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
    """Lift `coordination:` and `instructions:` sub-blocks back into"""
    out = dict(agent)

    coord = out.pop("coordination", None)
    if isinstance(coord, dict):
        if "delegate_to" in coord and "delegate_to" not in out:
            out["delegate_to"] = coord["delegate_to"]
        if "pool" in coord and "pool" not in out:
            out["pool"] = coord["pool"]

    instr = out.pop("instructions", None)
    if isinstance(instr, dict):
        if "file" in instr and "skills" not in out:
            out["skills"] = instr["file"]
        if "capabilities" in instr and "capabilities" not in out:
            out["capabilities"] = instr["capabilities"]
        if "specialty" in instr and "specialty" not in out:
            out["specialty"] = instr["specialty"]

    return out

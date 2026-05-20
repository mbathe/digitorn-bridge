"""Resolve the effective per-turn config for a chosen composer mode."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from digitorn.core.app.compiler import CompiledApp


@dataclass
class EffectiveTurn:
    """Per-turn config produced by `resolve_mode`."""

    active_mode_id: str | None
    mode_label: str
    max_turns: int
    timeout: float
    system_prompt_suffix: str
    tool_grants: list[Any] | None
    behavior_profile: str
    workspace_mode: str | None


def _default_mode_id(modes: dict[str, Any]) -> str | None:
    if not modes:
        return None
    if "auto" in modes:
        return "auto"
    return next(iter(modes))


def resolve_mode(
    compiled: CompiledApp,
    mode_id: str | None,
) -> EffectiveTurn:
    """Compute the effective per-turn config for a chosen mode id."""
    execution = compiled.execution
    modes: dict[str, Any] = getattr(execution, "modes", None) or {}

    resolved_id = mode_id if mode_id else _default_mode_id(modes)
    mode_def = modes.get(resolved_id) if resolved_id else None

    base_profile = ""
    behavior = getattr(compiled, "behavior", None)
    if behavior is not None:
        base_profile = getattr(behavior, "profile", "") or ""

    if mode_def is None:
        return EffectiveTurn(
            active_mode_id=None,
            mode_label="",
            max_turns=execution.max_turns,
            timeout=execution.timeout,
            system_prompt_suffix="",
            tool_grants=None,
            behavior_profile=base_profile,
            workspace_mode=None,
        )

    grants = getattr(mode_def, "tool_grants", None) or None
    if grants is not None and len(grants) == 0:
        grants = None

    label = getattr(mode_def, "label", "") or (resolved_id.capitalize() if resolved_id else "")

    return EffectiveTurn(
        active_mode_id=resolved_id,
        mode_label=label,
        max_turns=getattr(mode_def, "max_turns", None) or execution.max_turns,
        timeout=getattr(mode_def, "timeout", None) or execution.timeout,
        system_prompt_suffix=getattr(mode_def, "system_prompt", "") or "",
        tool_grants=list(grants) if grants is not None else None,
        behavior_profile=(
            getattr(mode_def, "behavior_profile", "") or base_profile
        ),
        workspace_mode=getattr(mode_def, "workspace_mode", None),
    )


def compute_tool_partition(
    grants: list[Any] | None,
    all_tool_names: list[str],
) -> tuple[set[str], set[str]]:
    """Partition the agent's short tool names into `(allowed, blocked)`."""
    if grants is None:
        return set(all_tool_names), set()

    from digitorn.core.runtime.tool_names import to_fqn

    granted: dict[str, set[str] | None] = {}
    for g in grants:
        module = getattr(g, "module", None) or (g.get("module") if isinstance(g, dict) else None)
        if not module:
            continue
        actions = (
            getattr(g, "actions", None)
            if not isinstance(g, dict)
            else g.get("actions")
        )
        if actions:
            granted[module] = (granted.get(module) or set()) | set(str(a) for a in actions)
        else:
            granted[module] = None

    allowed: set[str] = set()
    blocked: set[str] = set()
    for short in all_tool_names:
        fqn = to_fqn(short)
        if "." in fqn:
            module, _, action = fqn.partition(".")
        else:
            module, action = fqn, ""
        actions_filter = granted.get(module, "MISSING")
        if actions_filter == "MISSING":
            blocked.add(short)
        elif actions_filter is None or action in actions_filter:
            allowed.add(short)
        else:
            blocked.add(short)
    return allowed, blocked


def build_mode_switch_message(
    effective: EffectiveTurn,
    allowed_tool_names: set[str],
    blocked_tool_names: set[str],
) -> str:
    """Build the `system_message` text injected on every mode change."""
    label = effective.mode_label or (effective.active_mode_id or "")
    parts: list[str] = [f"[Mode: {label}]" if label else "[Mode]"]
    body = (effective.system_prompt_suffix or "").strip()
    if body:
        parts.append(body)

    if not blocked_tool_names:
        parts.append("All tools are available in this mode.")
    else:
        allowed_line = ", ".join(sorted(allowed_tool_names)) or "(none)"
        blocked_line = ", ".join(sorted(blocked_tool_names))
        parts.append(f"Tools available in this mode: {allowed_line}")
        parts.append(f"Tools blocked in this mode: {blocked_line}")
        parts.append(
            "If you need a blocked tool, ask the user to switch to a "
            "mode that allows it. Do not retry a blocked tool."
        )

    return "\n\n".join(parts)


__all__ = [
    "EffectiveTurn",
    "resolve_mode",
    "compute_tool_partition",
    "build_mode_switch_message",
]

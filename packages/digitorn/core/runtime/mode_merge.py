"""Resolve the effective per-turn config for a chosen composer mode.

The chat composer surfaces a "mode picker" (Ask / Plan / Auto / …)
backed by ``runtime.modes`` in the deployed YAML. Each entry is a
*sparse* override on top of the app's normal runtime / agent / tools
config: only the fields the user set are applied when that mode is
the active one. Empty fields fall back to the app defaults.

This module produces the [`EffectiveTurn`] view the dispatcher should
consult before kicking off a turn. The actual application of those
overrides (system_prompt suffix, tool index swap, behavior profile
swap, max_turns / timeout cap) happens in the agent loop and the
bootstrap layer; this file is just the merge brick they share.

Default-mode policy (when ``mode_id`` is None or empty):

  1. ``"auto"`` if declared.
  2. The first key of ``runtime.modes`` (insertion order) otherwise.
  3. ``None`` if no modes are declared at all - the dispatcher uses
     the app's normal config unmodified.

Unknown ``mode_id`` (string the client sent that does not match any
declared mode) is treated as "no mode picked" - we return the app's
defaults with ``active_mode_id=None``. The caller can decide whether
to log this as a client bug; refusing the turn would surprise the
user mid-conversation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from digitorn.core.app.compiler import CompiledApp


@dataclass
class EffectiveTurn:
    """Per-turn config produced by [`resolve_mode`].

    ``active_mode_id`` is the resolved id (after default-policy +
    unknown-id fallback). ``None`` means no mode is in effect; every
    other field then carries the app's untouched defaults.

    ``tool_grants`` is ``None`` when the mode does not narrow the
    grant list (the dispatcher keeps the app's normal capability
    index). When non-None, the mode supplies a strict subset; the
    dispatcher must rebuild the tool index from this list for the
    turn.

    ``system_prompt_suffix`` is appended (with a clear separator) to
    the agent's existing system prompt. Empty string = no append.

    ``workspace_mode`` overrides ``ui.workspace.mode`` for the turn
    so the client can hide the workspace pane in Ask mode, etc.
    ``None`` = inherit.
    """

    active_mode_id: str | None
    max_turns: int
    timeout: float
    system_prompt_suffix: str
    tool_grants: list[Any] | None
    behavior_profile: str
    workspace_mode: str | None


def _default_mode_id(modes: dict[str, Any]) -> str | None:
    """Server-side default-mode policy. Mirrors `_resolve_default_mode`
    in `manager_v2/_models.py` so the picker pre-selection on the
    client and the dispatcher fallback agree.
    """
    if not modes:
        return None
    if "auto" in modes:
        return "auto"
    return next(iter(modes))


def resolve_mode(
    compiled: CompiledApp,
    mode_id: str | None,
) -> EffectiveTurn:
    """Compute the effective per-turn config given a chosen mode id.

    Args:
        compiled: The deployed app (post-compile dataclass with
            ``execution.modes`` populated by the YAML compiler).
        mode_id: The mode the client picked, or ``None`` / empty to
            ask for the default-policy choice. Unknown ids fall back
            to the app's defaults with ``active_mode_id=None``.

    Returns:
        An [`EffectiveTurn`] ready for the dispatcher to consume.
    """
    execution = compiled.execution
    modes: dict[str, Any] = getattr(execution, "modes", None) or {}

    resolved_id = mode_id if mode_id else _default_mode_id(modes)
    mode_def = modes.get(resolved_id) if resolved_id else None

    base_profile = ""
    behavior = getattr(compiled, "behavior", None)
    if behavior is not None:
        base_profile = getattr(behavior, "profile", "") or ""

    if mode_def is None:
        # Either no modes declared, mode_id was unknown, or the
        # default policy returned None (no modes block at all). All
        # three collapse to "use app defaults, signal no mode active".
        return EffectiveTurn(
            active_mode_id=None,
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

    return EffectiveTurn(
        active_mode_id=resolved_id,
        max_turns=getattr(mode_def, "max_turns", None) or execution.max_turns,
        timeout=getattr(mode_def, "timeout", None) or execution.timeout,
        system_prompt_suffix=getattr(mode_def, "system_prompt", "") or "",
        tool_grants=list(grants) if grants is not None else None,
        behavior_profile=(
            getattr(mode_def, "behavior_profile", "") or base_profile
        ),
        workspace_mode=getattr(mode_def, "workspace_mode", None),
    )


__all__ = ["EffectiveTurn", "resolve_mode"]

"""Unit tests for `digitorn.core.runtime.mode_merge.resolve_mode`.

The function is the brick the dispatcher will consult on every turn
to know which sparse overrides to apply for the active composer mode.
The application of those overrides (system_prompt suffix, tool index
swap, behavior profile swap) lives elsewhere and gets covered by
integration tests when wired up.

Each test uses lightweight stand-ins for `CompiledApp` and the
`ModeDef` Pydantic class because building a real `CompiledApp`
requires the full compiler chain. The merge function only reads
attribute paths (`compiled.execution.modes`, `compiled.execution.
max_turns`, `compiled.behavior.profile`, …), so duck-typed stubs are
indistinguishable from the real thing for its purposes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from digitorn.core.runtime.mode_merge import EffectiveTurn, resolve_mode


# ── Stubs ────────────────────────────────────────────────────────────


@dataclass
class _FakeMode:
    """Stand-in for the `ModeDef` Pydantic class.

    `resolve_mode` only reads attributes by name, so a dataclass with
    matching attribute names is enough. Defaults match the real
    schema (None / empty-string / empty-list).
    """

    label: str = ""
    description: str = ""
    icon: str = ""
    accent: str = ""
    max_turns: int | None = None
    timeout: float | None = None
    workspace_mode: str | None = None
    system_prompt: str = ""
    tool_grants: list[Any] = field(default_factory=list)
    behavior_profile: str = ""


def _compiled(
    *,
    modes: dict[str, _FakeMode] | None = None,
    max_turns: int = 50,
    timeout: float = 300.0,
    behavior_profile: str = "",
) -> Any:
    """Build a minimal CompiledApp-shaped object for the merge function.

    Only the attribute paths `resolve_mode` actually reads are
    populated. Everything else stays absent.
    """
    execution = SimpleNamespace(
        max_turns=max_turns,
        timeout=timeout,
        modes=modes or {},
    )
    behavior = (
        SimpleNamespace(profile=behavior_profile)
        if behavior_profile
        else None
    )
    return SimpleNamespace(execution=execution, behavior=behavior)


# ── Tests ────────────────────────────────────────────────────────────


class TestNoModes:
    """`runtime.modes` empty or missing => merge is a no-op."""

    def test_empty_dict_no_mode_id(self) -> None:
        compiled = _compiled(modes={}, max_turns=42, timeout=120.0)
        result = resolve_mode(compiled, None)
        assert result == EffectiveTurn(
            active_mode_id=None,
            max_turns=42,
            timeout=120.0,
            system_prompt_suffix="",
            tool_grants=None,
            behavior_profile="",
            workspace_mode=None,
        )

    def test_empty_dict_with_unknown_mode_id(self) -> None:
        # The client may send a mode id that no longer exists (app was
        # redeployed without that mode). Must not error; falls back to
        # defaults silently.
        compiled = _compiled(modes={}, max_turns=42, timeout=120.0)
        result = resolve_mode(compiled, "plan")
        assert result.active_mode_id is None
        assert result.max_turns == 42
        assert result.tool_grants is None

    def test_inherits_behavior_profile(self) -> None:
        compiled = _compiled(modes={}, behavior_profile="coding")
        result = resolve_mode(compiled, None)
        assert result.behavior_profile == "coding"


class TestDefaultPolicy:
    """When `mode_id` is None, pick auto > first key > None."""

    def test_auto_wins_when_present(self) -> None:
        compiled = _compiled(modes={
            "ask": _FakeMode(),
            "plan": _FakeMode(),
            "auto": _FakeMode(),
        })
        result = resolve_mode(compiled, None)
        assert result.active_mode_id == "auto"

    def test_first_key_when_no_auto(self) -> None:
        compiled = _compiled(modes={
            "ask": _FakeMode(label="Ask"),
            "plan": _FakeMode(label="Plan"),
        })
        result = resolve_mode(compiled, None)
        assert result.active_mode_id == "ask"

    def test_empty_string_treated_as_none(self) -> None:
        compiled = _compiled(modes={"auto": _FakeMode()})
        result = resolve_mode(compiled, "")
        assert result.active_mode_id == "auto"


class TestExplicitMode:
    """When `mode_id` matches a declared mode, its overrides apply."""

    def test_max_turns_override(self) -> None:
        compiled = _compiled(
            modes={"ask": _FakeMode(max_turns=8)},
            max_turns=200,
        )
        result = resolve_mode(compiled, "ask")
        assert result.active_mode_id == "ask"
        assert result.max_turns == 8

    def test_max_turns_inherited_when_mode_omits_it(self) -> None:
        # max_turns=None on the mode means "fall back to the app
        # default", not "set to zero".
        compiled = _compiled(
            modes={"plan": _FakeMode()},
            max_turns=200,
        )
        result = resolve_mode(compiled, "plan")
        assert result.max_turns == 200

    def test_timeout_override(self) -> None:
        compiled = _compiled(
            modes={"ask": _FakeMode(timeout=30.0)},
            timeout=600.0,
        )
        result = resolve_mode(compiled, "ask").timeout
        assert result == 30.0

    def test_system_prompt_suffix_passes_through(self) -> None:
        compiled = _compiled(modes={
            "plan": _FakeMode(system_prompt="Outline first."),
        })
        result = resolve_mode(compiled, "plan")
        assert result.system_prompt_suffix == "Outline first."

    def test_workspace_mode_override(self) -> None:
        compiled = _compiled(modes={
            "ask": _FakeMode(workspace_mode="none"),
        })
        result = resolve_mode(compiled, "ask")
        assert result.workspace_mode == "none"


class TestToolGrants:
    """`tool_grants=None` means inherit; non-empty list means narrow."""

    def test_empty_list_inherits(self) -> None:
        # An empty list is the schema default. `None` from the
        # dispatcher's view means "inherit"; we surface both as None
        # so the downstream code only has one branch.
        compiled = _compiled(modes={"ask": _FakeMode(tool_grants=[])})
        result = resolve_mode(compiled, "ask")
        assert result.tool_grants is None

    def test_non_empty_list_passes_through(self) -> None:
        grant = SimpleNamespace(module="filesystem", actions=["read"])
        compiled = _compiled(modes={
            "ask": _FakeMode(tool_grants=[grant]),
        })
        result = resolve_mode(compiled, "ask")
        assert result.tool_grants == [grant]


class TestBehaviorProfile:
    """Mode profile takes precedence; absent => inherit app's profile."""

    def test_mode_profile_wins(self) -> None:
        compiled = _compiled(
            modes={"plan": _FakeMode(behavior_profile="coding")},
            behavior_profile="assistant",
        )
        result = resolve_mode(compiled, "plan")
        assert result.behavior_profile == "coding"

    def test_empty_mode_profile_inherits(self) -> None:
        compiled = _compiled(
            modes={"plan": _FakeMode()},
            behavior_profile="assistant",
        )
        result = resolve_mode(compiled, "plan")
        assert result.behavior_profile == "assistant"


class TestUnknownModeId:
    """A `mode_id` the YAML does not declare is silently ignored."""

    def test_unknown_id_with_modes_present(self) -> None:
        compiled = _compiled(modes={
            "ask": _FakeMode(max_turns=8),
            "plan": _FakeMode(max_turns=30),
        }, max_turns=200)
        result = resolve_mode(compiled, "stealth-mode")
        # Unknown id => treat as "no mode active": app defaults, no
        # overrides applied. The default-policy is NOT re-run because
        # the user did pick something - we just ignore the bad pick.
        assert result.active_mode_id is None
        assert result.max_turns == 200

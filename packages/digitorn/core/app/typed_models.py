"""Typed replacements for the four high-value ``dict[str, Any]`` fields.

Phase 2 swaps these dict shapes for proper Pydantic models so the IDE
auto-completes and typos surface as schema errors:

  - :class:`QuickPrompt`   <- ``AppMeta.quick_prompts``
  - :class:`SkillEntry`    <- ``AppDefinition.skills``
  - :class:`SlashCommand`  <- ``AppDefinition.slash_commands``
  - :class:`AgentPoolConfig` <- ``AgentDefinition.pool``

The remaining dict-typed fields (theme, middleware, behavior.rules,
constraints) keep their loose shape - either because they're opaque
client pass-throughs (theme) or because they have their own dedicated
validators (behavior.rules, constraints).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ─── QuickPrompt ────────────────────────────────────────────────


class QuickPrompt(BaseModel):
    """A one-click suggested prompt rendered by the client.

    Future-proofed with ``extra: allow`` - the client may consume
    additional fields (tooltip, accent, ...) without a schema bump."""

    model_config = {"extra": "allow"}

    label: str = Field(
        ...,
        min_length=1,
        description="Short button label (e.g. 'Counter', 'New PR').",
    )
    message: str = Field(
        ...,
        min_length=1,
        description="The full prompt sent to the agent when clicked.",
    )
    icon: str = Field(
        default="",
        description="Optional emoji or icon name for the button.",
    )


# ─── SkillEntry ─────────────────────────────────────────────────


class SkillEntry(BaseModel):
    """One entry in the app-level ``skills:`` list.

    Skills are reusable command files (.md) the agent can invoke via
    a slash command. The compiler reads the file at compile time and
    surfaces the content to the agent via the slash-command palette."""

    model_config = {"extra": "forbid"}

    command: str = Field(
        ...,
        min_length=1,
        description="Slash command id (e.g. '/commit', '/review').",
    )
    description: str = Field(
        default="",
        description="One-line description shown in the command palette.",
    )
    path: str = Field(
        ...,
        min_length=1,
        description="Path to the .md file relative to the bundle dir.",
    )


# ─── SlashCommand ───────────────────────────────────────────────


class SlashCommand(BaseModel):
    """One entry in the app-level ``slash_commands:`` list.

    Distinct from ``skills:`` in that slash commands are pure client-side
    UI: they render in the / palette but DO NOT load a markdown file.
    The ``template`` is filled with form values and sent to the agent
    as a normal message.

    Future-proofed with ``extra: allow`` to keep the contract evolvable."""

    model_config = {"extra": "allow"}

    command: str = Field(
        ...,
        min_length=1,
        description="Slash command id (e.g. '/deploy', '/restart').",
    )
    description: str = Field(
        default="",
        description="One-line description shown in the / palette.",
    )
    template: str = Field(
        default="",
        description="Optional message template with {{var}} placeholders.",
    )


# ─── AgentPoolConfig ────────────────────────────────────────────


class CoordinationBlock(BaseModel):
    """Phase 9 grouped orchestration concerns on an agent.

    Replaces the historical scatter (``delegate_to``, ``pool``) with
    a single sub-block. Both shapes still compile - the legacy fields
    are aliased into this block at compile time."""

    model_config = {"extra": "forbid"}

    delegate_to: list[str] = Field(
        default_factory=list,
        description="Agent ids this coordinator can dispatch to.",
    )
    pool: "AgentPoolConfig | None" = Field(
        default=None,
        description="Agent-pool config (max_workers, progress, auto_retry).",
    )


class InstructionsBlock(BaseModel):
    """Phase 9 grouped prompt-extension concerns on an agent.

    Replaces the historical scatter (``specialty``, ``skills``,
    ``capabilities``) with a single sub-block whose role is clearer:
    everything here ENRICHES the agent's prompt at runtime.

    Both shapes still compile - the legacy fields are aliased into this
    block at compile time when the new shape is empty."""

    model_config = {"extra": "forbid"}

    file: str = Field(
        default="",
        description=(
            "Path to a .md file with detailed methodology / instructions. "
            "Loaded at compile time and appended to the system prompt."
        ),
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description=(
            "Names of skill files to auto-load from the bundle's "
            "``./skills/`` directory."
        ),
    )
    specialty: str = Field(
        default="",
        description=(
            "Short description of this specialist's expertise. Shown to "
            "the coordinator in the agent_spawn module's specialist "
            "catalogue."
        ),
    )


class IncludeBlock(BaseModel):
    """The optional ``include:`` block that drives fragmentation.

    The compiler resolves these paths BEFORE Pydantic validation - by
    the time AppDefinition.model_validate runs, the entries here have
    already been merged into ``agents:`` / ``execution.hooks`` etc and
    the block has been popped from the raw dict. We still declare it
    on the schema so editors give autocomplete and the JSON Schema
    documents the feature.

    Each value is either a path string ('./agents/') or a list of
    paths (['./roster/triage.yaml', './roster/refund.yaml'])."""

    model_config = {"extra": "forbid"}

    agents: str | list[str] | None = Field(
        default=None,
        description=(
            "Path to a directory of agent YAML files or a list of paths. "
            "Convention auto-loads ``./agents/*.yaml`` even without this entry."
        ),
    )
    hooks: str | list[str] | None = Field(
        default=None,
        description=(
            "Path to a directory of hook YAML files or a list of paths. "
            "Convention auto-loads ``./hooks/*.yaml`` even without this entry."
        ),
    )


class AgentPoolConfig(BaseModel):
    """Pool configuration for coordinator agents that spawn specialists.

    Replaces the old ``dict[str, Any]`` form which used to be validated
    only by hand in the compiler. The compiler-side check is now a
    no-op - Pydantic enforces the shape and bounds."""

    model_config = {"extra": "forbid"}

    max_workers: int = Field(
        default=3,
        ge=1,
        le=100,
        description="Maximum concurrent specialist agents this coordinator can spawn.",
    )
    progress: bool = Field(
        default=False,
        description="Whether to relay progress events from spawned agents to the coordinator.",
    )
    auto_retry: int = Field(
        default=0,
        ge=0,
        le=5,
        description="Number of automatic retries when a specialist fails.",
    )

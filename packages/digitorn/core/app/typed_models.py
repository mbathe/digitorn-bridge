"""Typed replacements for the four high-value `dict[str, Any]` fields."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class QuickPrompt(BaseModel):
    """A one-click suggested prompt rendered by the client."""

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


class SkillEntry(BaseModel):
    """One entry in the app-level `skills:` list."""

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


class SlashCommand(BaseModel):
    """One entry in the app-level `slash_commands:` list."""

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


class CoordinationBlock(BaseModel):
    """Grouped orchestration concerns on an agent."""

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
    """Grouped prompt-extension concerns on an agent."""

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
            "`./skills/` directory."
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
    """The optional `include:` block that drives fragmentation."""

    model_config = {"extra": "forbid"}

    agents: str | list[str] | None = Field(
        default=None,
        description=(
            "Path to a directory of agent YAML files or a list of paths. "
            "Convention auto-loads `./agents/*.yaml` even without this entry."
        ),
    )
    hooks: str | list[str] | None = Field(
        default=None,
        description=(
            "Path to a directory of hook YAML files or a list of paths. "
            "Convention auto-loads `./hooks/*.yaml` even without this entry."
        ),
    )


class AgentPoolConfig(BaseModel):
    """Pool configuration for coordinator agents that spawn specialists."""

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

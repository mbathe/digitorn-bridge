"""Agent spawn - unified Agent tool with mode dispatch via hidden params."""

from __future__ import annotations

from pydantic import BaseModel, Field

_HIDDEN = {"hidden": True}

class AgentParams(BaseModel):
    """Launch, monitor, or manage sub-agents."""

    prompt: str | None = Field(
        default=None,
        description=(
            "The task for the agent. Must be self-contained - "
            "the agent cannot see your conversation."
        ),
    )
    description: str = Field(
        default="",
        description="Short label for the UI (e.g. 'Search API endpoints').",
    )

    agent_id: str | None = Field(
        default=None,
        json_schema_extra=_HIDDEN,
        description="Existing agent ID - check status, wait, cancel, or reassign.",
    )
    agent_ids: list[str] | None = Field(
        default=None,
        json_schema_extra=_HIDDEN,
        description="Wait for multiple agents. Omit = wait for all running.",
    )
    wait: bool = Field(
        default=False,
        description="Wait for the agent to finish (blocks until done). Default: false (background).",
    )
    cancel: bool = Field(
        default=False,
        json_schema_extra=_HIDDEN,
        description="Cancel a running agent (requires agent_id).",
    )
    reassign: str | None = Field(
        default=None,
        json_schema_extra=_HIDDEN,
        description="New task for a failed/cancelled agent (requires agent_id).",
    )
    list_agents: bool = Field(
        default=False,
        json_schema_extra=_HIDDEN,
        description="List all agents with their status.",
    )

    specialist: str | None = Field(
        default=None,
        description=(
            "Optional specialist agent id to run under (e.g. "
            "'web_researcher', 'writer', 'explore'). Must match one "
            "of the `agents:` declared in the app YAML. Omit for "
            "the default general-purpose worker."
        ),
    )
    system_prompt: str | None = Field(
        default=None,
        json_schema_extra=_HIDDEN,
        description="Custom system prompt for ad-hoc agents.",
    )
    max_turns: int = Field(
        default=100,
        ge=1,
        le=10000,
        json_schema_extra=_HIDDEN,
        description="Maximum turns before the agent stops.",
    )
    timeout: float = Field(
        default=3600.0,
        ge=1.0,
        le=7200.0,
        json_schema_extra=_HIDDEN,
        description="Max execution time in seconds.",
    )

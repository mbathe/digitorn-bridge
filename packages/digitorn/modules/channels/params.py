"""Pydantic parameter models for all channel actions.

Each model validates agent-provided parameters before execution.
Used with ``@action(params_model=...)`` for automatic schema generation
and runtime validation.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ── Send / Reply / Broadcast ─────────────────────────────────────────


class SendMessageParams(BaseModel):
    """Send a message through a specific channel provider."""

    provider: str = Field(
        ..., description="Provider instance name from the channels config."
    )
    text: str = Field(
        ..., description="Message text to send.", max_length=50_000
    )
    recipient: str = Field(
        default="",
        description="Override recipient (phone, email, channel ID). "
        "If empty, uses the provider's default target.",
    )
    subject: str = Field(
        default="", description="Subject/title (email, Slack header)."
    )
    thread_id: str = Field(
        default="",
        description="Thread ID for reply threading (Slack ts, email Message-ID).",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra metadata passed to the adapter.",
    )


class ReplyParams(BaseModel):
    """Reply to the current inbound event on its originating channel."""

    text: str = Field(
        ..., description="Reply text.", max_length=50_000
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra metadata for the reply.",
    )


class BroadcastParams(BaseModel):
    """Send the same message to multiple providers."""

    providers: list[str] = Field(
        ..., description="List of provider instance names.", min_length=1
    )
    text: str = Field(
        ..., description="Message text.", max_length=50_000
    )
    subject: str = Field(
        default="", description="Subject/title."
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra metadata.",
    )


# ── Provider management ──────────────────────────────────────────────


class ListProvidersParams(BaseModel):
    """List all configured channel providers."""

    include_status: bool = Field(
        default=True, description="Include runtime status for each provider."
    )


class ProviderStatusParams(BaseModel):
    """Get detailed status of a specific provider."""

    provider: str = Field(..., description="Provider instance name.")


class PauseProviderParams(BaseModel):
    """Pause a provider's inbound listener."""

    provider: str = Field(..., description="Provider instance name to pause.")


class ResumeProviderParams(BaseModel):
    """Resume a paused provider's inbound listener."""

    provider: str = Field(..., description="Provider instance name to resume.")


# ── Events / History ─────────────────────────────────────────────────


class ProviderHistoryParams(BaseModel):
    """Get recent inbound/outbound event history for a provider."""

    provider: str = Field(
        default="", description="Provider name. Empty = all providers."
    )
    limit: int = Field(
        default=20, ge=1, le=100, description="Max events to return."
    )
    direction: str = Field(
        default="all",
        description="Filter by direction: 'inbound', 'outbound', or 'all'.",
    )


class StatsParams(BaseModel):
    """Get aggregate statistics for all providers."""

    pass


# ── Testing / Debug ──────────────────────────────────────────────────


class SimulateEventParams(BaseModel):
    """Simulate an inbound event for testing."""

    provider: str = Field(..., description="Provider instance to simulate on.")
    payload: dict[str, Any] = Field(
        default_factory=dict, description="Simulated event payload."
    )
    source: str = Field(
        default="test", description="Simulated sender identifier."
    )
    message: str = Field(
        default="", description="Simulated message text."
    )


class TestSendParams(BaseModel):
    """Send a test message to verify outbound connectivity."""

    provider: str = Field(..., description="Provider instance to test.")
    text: str = Field(
        default="Digitorn test message",
        description="Test message content.",
    )

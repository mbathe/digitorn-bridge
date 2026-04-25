"""Base LLM provider protocol and shared types.

All provider backends implement ``BaseLLMProvider`` so the module can
dispatch to any provider through a single interface.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


@dataclass
class ChatMessage:
    """A single message in a conversation."""

    role: str
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


@dataclass
class TokenUsage:
    """Token consumption for a single request."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass
class ChatResponse:
    """Response from a chat completion request."""

    content: str
    model: str
    finish_reason: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    tool_calls: list[dict[str, Any]] | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamChunk:
    """A single chunk from a streaming response."""

    delta: str
    finish_reason: str | None = None
    usage: TokenUsage | None = None
    tool_calls: list[dict[str, Any]] | None = None
    thinking: str | None = None  # Native thinking content (Anthropic extended thinking)


@dataclass
class ProviderCapabilities:
    """What a provider/model supports."""

    streaming: bool = True
    tool_use: bool = False
    vision: bool = False
    json_mode: bool = False
    system_message: bool = True
    max_context_window: int = 0
    max_output_tokens: int = 0


@dataclass
class ProviderInfo:
    """Metadata about a configured provider instance."""

    provider_id: str
    backend: str
    model: str
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    extra: dict[str, Any] = field(default_factory=dict)


class BaseLLMProvider(abc.ABC):
    """Abstract base for all LLM provider backends.

    Each backend handles one SDK/API family (Anthropic native, OpenAI-compat).
    The module layer manages named instances and dispatch.
    """

    def __init__(
        self,
        provider_id: str,
        model: str,
        *,
        api_key: str = "",
        base_url: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 2,
        default_params: dict[str, Any] | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.default_params = default_params or {}
        self._client: Any = None

    @abc.abstractmethod
    async def initialize(self) -> None:
        """Create the underlying SDK client."""

    @abc.abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ChatResponse:
        """Send a chat completion request."""

    @abc.abstractmethod
    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Send a streaming chat completion request."""
        ...  # pragma: no cover

    @abc.abstractmethod
    def get_info(self) -> ProviderInfo:
        """Return metadata about this provider instance."""

    async def close(self) -> None:
        """Release resources. Override if the SDK client needs cleanup."""
        self._client = None

    def _merge_params(self, **overrides: Any) -> dict[str, Any]:
        """Merge default_params with per-request overrides.

        Explicit overrides (non-None) win over defaults.
        """
        merged = dict(self.default_params)
        for k, v in overrides.items():
            if v is not None:
                merged[k] = v
        return merged

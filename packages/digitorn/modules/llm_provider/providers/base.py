"""Base LLM provider protocol and shared types."""

from __future__ import annotations


import logging

logger = logging.getLogger(__name__)
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
    reasoning_content: str | None = None

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
    reasoning_content: str | None = None

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
    """Abstract base for all LLM provider backends."""

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

    def clone(self, *, provider_id_suffix: str = "") -> "BaseLLMProvider":
        """Return a brand-new provider instance with the same config."""
        new_id = (
            f"{self.provider_id}:{provider_id_suffix}"
            if provider_id_suffix else self.provider_id
        )
        clone = type(self)(
            provider_id=new_id,
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=self.max_retries,
            default_params=dict(self.default_params),
        )
        # tagged so callers can `await close()` and release the httpx pool.
        clone._is_clone = True  # type: ignore[attr-defined]
        return clone

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

    def _model_for_tokenizer(self) -> str:
        return self.model

    def count_tokens(self, text: str) -> int:
        """Token count for a free-form string under this provider's model."""
        if not text:
            return 0
        try:
            from litellm import token_counter
            return int(token_counter(
                model=self._model_for_tokenizer(), text=text,
            ))
        except Exception as exc:
            logger.debug("base best-effort block failed: %s", exc)
        return max(1, len(text) // 4)

    def count_message_tokens(
        self, messages: list[dict[str, Any]],
    ) -> int:
        """Token count for an OpenAI-format conversation; includes per-message overhead."""
        if not messages:
            return 0
        try:
            from litellm import token_counter
            return int(token_counter(
                model=self._model_for_tokenizer(), messages=messages,
            ))
        except Exception as exc:
            logger.debug("base best-effort block failed: %s", exc)
        total = 0
        for m in messages:
            c = m.get("content", "")
            if isinstance(c, str):
                total += len(c) // 4
            elif isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict):
                        t = blk.get("text", "")
                        if isinstance(t, str):
                            total += len(t) // 4
        return max(1, total)

    def count_tokens(self, text: str) -> int:
        """Return the token count of `text` for this provider's model."""
        if not text:
            return 0
        try:
            from tokencost import count_string_tokens
            return int(count_string_tokens(text, model=self.model))
        except ValueError:
            # tokencost raises ValueError for Anthropic; wrap as a
            # 1-message conversation.
            try:
                return self.count_message_tokens(
                    [{"role": "user", "content": text}],
                )
            except Exception as exc:
                logger.debug("base best-effort block failed: %s", exc)
        except Exception as exc:
            logger.debug("base best-effort block failed: %s", exc)
        return max(1, len(text) // 4)

    def count_message_tokens(
        self, messages: list[dict[str, Any]],
    ) -> int:
        """Return the token count for an OpenAI-format message list."""
        if not messages:
            return 0
        try:
            from tokencost import count_message_tokens as _cmt
            return int(_cmt(messages, model=self.model))
        except Exception as exc:
            logger.debug("base best-effort block failed: %s", exc)
        total = 0
        for m in messages:
            c = m.get("content", "")
            if isinstance(c, str):
                total += len(c) // 4
            elif isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict):
                        t = blk.get("text", "")
                        if isinstance(t, str):
                            total += len(t) // 4
        return max(1, total)

    def _merge_params(self, **overrides: Any) -> dict[str, Any]:
        merged = dict(self.default_params)
        for k, v in overrides.items():
            if v is not None:
                merged[k] = v
        return merged
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

    # ── Token counting (model-aware, real values) ──────────────────
    #
    # Both helpers route through ``litellm.token_counter`` which loads
    # the right local tokenizer per model — ``tiktoken`` for OpenAI /
    # GPT-style BPE, the Anthropic tokenizer for Claude 3+ (no network
    # call), HuggingFace tokenizers for Mistral / Llama / Qwen / Gemini.
    # The model name comes from ``self.model`` so each provider
    # instance counts against its actual configured model — not a
    # generic estimate. Cached internally by litellm.
    #
    # Subclasses CAN override when they have a tighter, exact tokenizer
    # bundled with their SDK and litellm misses the model. All counts
    # are best-effort: a token-count error never raises — falls back
    # to a char/4 heuristic so UI pressure indicators stay alive.

    def _model_for_tokenizer(self) -> str:
        """The model identifier passed to litellm. Subclasses can
        override to disambiguate (e.g. ``deepseek/deepseek-chat``
        instead of bare ``deepseek-chat``)."""
        return self.model

    def count_tokens(self, text: str) -> int:
        """Token count for a free-form string under this provider's
        model. Routes through litellm.token_counter."""
        if not text:
            return 0
        try:
            from litellm import token_counter
            return int(token_counter(
                model=self._model_for_tokenizer(), text=text,
            ))
        except Exception:
            pass
        return max(1, len(text) // 4)

    def count_message_tokens(
        self, messages: list[dict[str, Any]],
    ) -> int:
        """Token count for an OpenAI-format conversation under this
        provider's model. Each dict must have ``role`` + ``content``.
        Counts boilerplate (per-message overhead) too — this is the
        number the LLM API will bill you for on the prompt side."""
        if not messages:
            return 0
        try:
            from litellm import token_counter
            return int(token_counter(
                model=self._model_for_tokenizer(), messages=messages,
            ))
        except Exception:
            pass
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

    # ── Token counting (model-aware, real values) ──────────────────
    #
    # Both helpers route through ``tokencost`` which selects the right
    # tokenizer per model: ``tiktoken`` for OpenAI / DeepSeek / GPT-style
    # BPE models, the Anthropic beta token-counting API for Claude 3+,
    # and ``cl100k_base`` as a last-resort fallback for unknown models.
    # Subclasses can override when they have a tighter, exact tokenizer
    # bundled with their SDK (e.g. an in-process Anthropic tokenizer).
    #
    # All counts are best-effort — if the lookup fails the fallback is
    # the rough char/4 heuristic so callers (UI pressure indicators,
    # context-budget estimators) never crash on a token-count error.

    def count_tokens(self, text: str) -> int:
        """Return the token count of ``text`` for this provider's model.

        Used for system prompt + tool schema + standalone string sizing.
        Anthropic doesn't expose a stateless string tokenizer through
        ``tokencost``; this method falls back to a wrapped messages call
        (``user`` role, single message) so the same signature works for
        every provider.
        """
        if not text:
            return 0
        try:
            from tokencost import count_string_tokens
            return int(count_string_tokens(text, model=self.model))
        except ValueError:
            # tokencost raises ValueError for Anthropic — wrap in a
            # 1-message conversation and route through the messages path.
            try:
                return self.count_message_tokens(
                    [{"role": "user", "content": text}],
                )
            except Exception:
                pass
        except Exception:
            pass
        # Heuristic fallback so the UI never sees a 500.
        return max(1, len(text) // 4)

    def count_message_tokens(
        self, messages: list[dict[str, Any]],
    ) -> int:
        """Return the token count for an OpenAI-format message list.

        Each message must be a dict with ``role`` and ``content`` keys
        (system/user/assistant/tool). Used to size full conversations,
        tool-call payloads, and any structured prompt the LLM sees.
        """
        if not messages:
            return 0
        try:
            from tokencost import count_message_tokens as _cmt
            return int(_cmt(messages, model=self.model))
        except Exception:
            pass
        # Heuristic fallback — sum of content char/4 across messages.
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
        """Merge default_params with per-request overrides.

        Explicit overrides (non-None) win over defaults.
        """
        merged = dict(self.default_params)
        for k, v in overrides.items():
            if v is not None:
                merged[k] = v
        return merged

"""Pydantic parameter models for all llm_provider actions.

Each action has a dedicated model so that LLM agents see the full
JSON schema with types, descriptions, and constraints.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ConfigureParams(BaseModel):
    """Register a named LLM provider instance."""

    provider_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description=(
            "Unique name for this provider instance "
            "(e.g. 'coordinator_brain', 'worker_brain', 'deepseek_coder')."
        ),
    )
    backend: str = Field(
        "openai_compat",
        description=(
            "Provider backend: 'anthropic' (native Anthropic SDK) or "
            "'openai_compat' (any OpenAI-compatible API: OpenAI, DeepSeek, "
            "Groq, Mistral, Together, Ollama, vLLM, LM Studio, etc.)."
        ),
        pattern="^(anthropic|openai_compat)$",
    )
    model: str = Field(
        ...,
        min_length=1,
        description="Model identifier (e.g. 'claude-sonnet-4-20250514', 'deepseek-chat', 'gpt-4o').",
    )
    api_key: str = Field(
        "",
        description=(
            "API key. Leave empty for local providers (Ollama, vLLM) "
            "or if set via environment variable."
        ),
    )
    base_url: str | None = Field(
        None,
        description=(
            "API base URL. Auto-resolved for known providers "
            "(openai, deepseek, groq, mistral, together, ollama, lm_studio, vllm). "
            "Set explicitly for custom endpoints."
        ),
    )
    provider_hint: str | None = Field(
        None,
        description=(
            "Hint for auto-resolving base_url: 'openai', 'deepseek', 'groq', "
            "'mistral', 'together', 'ollama', 'lm_studio', 'vllm'."
        ),
    )
    timeout: float = Field(
        120.0,
        ge=1.0,
        le=600.0,
        description="Request timeout in seconds.",
    )
    max_retries: int = Field(
        2,
        ge=0,
        le=10,
        description="Max automatic retries on transient errors.",
    )
    default_params: dict[str, Any] | None = Field(
        None,
        description=(
            "Default generation parameters applied to every request "
            "(temperature, max_tokens, top_p, etc.). "
            "Per-request overrides take precedence."
        ),
    )


class MessageParam(BaseModel):
    """A single message in a conversation."""

    role: str = Field(
        ...,
        description="Message role: 'system', 'user', 'assistant', or 'tool'.",
        pattern="^(system|user|assistant|tool)$",
    )
    # LP9: accept Union[str, list] to support multimodal content blocks
    # (image_url, image, text). The runtime preserves lists when images
    # are present and flattens to string otherwise.
    content: Any = Field(
        ...,
        description="Message content (string or list of content blocks for multimodal).",
    )
    name: str | None = Field(
        None,
        description="Optional speaker name (for multi-agent conversations).",
    )
    tool_call_id: str | None = Field(
        None,
        description="Tool call ID (required when role='tool').",
    )
    tool_calls: list[dict[str, Any]] | None = Field(
        None,
        description="Tool calls made by the assistant.",
    )


class ChatParams(BaseModel):
    """Send a chat completion request to a configured provider."""

    provider_id: str = Field(
        ...,
        description="Name of the configured provider instance to use.",
    )
    messages: list[MessageParam] = Field(
        ...,
        min_length=1,
        description="Conversation messages.",
    )
    temperature: float | None = Field(
        None,
        ge=0.0,
        le=2.0,
        description="Sampling temperature (0.0 = deterministic, 2.0 = creative). Overrides provider default.",
    )
    max_tokens: int | None = Field(
        None,
        ge=1,
        le=1_000_000,
        description="Maximum tokens to generate. Overrides provider default.",
    )
    top_p: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Nucleus sampling threshold. Overrides provider default.",
    )
    stop: list[str] | None = Field(
        None,
        description="Stop sequences — generation stops when any is produced.",
    )
    tools: list[dict[str, Any]] | None = Field(
        None,
        description="Tool definitions (OpenAI function-calling format).",
    )
    tool_choice: str | None = Field(
        None,
        description="Tool selection strategy: 'auto', 'none', 'required', or a specific tool name.",
    )
    response_format: dict[str, Any] | None = Field(
        None,
        description="Response format constraint (e.g. {'type': 'json_object'}).",
    )
    stream: bool = Field(
        False,
        description="If True, return a streaming response.",
    )
    extra: dict[str, Any] | None = Field(
        None,
        description="Provider-specific extra parameters passed through to the API.",
    )


class RemoveParams(BaseModel):
    """Remove a configured provider instance."""

    provider_id: str = Field(
        ...,
        description="Name of the provider instance to remove.",
    )


class ListProvidersParams(BaseModel):
    """List all configured provider instances. No required parameters."""


class GetProviderInfoParams(BaseModel):
    """Get metadata about a configured provider instance."""

    provider_id: str = Field(
        ...,
        description="Name of the provider instance.",
    )


class UpdateDefaultsParams(BaseModel):
    """Update default generation parameters for a provider instance."""

    provider_id: str = Field(
        ...,
        description="Name of the provider instance to update.",
    )
    temperature: float | None = Field(
        None,
        ge=0.0,
        le=2.0,
        description="Default sampling temperature.",
    )
    max_tokens: int | None = Field(
        None,
        ge=1,
        le=1_000_000,
        description="Default max tokens.",
    )
    top_p: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Default nucleus sampling threshold.",
    )
    extra: dict[str, Any] | None = Field(
        None,
        description="Additional default parameters to merge.",
    )

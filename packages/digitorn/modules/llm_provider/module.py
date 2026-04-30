"""LLMProviderModule - unified access to all LLM providers.

Named provider instances (like named database connections) can be configured
at startup via app YAML or dynamically at runtime. Each instance wraps a
specific backend (Anthropic native, OpenAI-compatible) and model with its
own default parameters.

Actions:
    configure       Register a named provider instance
    chat            Send a chat completion request
    remove          Remove a provider instance
    list_providers  List all configured provider instances
    get_provider_info   Get metadata about a provider instance
    update_defaults     Update default generation parameters
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from pydantic import BaseModel, Field

from digitorn.core.credentials.slot import CredentialSlot
from digitorn.modules.base import ActionResult, BaseModule, Platform

logger = logging.getLogger(__name__)


# ── Config model (compile-time validation via CONFIG_MODEL) ──────


class LlmProviderConfig(BaseModel):
    """Pydantic config for the llm_provider module (validated at compile time).

    ``providers`` stays permissive because each backend (Anthropic native,
    OpenAI-compatible, etc.) has its own set of keys. Extras are allowed
    so apps may declare forward-compatible hints (``default_provider``,
    routing policies, ...) without being rejected.
    """

    model_config = {"extra": "allow"}

    workspace: str = Field(default="", description="Auto-injected by the daemon.")
    providers: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Named LLM provider instances - free-form per backend.",
    )
    default_provider: str | None = Field(
        default=None,
        description="Name of the default provider used when agents don't pick one.",
    )
from digitorn.modules.decorators import action
from digitorn.modules.llm_provider.params import (
    ChatParams,
    ConfigureParams,
    GetProviderInfoParams,
    ListProvidersParams,
    RemoveParams,
    UpdateDefaultsParams,
)
from digitorn.modules.llm_provider.providers.base import (
    BaseLLMProvider,
    ChatMessage,
)
from digitorn.modules.manifest import ModuleManifest


class LLMProviderModule(BaseModule):
    """Unified LLM provider module.

    Manages named provider instances and dispatches chat requests
    to the appropriate backend. Supports Anthropic (native SDK) and
    any OpenAI-compatible API (OpenAI, DeepSeek, Groq, Mistral,
    Together, Ollama, vLLM, LM Studio, etc.).
    """

    MODULE_ID = "llm_provider"
    VERSION = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]
    MODULE_TYPE = "system"
    CONFIG_MODEL = LlmProviderConfig

    # Declarative credential slot. The compiler walks this list and
    # builds the per-app credential manifest from it. Per-agent brain
    # credentials use the same slot since every brain block is
    # ultimately one provider config.
    credential_slots: list[CredentialSlot] = [
        CredentialSlot(
            id="brain_credential",
            label="LLM provider credential",
            handler_types=[
                "api_key", "bearer_token", "oauth2",
            ],
            providers=[
                "openai", "anthropic", "deepseek", "groq", "mistral",
                "together", "gemini", "google-gemini", "xai", "grok",
                "cerebras", "perplexity", "fireworks", "cohere",
                "ollama", "lm_studio", "vllm",
            ],
            scopes_preferred=["per_user", "system_wide"],
            scopes_allowed=None,
            inject={
                "api_key": "{block}.config.api_key",
                "organization": "{block}.config.organization",
                "base_url": "{block}.config.base_url",
            },
            required=False,
            help=(
                "Bound at deploy time for system_wide / per_app_shared "
                "scopes; bound at session start for per_user scopes "
                "(hot-swapped onto the live provider instance)."
            ),
        ),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._providers: dict[str, BaseLLMProvider] = {}


    async def on_start(self) -> None:
        """Called when module transitions to ACTIVE."""

    async def on_stop(self) -> None:
        """Close all provider connections."""
        for provider in self._providers.values():
            try:
                await provider.close()
            except Exception:
                pass
        self._providers.clear()

    async def on_config_update(self, config: dict[str, Any]) -> None:
        """Handle config updates from app YAML.

        Expects config in the format::

            providers:
              coordinator_brain:
                backend: anthropic
                model: claude-sonnet-4-20250514
                api_key: "..."
                temperature: 0.2
                max_tokens: 8192
              worker_brain:
                backend: openai_compat
                provider_hint: deepseek
                model: deepseek-chat
                api_key: "..."

        Or a flat single-provider config (from agent brain)::

            provider: deepseek
            model: deepseek-chat
            temperature: 0.2
            ...
        """
        await super().on_config_update(config)
        providers_conf = config.get("providers", {})
        if providers_conf:
            for pid, pconf in providers_conf.items():
                await self._configure_from_dict(pid, pconf)

    def state_snapshot(self) -> dict[str, Any]:
        """Persist provider configurations for restart recovery."""
        snapshot: dict[str, Any] = {}
        for pid, provider in self._providers.items():
            info = provider.get_info()
            snapshot[pid] = {
                "backend": info.backend,
                "model": info.model,
                "base_url": provider.base_url,
                "timeout": provider.timeout,
                "max_retries": provider.max_retries,
                "default_params": provider.default_params,
            }
        return {"providers": snapshot}

    async def restore_state(self, state: dict[str, Any]) -> None:
        """Restore providers from saved state (minus API keys)."""
        providers = state.get("providers", {})
        for pid, pconf in providers.items():
            if pid not in self._providers:
                try:
                    await self._configure_from_dict(pid, pconf)
                except Exception:
                    pass


    @action(
        description=(
            "Register a named LLM provider instance. Supports Anthropic (native SDK) "
            "and any OpenAI-compatible API (OpenAI, DeepSeek, Groq, Mistral, Together, "
            "Ollama, vLLM, LM Studio). Provider instances can be referenced by agents "
            "in their brain configuration."
        ),
        params_model=ConfigureParams,
        permissions=["llm_provider:admin"],
        risk_level="medium",
        tags=["configuration", "lifecycle"],
        side_effects=["network_request"],
    )
    async def configure(self, params: ConfigureParams) -> ActionResult:
        """Register and initialize a named provider instance."""
        try:
            provider = self._create_provider(
                provider_id=params.provider_id,
                backend=params.backend,
                model=params.model,
                api_key=params.api_key,
                base_url=params.base_url,
                provider_hint=params.provider_hint,
                timeout=params.timeout,
                max_retries=params.max_retries,
                default_params=params.default_params,
            )

            await provider.initialize()

            old = self._providers.get(params.provider_id)
            if old is not None:
                await old.close()

            self._providers[params.provider_id] = provider
            info = provider.get_info()

            return ActionResult(
                success=True,
                data={
                    "provider_id": params.provider_id,
                    "backend": info.backend,
                    "model": info.model,
                    "base_url": provider.base_url,
                    "capabilities": asdict(info.capabilities),
                },
            )
        except ImportError as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:
            return ActionResult(success=False, error=f"Failed to configure provider: {exc}")

    @action(
        description=(
            "Send a chat completion request to a configured LLM provider. "
            "Supports all standard parameters (temperature, max_tokens, top_p, stop, tools) "
            "plus provider-specific extras. Returns the model response with token usage."
        ),
        params_model=ChatParams,
        permissions=["llm_provider:chat"],
        risk_level="low",
        tags=["inference", "chat"],
        side_effects=["network_request"],
    )
    async def chat(self, params: ChatParams) -> ActionResult:
        """Send a chat completion request."""
        provider = self._providers.get(params.provider_id)
        if provider is None:
            return ActionResult(
                success=False,
                error=f"Provider '{params.provider_id}' not configured. Use 'configure' first.",
            )

        messages = [
            ChatMessage(
                role=m.role,
                content=m.content,
                name=m.name,
                tool_call_id=m.tool_call_id,
                tool_calls=m.tool_calls,
            )
            for m in params.messages
        ]

        if params.stream:
            # Streaming mode - preserve accumulated chunks even on mid-stream error
            chunks: list[str] = []
            final_usage = None
            final_finish = None
            stream_error: Exception | None = None
            # LP2: accumulate tool_calls - they were previously DROPPED in streaming
            # mode, completely breaking streaming agent loops with tool use.
            accumulated_tool_calls: list[dict[str, Any]] = []
            seen_tool_call_ids: set[str] = set()
            try:
                async for chunk in provider.chat_stream(
                    messages,
                    temperature=params.temperature,
                    max_tokens=params.max_tokens,
                    top_p=params.top_p,
                    stop=params.stop,
                    tools=params.tools,
                    tool_choice=params.tool_choice,
                    extra=params.extra,
                ):
                    chunks.append(chunk.delta)
                    if chunk.finish_reason:
                        final_finish = chunk.finish_reason
                    if chunk.usage:
                        final_usage = chunk.usage
                    if chunk.tool_calls:
                        # Dedupe by id - providers may emit the same tool_call
                        # multiple times across chunks (delta + final).
                        for tc in chunk.tool_calls:
                            tc_id = tc.get("id") if isinstance(tc, dict) else None
                            if tc_id and tc_id in seen_tool_call_ids:
                                continue
                            if tc_id:
                                seen_tool_call_ids.add(tc_id)
                            accumulated_tool_calls.append(tc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                stream_error = exc
                logger.warning(
                    "llm_stream_error after %d chunks: %s",
                    len(chunks), exc,
                )

            content = "".join(chunks)
            data = {
                "content": content,
                "model": provider.model,
                "finish_reason": final_finish or ("error" if stream_error else None),
                "usage": asdict(final_usage) if final_usage else {},
                "tool_calls": accumulated_tool_calls if accumulated_tool_calls else None,
                "streamed": True,
                "partial": stream_error is not None,
                "chunks_received": len(chunks),
            }
            if stream_error is not None:
                # Stream failed mid-message - return failure but PRESERVE the
                # accumulated chunks so the caller doesn't lose what was streamed.
                return ActionResult(
                    success=False,
                    error=f"Chat stream failed after {len(chunks)} chunks: {stream_error}",
                    data=data,
                )
            return ActionResult(success=True, data=data)

        # Non-streaming mode
        try:
            response = await provider.chat(
                messages,
                temperature=params.temperature,
                max_tokens=params.max_tokens,
                top_p=params.top_p,
                stop=params.stop,
                tools=params.tools,
                tool_choice=params.tool_choice,
                response_format=params.response_format,
                extra=params.extra,
            )

            return ActionResult(
                success=True,
                data={
                    "content": response.content,
                    "model": response.model,
                    "finish_reason": response.finish_reason,
                    "usage": asdict(response.usage),
                    "tool_calls": response.tool_calls,
                    "streamed": False,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ActionResult(success=False, error=f"Chat request failed: {exc}")

    @action(
        description="Remove a configured LLM provider instance and release its resources.",
        params_model=RemoveParams,
        permissions=["llm_provider:admin"],
        risk_level="low",
        tags=["configuration", "lifecycle"],
    )
    async def remove(self, params: RemoveParams) -> ActionResult:
        """Remove a provider instance."""
        provider = self._providers.pop(params.provider_id, None)
        if provider is None:
            return ActionResult(
                success=False,
                error=f"Provider '{params.provider_id}' not found.",
            )

        await provider.close()
        return ActionResult(
            success=True,
            data={"provider_id": params.provider_id, "removed": True},
        )

    @action(
        description="List all configured LLM provider instances with their models and backends.",
        params_model=ListProvidersParams,
        permissions=["llm_provider:read"],
        risk_level="low",
        tags=["query", "info"],
    )
    async def list_providers(self, params: ListProvidersParams) -> ActionResult:
        """List all configured provider instances."""
        providers = []
        for pid, provider in self._providers.items():
            info = provider.get_info()
            providers.append({
                "provider_id": pid,
                "backend": info.backend,
                "model": info.model,
                "base_url": provider.base_url,
            })

        return ActionResult(
            success=True,
            data={"providers": providers, "count": len(providers)},
        )

    @action(
        description="Get detailed metadata about a configured provider instance including capabilities.",
        params_model=GetProviderInfoParams,
        permissions=["llm_provider:read"],
        risk_level="low",
        tags=["query", "info"],
    )
    async def get_provider_info(self, params: GetProviderInfoParams) -> ActionResult:
        """Get metadata about a provider instance."""
        provider = self._providers.get(params.provider_id)
        if provider is None:
            return ActionResult(
                success=False,
                error=f"Provider '{params.provider_id}' not found.",
            )

        info = provider.get_info()
        return ActionResult(
            success=True,
            data={
                "provider_id": info.provider_id,
                "backend": info.backend,
                "model": info.model,
                "capabilities": asdict(info.capabilities),
                "default_params": provider.default_params,
                "timeout": provider.timeout,
                "base_url": provider.base_url,
            },
        )

    @action(
        description=(
            "Update default generation parameters (temperature, max_tokens, top_p) "
            "for an existing provider instance. These defaults apply to all subsequent "
            "chat requests unless overridden per-request."
        ),
        params_model=UpdateDefaultsParams,
        permissions=["llm_provider:admin"],
        risk_level="low",
        tags=["configuration"],
    )
    async def update_defaults(self, params: UpdateDefaultsParams) -> ActionResult:
        """Update default generation parameters."""
        provider = self._providers.get(params.provider_id)
        if provider is None:
            return ActionResult(
                success=False,
                error=f"Provider '{params.provider_id}' not found.",
            )

        if params.temperature is not None:
            provider.default_params["temperature"] = params.temperature
        if params.max_tokens is not None:
            provider.default_params["max_tokens"] = params.max_tokens
        if params.top_p is not None:
            provider.default_params["top_p"] = params.top_p
        if params.extra:
            provider.default_params.update(params.extra)

        return ActionResult(
            success=True,
            data={
                "provider_id": params.provider_id,
                "default_params": provider.default_params,
            },
        )


    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(
            update={
                "description": (
                    "Unified LLM provider module - configure and use any LLM "
                    "through named provider instances. Supports Anthropic (native), "
                    "OpenAI, DeepSeek, Groq, Mistral, Together, Ollama, and more."
                ),
                "author": "Digitorn Team",
                "tags": ["core", "llm", "ai", "inference"],
            }
        )


    def _create_provider(
        self,
        provider_id: str,
        backend: str,
        model: str,
        api_key: str = "",
        base_url: str | None = None,
        provider_hint: str | None = None,
        timeout: float | None = None,
        max_retries: int = 2,
        default_params: dict[str, Any] | None = None,
    ) -> BaseLLMProvider:
        """Create a provider instance without initializing it."""
        if backend == "anthropic":
            from digitorn.modules.llm_provider.providers.anthropic import AnthropicProvider

            return AnthropicProvider(
                provider_id=provider_id,
                model=model,
                api_key=api_key,
                base_url=base_url,
                timeout=timeout or 120.0,
                max_retries=max_retries,
                default_params=default_params,
            )
        elif backend == "openai_compat":
            from digitorn.modules.llm_provider.providers.openai_compat import OpenAICompatProvider

            return OpenAICompatProvider(
                provider_id=provider_id,
                model=model,
                api_key=api_key,
                base_url=base_url,
                provider_hint=provider_hint,
                timeout=timeout,
                max_retries=max_retries,
                default_params=default_params,
            )
        else:
            raise ValueError(f"Unknown backend: '{backend}'. Use 'anthropic' or 'openai_compat'.")

    async def _configure_from_dict(self, provider_id: str, conf: dict[str, Any]) -> None:
        """Configure a provider from a dict (used by on_config_update and restore_state)."""
        backend = conf.get("backend", "openai_compat")
        model = conf.get("model", "")
        api_key = conf.get("api_key", "")
        base_url = conf.get("base_url")
        # ── DEBUG: trace api_key origin (remove after investigation) ──
        _ak_preview = (
            (api_key[:6] + "..." + api_key[-4:])
            if isinstance(api_key, str) and len(api_key) > 12
            else repr(api_key)[:30]
        )
        logger.warning(
            "DEBUG_LLM_CONFIGURE pid=%s backend=%s model=%s api_key=%s base_url=%s",
            provider_id, backend, model, _ak_preview, base_url,
        )
        provider_hint = conf.get("provider_hint") or conf.get("provider")
        timeout = conf.get("timeout")
        max_retries = conf.get("max_retries", 2)

        reserved = {"backend", "model", "api_key", "base_url", "provider_hint", "provider",
                     "timeout", "max_retries", "default_params"}
        default_params = dict(conf.get("default_params") or {})
        for k, v in conf.items():
            if k not in reserved and k not in default_params:
                default_params[k] = v

        provider = self._create_provider(
            provider_id=provider_id,
            backend=backend,
            model=model,
            api_key=api_key,
            base_url=base_url,
            provider_hint=provider_hint,
            timeout=timeout,
            max_retries=max_retries,
            default_params=default_params or None,
        )

        await provider.initialize()

        old = self._providers.get(provider_id)
        if old is not None:
            await old.close()

        self._providers[provider_id] = provider

    async def create_provider_from_brain(self, brain: Any) -> Any:
        """Build and initialize a fresh provider from either a schema
        ``AgentBrain`` (YAML) or a runtime ``CompiledBrain``.

        Both shapes need inline instantiation because they're not declared
        in ``providers:`` - fallback brains, behavior classifiers,
        per-agent inline brains, etc.

        * ``CompiledBrain`` exposes a flat ``inline_config`` dict.
        * ``AgentBrain`` exposes ``provider``, ``backend``, ``model``,
          ``config={api_key,…}``, ``temperature`` etc.
        """
        # CompiledBrain path: use the already-flattened inline_config.
        inline_config = getattr(brain, "inline_config", None)
        if isinstance(inline_config, dict) and inline_config:
            conf: dict[str, Any] = dict(inline_config)
            # Layer any extra knobs from the compiled brain into
            # default_params (temperature / max_tokens / top_p).
            default_params = dict(conf.get("default_params") or {})
            for k in ("temperature", "max_tokens", "top_p"):
                v = getattr(brain, k, None)
                if v is not None and k not in default_params:
                    default_params[k] = v
            if default_params:
                conf["default_params"] = default_params
        else:
            # AgentBrain path - flatten the schema fields.
            cfg = dict(getattr(brain, "config", None) or {})
            conf = {
                "backend": getattr(brain, "backend", None) or "openai_compat",
                "model": getattr(brain, "model", "") or "",
                "api_key": cfg.get("api_key", ""),
                "base_url": cfg.get("base_url"),
                "provider_hint": (
                    cfg.get("provider_hint") or getattr(brain, "provider", None)
                ),
                "timeout": cfg.get("timeout"),
                "max_retries": cfg.get("max_retries", 2),
            }
            default_params: dict[str, Any] = {}
            for k in ("temperature", "max_tokens", "top_p"):
                v = getattr(brain, k, None)
                if v is not None:
                    default_params[k] = v
            if default_params:
                conf["default_params"] = default_params

        pid = f"_inline_{id(brain):x}"
        await self._configure_from_dict(pid, conf)
        return self._providers.get(pid)

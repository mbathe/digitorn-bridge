"""Drop-in proxies for modules and LLM providers hosted in workers."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from .client import WorkerClient
from .registry import WorkerEndpoint

logger = logging.getLogger(__name__)

class ModuleProxy:
    """Transparent stand-in for a Digitorn module."""

    def __init__(
        self,
        module_name: str,
        endpoint: WorkerEndpoint,
        *,
        client: WorkerClient | None = None,
    ) -> None:
        self._module_name = module_name
        self._endpoint = endpoint
        # Allow injecting a pre-built client (tests). In production
        # use the per-endpoint shared client so we don't create one
        # httpx.AsyncClient (= one cert-store load) per proxy.
        if client is None:
            from .client import get_or_create_client
            client = get_or_create_client(endpoint)
        self._client = client

    @property
    def name(self) -> str:
        return self._module_name

    @property
    def endpoint(self) -> WorkerEndpoint:
        return self._endpoint

    async def call_action(
        self,
        action: str,
        args: dict[str, Any],
        *,
        ctx_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Forward one action call to the worker."""
        logger.debug(
            "module_proxy_call module=%s action=%s endpoint=%s",
            self._module_name, action, self._endpoint.worker_id,
        )
        return await self._client.call_action(
            self._module_name, action, args, ctx=ctx_payload,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

class LLMProviderProxy:
    """Drop-in replacement for a BaseLLMProvider instance."""

    def __init__(
        self,
        endpoint: WorkerEndpoint,
        *,
        provider_id: str,
        model: str,
        provider_name: str,
        brain_config: dict[str, Any],
        api_key: str = "",
        base_url: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 2,
        default_params: dict[str, Any] | None = None,
        provider_info: dict[str, Any] | None = None,
        client: WorkerClient | None = None,
        live_provider: "Any | None" = None,
    ) -> None:
        # Mirror BaseLLMProvider's attribute surface for getattr-based callers.
        self.provider_id = provider_id
        self.model = model
        self.provider_name = provider_name
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.default_params = dict(default_params or {})
        # Construction-time brain config; used as fallback when live_provider
        # is missing fields. Live values are read fresh on each chat call so
        # inject_session_time hot-swaps reach the worker.
        self._brain_config = dict(brain_config)
        self._live_provider = live_provider
        # ProviderInfo snapshot captured at construction to avoid a round trip.
        self._provider_info = dict(provider_info or {})
        # Shared per-endpoint client to avoid a per-proxy SSL-context load.
        self._endpoint = endpoint
        if client is None:
            from .client import get_or_create_client
            client = get_or_create_client(endpoint)
        self._client = client
        self._known_tool_names: set[str] | None = None
        self._is_clone: bool = False
        self._closed = False

    @property
    def endpoint(self) -> WorkerEndpoint:
        return self._endpoint

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<LLMProviderProxy provider_id={self.provider_id} "
            f"model={self.model} endpoint={self._endpoint.worker_id}>"
        )

    def _fresh_brain_config(self) -> dict[str, Any]:
        cfg = dict(self._brain_config)
        live = self._live_provider
        if live is None:
            return cfg
        for src_attr, dst_key in (
            ("api_key", "api_key"),
            ("base_url", "base_url"),
            ("model", "model"),
            ("timeout", "timeout"),
            ("max_retries", "max_retries"),
        ):
            val = getattr(live, src_attr, None)
            if val is not None and val != "":
                cfg[dst_key] = val
        # backend must match the live provider class after gateway resolver swaps.
        cls_name = type(live).__name__.lower()
        if "anthropic" in cls_name:
            cfg["backend"] = "anthropic"
        elif "copilot" in cls_name:
            cfg["backend"] = "github_copilot"
        elif "openai" in cls_name:
            cfg["backend"] = "openai_compat"
        # Merge live default_params over cached for per-session overrides.
        live_dp = getattr(live, "default_params", None)
        if isinstance(live_dp, dict) and live_dp:
            merged = dict(cfg.get("default_params") or {})
            merged.update(live_dp)
            cfg["default_params"] = merged
        return cfg

    async def chat_stream(
        self,
        messages: "list[Any]",
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: "str | dict | None" = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator["Any"]:
        """Yield `StreamChunk` objects produced by the worker."""
        if self._closed:
            raise RuntimeError(
                f"LLMProviderProxy(provider_id={self.provider_id}) "
                f"is closed -- cannot start a new stream",
            )

        # Read live provider attributes so hot-swapped credentials reach the worker.
        live_cfg = self._fresh_brain_config()

        # Snapshot the daemon's RequestContext for the worker to restore;
        # ContextVar does not cross the HTTP boundary on its own.
        request_ctx_dict = _snapshot_request_context()

        args: dict[str, Any] = {
            "messages": _serialize_messages(messages),
            "tools": tools or [],
            "gen_params": _pack_gen_params(
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                stop=stop,
                tool_choice=tool_choice,
                extra=extra,
            ),
            "provider_id": self.provider_id,
            "brain_config": live_cfg,
            "model": live_cfg.get("model", self.model),
            "provider_name": self.provider_name,
            "request_ctx": request_ctx_dict,
        }

        async for chunk_dict in self._client.stream_action(
            "llm_provider", "chat_stream", args,
        ):
            if not isinstance(chunk_dict, dict):
                logger.warning(
                    "llm_proxy_chat_stream: dropping non-dict chunk "
                    "type=%s", type(chunk_dict).__name__,
                )
                continue
            # Worker may emit a sentinel error chunk so the daemon
            # can re-raise into the agent loop's retry path.
            if chunk_dict.get("__error__"):
                err_type = str(chunk_dict.get("error_type", "RuntimeError"))
                err_msg = str(chunk_dict.get("error", "worker error"))
                raise _rehydrate_error(err_type, err_msg)
            yield _rehydrate_stream_chunk(chunk_dict)

    async def chat(
        self,
        messages: "list[Any]",
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: "str | dict | None" = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> "Any":
        """Unary chat completion built as a thin accumulator over `chat_stream`."""
        if self._closed:
            raise RuntimeError(
                f"LLMProviderProxy(provider_id={self.provider_id}) "
                f"is closed",
            )

        # response_format is forwarded through extra so the stream endpoint passes it through.
        merged_extra = dict(extra or {})
        if response_format is not None:
            merged_extra.setdefault("response_format", response_format)

        chunks_text: list[str] = []
        final_finish: str | None = None
        final_usage: Any = None
        # Streamed tool-call deltas merged by index; id/name/arguments arrive across chunks.
        tc_acc: dict[int, dict[str, Any]] = {}
        thinking_acc: list[str] = []

        async for chunk in self.chat_stream(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            tools=tools,
            tool_choice=tool_choice,
            extra=merged_extra,
        ):
            if chunk.delta:
                chunks_text.append(chunk.delta)
            if chunk.finish_reason:
                final_finish = chunk.finish_reason
            if chunk.usage is not None:
                final_usage = chunk.usage
            if chunk.tool_calls:
                for tc in chunk.tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    # Stable key: explicit index > id-derived slot > sequential fallback.
                    raw_idx = tc.get("index")
                    tc_id = tc.get("id") or ""
                    if isinstance(raw_idx, int):
                        idx = raw_idx
                    elif tc_id:
                        idx = -hash(tc_id) % (2**31)
                    else:
                        idx = -(len(tc_acc) + 1)
                    entry = tc_acc.setdefault(
                        idx, {"id": "", "name": "", "args_parts": []},
                    )
                    if tc_id:
                        entry["id"] = tc_id
                    tc_name = tc.get("name") or ""
                    if tc_name and not entry["name"]:
                        entry["name"] = tc_name
                    tc_args = tc.get("arguments")
                    if tc_args:
                        if isinstance(tc_args, str):
                            entry["args_parts"].append(tc_args)
                        elif isinstance(tc_args, (dict, list)):
                            import json as _json
                            entry["args_parts"].append(
                                _json.dumps(tc_args, ensure_ascii=False),
                            )
            if chunk.thinking:
                thinking_acc.append(chunk.thinking)

        import json as _json
        import uuid as _uuid
        accumulated_tool_calls: list[dict[str, Any]] = []
        for idx in sorted(tc_acc.keys()):
            entry = tc_acc[idx]
            if not entry["name"]:
                continue
            args_str = "".join(entry["args_parts"])
            args_obj: Any = {}
            if args_str.strip():
                try:
                    args_obj = _json.loads(args_str)
                except _json.JSONDecodeError:
                    args_obj = args_str  # leave raw for downstream recovery
            accumulated_tool_calls.append({
                "id": entry["id"] or f"call_{_uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": entry["name"],
                    "arguments": args_obj,
                },
            })

        from digitorn.modules.llm_provider.providers.base import (
            ChatResponse, TokenUsage,
        )
        return ChatResponse(
            content="".join(chunks_text),
            model=self.model,
            finish_reason=final_finish,
            usage=final_usage if final_usage is not None else TokenUsage(),
            tool_calls=accumulated_tool_calls or None,
            raw={},
            reasoning_content="".join(thinking_acc) or None,
        )

    def _model_for_tokenizer(self) -> str:
        return self.model

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        try:
            from litellm import token_counter
            return int(token_counter(
                model=self._model_for_tokenizer(), text=text,
            ))
        except Exception as exc:
            logger.debug("proxy best-effort block failed: %s", exc)
        return max(1, len(text) // 4)

    def count_message_tokens(self, messages: list[dict[str, Any]]) -> int:
        if not messages:
            return 0
        try:
            from litellm import token_counter
            return int(token_counter(
                model=self._model_for_tokenizer(), messages=messages,
            ))
        except Exception as exc:
            logger.debug("proxy best-effort block failed: %s", exc)
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
            total += 4  # per-message overhead, matches base.py heuristic
        return max(1, total)

    def get_info(self) -> "Any":
        """Return a `ProviderInfo` reconstructed from the construction-time snapshot."""
        return _rehydrate_provider_info(
            self._provider_info or {
                "provider_id": self.provider_id,
                "backend": self.provider_name,
                "model": self.model,
            },
        )

    async def close(self) -> None:
        """Close the httpx client. The worker keeps the real SDK client alive."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._client.aclose()
        except Exception as exc:
            logger.debug("llm_proxy_close_warning: %s", exc)

    def clone(self, *, provider_id_suffix: str = "") -> "LLMProviderProxy":
        """Clone the proxy with a suffixed provider_id; mirrors BaseLLMProvider.clone."""
        new_id = (
            f"{self.provider_id}:{provider_id_suffix}"
            if provider_id_suffix else self.provider_id
        )
        clone = LLMProviderProxy(
            self._endpoint,
            provider_id=new_id,
            model=self.model,
            provider_name=self.provider_name,
            brain_config=self._brain_config,
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=self.max_retries,
            default_params=self.default_params,
            provider_info=self._provider_info,
        )
        clone._is_clone = True
        return clone

def _serialize_messages(messages: "list[Any]") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, dict):
            out.append(m)
            continue
        # ChatMessage-like: convert public fields. Use getattr to be
        # robust to either the dataclass or a duck-typed alternative.
        d: dict[str, Any] = {
            "role": getattr(m, "role", "user"),
            "content": getattr(m, "content", ""),
        }
        for opt_field in (
            "name", "tool_call_id", "tool_calls", "reasoning_content",
        ):
            val = getattr(m, opt_field, None)
            if val is not None:
                d[opt_field] = val
        out.append(d)
    return out

def _pack_gen_params(**kwargs: Any) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None}

def _rehydrate_stream_chunk(d: dict[str, Any]) -> "Any":
    from digitorn.modules.llm_provider.providers.base import (
        StreamChunk, TokenUsage,
    )
    usage_dict = d.get("usage")
    usage_obj = None
    if isinstance(usage_dict, dict):
        usage_obj = TokenUsage(
            prompt_tokens=int(usage_dict.get("prompt_tokens", 0) or 0),
            completion_tokens=int(
                usage_dict.get("completion_tokens", 0) or 0,
            ),
            total_tokens=int(usage_dict.get("total_tokens", 0) or 0),
            cache_read_tokens=int(
                usage_dict.get("cache_read_tokens", 0) or 0,
            ),
            cache_creation_tokens=int(
                usage_dict.get("cache_creation_tokens", 0) or 0,
            ),
        )
    return StreamChunk(
        delta=str(d.get("delta", "") or ""),
        finish_reason=d.get("finish_reason"),
        usage=usage_obj,
        tool_calls=d.get("tool_calls"),
        thinking=d.get("thinking"),
    )

def _rehydrate_chat_response(d: dict[str, Any]) -> "Any":
    from digitorn.modules.llm_provider.providers.base import (
        ChatResponse, TokenUsage,
    )
    usage_dict = d.get("usage") or {}
    usage_obj = TokenUsage(
        prompt_tokens=int(usage_dict.get("prompt_tokens", 0) or 0),
        completion_tokens=int(
            usage_dict.get("completion_tokens", 0) or 0,
        ),
        total_tokens=int(usage_dict.get("total_tokens", 0) or 0),
        cache_read_tokens=int(usage_dict.get("cache_read_tokens", 0) or 0),
        cache_creation_tokens=int(
            usage_dict.get("cache_creation_tokens", 0) or 0,
        ),
    )
    return ChatResponse(
        content=str(d.get("content", "") or ""),
        model=str(d.get("model", "") or ""),
        finish_reason=d.get("finish_reason"),
        usage=usage_obj,
        tool_calls=d.get("tool_calls"),
        raw=d.get("raw") or {},
        reasoning_content=d.get("reasoning_content"),
    )

def _rehydrate_provider_info(d: dict[str, Any]) -> "Any":
    from digitorn.modules.llm_provider.providers.base import (
        ProviderCapabilities, ProviderInfo,
    )
    caps_dict = d.get("capabilities") or {}
    caps = ProviderCapabilities(
        streaming=bool(caps_dict.get("streaming", True)),
        tool_use=bool(caps_dict.get("tool_use", False)),
        vision=bool(caps_dict.get("vision", False)),
        json_mode=bool(caps_dict.get("json_mode", False)),
        system_message=bool(caps_dict.get("system_message", True)),
        max_context_window=int(caps_dict.get("max_context_window", 0) or 0),
        max_output_tokens=int(caps_dict.get("max_output_tokens", 0) or 0),
    )
    return ProviderInfo(
        provider_id=str(d.get("provider_id", "") or ""),
        backend=str(d.get("backend", "") or ""),
        model=str(d.get("model", "") or ""),
        capabilities=caps,
        extra=d.get("extra") or {},
    )

def _snapshot_request_context() -> dict[str, Any] | None:
    try:
        from digitorn.core.runtime.request_context import get_request_context
        rc = get_request_context()
        if rc is None:
            return None
        return {
            "user_id": rc.user_id,
            "app_id": rc.app_id,
            "session_id": rc.session_id,
            "run_id": rc.run_id,
            "agent_id": rc.agent_id,
            "user_jwt": rc.user_jwt,
        }
    except Exception:
        return None

def _rehydrate_error(error_type: str, message: str) -> Exception:
    name = (error_type or "").lower()
    body = (message or "").lower()
    # Inspect message body for canonical quota markers before name-based dispatch
    # so generic SDK wrappers (APIError, 429 with quota_exceeded body) classify correctly.
    if (
        "quota_exceeded" in body
        or "quota exceeded" in body
        or "insufficient_quota" in body
    ):
        try:
            from digitorn.modules.llm_provider.errors import QuotaExceededError
            return QuotaExceededError(message)
        except ImportError:
            return RuntimeError(f"QuotaExceededError: {message}")
    if "insufficient_balance" in body or "insufficient balance" in body:
        return RuntimeError(f"BillingError: {message}")

    if "quota" in name or "quotaexceeded" in name:
        try:
            from digitorn.modules.llm_provider.errors import QuotaExceededError
            return QuotaExceededError(message)
        except ImportError:
            return RuntimeError(f"QuotaExceededError: {message}")
    if "billing" in name or "402" in name or "insufficient_balance" in name:
        return RuntimeError(f"BillingError: {message}")
    if "auth" in name or "401" in name:
        return RuntimeError(f"AuthError: {message}")
    if "ratelimit" in name or "429" in name:
        # 429 carrying a quota_exceeded body is non-retriable quota, not a transient limit.
        if "quota_exceeded" in message.lower():
            try:
                from digitorn.modules.llm_provider.errors import QuotaExceededError
                return QuotaExceededError(message)
            except ImportError:
                pass
        return RuntimeError(f"RateLimitError: {message}")
    if "timeout" in name:
        return asyncio.TimeoutError(message)
    return RuntimeError(message)

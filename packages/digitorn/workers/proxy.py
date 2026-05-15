"""Drop-in proxies for modules and LLM providers hosted in workers.

These types implement the SAME interfaces as their in-process
counterparts so existing callers (tool_exec, agent_loop, hooks,
middleware, streaming) cannot tell whether they're talking to a
local module or a remote worker. That transparency is the cornerstone
of the design: without it we'd have to thread "is this remoted?"
checks through the whole codebase, and the daemon's behaviour would
diverge from today.

What we INTENTIONALLY do NOT do here:
  * Patch module decorators (``@action``) -- the worker has those
    via the real module loader; the proxy just forwards by name.
  * Re-implement business logic -- proxies are pure forwarders.
  * Touch the LLM provider class hierarchy -- ``LLMProviderProxy``
    duck-types ``BaseLLMProvider``'s public surface used by
    ``agent_loop`` / ``streaming`` / ``bootstrap``.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from .client import WorkerClient
from .registry import WorkerEndpoint

logger = logging.getLogger(__name__)


class ModuleProxy:
    """Transparent stand-in for a Digitorn module.

    The daemon's module registry holds one of these in place of the
    real module instance. When the dispatcher calls an action --
    ``await module.bash(command=...)`` or
    ``await module.dispatch("bash", {...})`` -- the proxy serialises
    the args, sends them to the worker, and deserialises the
    ``ActionResult``-shaped response.

    Phase 1 (this file) defines the public surface only. The
    integration glue (how the proxy gets installed in the registry,
    how AgentContext is serialised into the call) is added in
    Phase 2 once we wire it into bootstrap.
    """

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
        """Forward one action call to the worker. ``ctx_payload`` is
        the AgentContext-derived dict shape we agree on in Phase 2
        (session_id, user_id, agent_id, workspace, etc.).

        Returns the raw JSON-deserialised payload -- the dispatcher
        in Phase 2 wraps this into an ``ActionResult`` instance so
        the caller sees the same type as today.
        """
        logger.debug(
            "module_proxy_call module=%s action=%s endpoint=%s",
            self._module_name, action, self._endpoint.worker_id,
        )
        return await self._client.call_action(
            self._module_name, action, args, ctx=ctx_payload,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


# ── LLM Provider Proxy ────────────────────────────────────────────


class LLMProviderProxy:
    """Drop-in replacement for a :class:`BaseLLMProvider` instance,
    forwarding every call to a worker process over HTTP.

    Duck-types the public surface ``agent_loop`` / ``streaming.py`` /
    ``bootstrap.py`` use:

      * ``chat_stream(messages, **gen_params)`` -> async iterator of
        ``StreamChunk`` objects. **HTTP-streamed via NDJSON** so the
        first token appears as fast as the worker can talk to the
        LLM SDK.
      * ``chat(messages, **gen_params)`` -> ``ChatResponse`` (unary).
      * ``count_tokens(text)`` / ``count_message_tokens(messages)``
        -- kept **in-process** via the same litellm path the real
        ``BaseLLMProvider`` uses. Round-tripping a token count
        through HTTP would add 5-50 ms per prompt-render and burn
        worker capacity for a function that doesn't need network.
      * ``get_info()`` -> cached ``ProviderInfo``.
      * ``close()`` -> closes the underlying httpx client.
      * ``clone(provider_id_suffix=...)`` -> a fresh proxy with the
        suffixed id (matches ``BaseLLMProvider.clone()``).

    Identity attributes (``model``, ``provider_id``, ``provider_name``,
    ``api_key``, ``base_url``, ...) live as plain attributes so
    code paths that read them via ``getattr`` find them. The
    dynamically-set markers ``_known_tool_names`` (bootstrap.py
    line 787) and ``_is_clone`` (clone tagging) are writable.

    Failure semantics: if the worker is unreachable or returns an
    error, the proxy raises the same shape of exception the real
    provider would (``RuntimeError``/``httpx.HTTPError``). The
    agent loop's existing retry + brain-fallback logic kicks in
    untouched.
    """

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
        # Mirror BaseLLMProvider's public attribute surface so any
        # code that reads these via ``getattr(provider, "field", ...)``
        # finds them. ``provider_id`` is the cache key the worker
        # uses to keep the underlying SDK client alive across calls.
        self.provider_id = provider_id
        self.model = model
        self.provider_name = provider_name
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.default_params = dict(default_params or {})
        # Brain config snapshot at construction. Used as the
        # FALLBACK when ``live_provider`` is missing fields. If
        # ``live_provider`` is set (default), each chat_stream call
        # rebuilds the brain_config from the live provider's
        # **current** attribute values -- this is what catches
        # ``inject_session_time`` hot-swaps (api_key replaced with a
        # session token, base_url rewritten to the gateway, etc.) so
        # the worker sees the FRESH credentials every call rather
        # than the deploy-time placeholders frozen at proxy
        # construction.
        self._brain_config = dict(brain_config)
        self._live_provider = live_provider
        # ProviderInfo snapshot captured at construction. Used by
        # ``get_info()`` to avoid an extra round trip; the worker's
        # info would be identical anyway since both ends configure
        # the same backend + model + capabilities.
        self._provider_info = dict(provider_info or {})
        # Network plumbing -- use the shared per-endpoint client
        # (see ``client.get_or_create_client``) to avoid spinning up
        # one httpx.AsyncClient (= one SSL-context load) per proxy.
        self._endpoint = endpoint
        if client is None:
            from .client import get_or_create_client
            client = get_or_create_client(endpoint)
        self._client = client
        # Dynamic markers some callers set on the live provider. Kept
        # as plain attributes so ``provider._known_tool_names = {...}``
        # at bootstrap.py line 787 works without any special handling.
        self._known_tool_names: set[str] | None = None
        self._is_clone: bool = False
        self._closed = False

    # ── Identity ─────────────────────────────────────────────────

    @property
    def endpoint(self) -> WorkerEndpoint:
        return self._endpoint

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<LLMProviderProxy provider_id={self.provider_id} "
            f"model={self.model} endpoint={self._endpoint.worker_id}>"
        )

    def _fresh_brain_config(self) -> dict[str, Any]:
        """Build a brain_config snapshot from the LIVE provider's
        current attribute values, falling back to the construction-
        time snapshot for fields the provider doesn't carry.

        This is the critical path for credential freshness when
        ``inject_session_time`` hot-swaps the underlying provider
        (BYOK key, gateway token, gateway URL substitution). Every
        ``chat_stream`` / ``chat`` call reads the live state -- if
        the daemon swapped the provider's ``api_key`` to a session
        token, that token reaches the worker on the next call.
        """
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
        # Default params may be hot-swapped too (e.g. temperature
        # override per-session). Merge live values over cached.
        live_dp = getattr(live, "default_params", None)
        if isinstance(live_dp, dict) and live_dp:
            merged = dict(cfg.get("default_params") or {})
            merged.update(live_dp)
            cfg["default_params"] = merged
        return cfg

    # ── Streaming ────────────────────────────────────────────────

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
        """Yield ``StreamChunk`` objects produced by the worker.

        The worker forwards anthropic / openai SDK chunks line-by-
        line as NDJSON; we reconstruct each into a ``StreamChunk``
        (and its nested ``TokenUsage``) so the caller sees the
        SAME object shape as a direct provider call. Crucial -- the
        agent loop's ``streaming.py`` accesses chunk attributes via
        ``getattr`` and would break on a generic dict.
        """
        if self._closed:
            raise RuntimeError(
                f"LLMProviderProxy(provider_id={self.provider_id}) "
                f"is closed -- cannot start a new stream",
            )

        # Rebuild brain_config from the LIVE provider's current
        # attributes every call. inject_session_time hot-swaps the
        # ``api_key`` / ``base_url`` on the underlying provider at
        # session start (BYOK key resolved, gateway token issued,
        # gateway URL substituted); reading these live ensures the
        # worker connects with FRESH credentials each call rather
        # than the deploy-time placeholders captured at proxy
        # construction.
        live_cfg = self._fresh_brain_config()

        # Capture the daemon-side RequestContext so the worker can
        # restore it in its own ContextVar before invoking the live
        # provider. Without this, gateway-routed providers (which
        # carry ``api_key = "{{user.jwt}}"``) would have no way to
        # substitute the real bearer at the HTTP layer -- the
        # ContextVar lives in the daemon process and does not cross
        # the HTTP boundary on its own.
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

    # ── Unary chat ────────────────────────────────────────────────

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
        """Unary chat completion. Implemented as a thin accumulator
        over ``chat_stream`` so the worker only needs ONE network
        endpoint (the streaming one). Mirrors what the daemon-side
        ``llm_provider.chat`` ``@action`` does when ``stream=True``
        (see ``module.py:280-344``) -- accumulate deltas, capture
        tool_calls + usage, build a ``ChatResponse``.

        ``response_format`` is forwarded into ``gen_params.extra`` so
        backends that support it (OpenAI JSON mode) still see it; the
        streaming path on the worker passes it through ``extra``.
        """
        if self._closed:
            raise RuntimeError(
                f"LLMProviderProxy(provider_id={self.provider_id}) "
                f"is closed",
            )

        # Merge response_format into extra so it transits the stream
        # endpoint unchanged. ``chat_stream`` doesn't accept it as a
        # top-level kwarg (the base class signature splits them).
        merged_extra = dict(extra or {})
        if response_format is not None:
            merged_extra.setdefault("response_format", response_format)

        chunks_text: list[str] = []
        final_finish: str | None = None
        final_usage: Any = None
        accumulated_tool_calls: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
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
                    tc_id = tc.get("id")
                    if tc_id and tc_id in seen_ids:
                        continue
                    if tc_id:
                        seen_ids.add(tc_id)
                    accumulated_tool_calls.append(tc)
            if chunk.thinking:
                thinking_acc.append(chunk.thinking)

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

    # ── Tokenisation (local) ─────────────────────────────────────
    #
    # The same litellm-based implementation BaseLLMProvider uses --
    # NO network round-trip. Tokenisation is sub-millisecond after
    # the tokenizer cache is warm (see ``runtime/session_metrics.py``
    # warmup at boot), so HTTP would only add overhead and burn worker
    # capacity that should serve real LLM calls.

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
        except Exception:
            pass
        return max(1, len(text) // 4)

    def count_message_tokens(self, messages: list[dict[str, Any]]) -> int:
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
            total += 4  # per-message overhead, matches base.py heuristic
        return max(1, total)

    # ── Metadata + lifecycle ─────────────────────────────────────

    def get_info(self) -> "Any":
        """Return a ``ProviderInfo`` reconstructed from the snapshot
        captured at proxy creation. Same shape the real provider
        returns. Cheap, no HTTP.
        """
        return _rehydrate_provider_info(
            self._provider_info or {
                "provider_id": self.provider_id,
                "backend": self.provider_name,
                "model": self.model,
            },
        )

    async def close(self) -> None:
        """Close the httpx client. The worker holds the real SDK
        client; it stays alive across proxy lifecycles and is closed
        when the worker process exits (or when its lazy cache evicts
        the provider_id, future work).
        """
        if self._closed:
            return
        self._closed = True
        try:
            await self._client.aclose()
        except Exception as exc:
            logger.debug("llm_proxy_close_warning: %s", exc)

    def clone(self, *, provider_id_suffix: str = "") -> "LLMProviderProxy":
        """Return a fresh proxy carrying the same identity (same
        worker, same brain config) but a suffixed provider_id.
        Matches ``BaseLLMProvider.clone`` semantics so sub-agents
        and parallel paths get independent network handles.
        """
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


# ── Serialization helpers ────────────────────────────────────────


def _serialize_messages(messages: "list[Any]") -> list[dict[str, Any]]:
    """Convert a list of ``ChatMessage`` dataclasses (or already-dict
    messages) into JSON-safe dicts for the HTTP wire.
    """
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
    """Drop None values so the worker doesn't have to filter them
    out before passing to the SDK (which may reject None for some
    params).
    """
    return {k: v for k, v in kwargs.items() if v is not None}


def _rehydrate_stream_chunk(d: dict[str, Any]) -> "Any":
    """Build a ``StreamChunk`` from the worker's JSON payload.

    Critical: the ``streaming.py`` consumer reads chunk fields via
    ``getattr`` so a malformed/missing field becomes a no-op
    (default empty string for delta, None for the rest). We still
    rehydrate ``TokenUsage`` properly because the agent loop's
    final-chunk handling unpacks it via ``asdict``.
    """
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
    """Build a ``ChatResponse`` from the worker's JSON payload."""
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
    """Build a ``ProviderInfo`` from a JSON snapshot."""
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
    """Capture the daemon-side ``RequestContext`` ContextVar as a
    JSON-safe dict. The worker restores it in its own ContextVar so
    the live provider's HTTP layer (``openai_compat._inject_digitorn_
    request_headers``) finds the user JWT and the X-Digitorn-* IDs
    at call time -- exactly as if the call were happening in-process.

    Returns ``None`` when no context is set (CLI / healthcheck /
    internal turns); in that case the worker simply runs without a
    context, matching the daemon's behaviour for the same callers.
    """
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
    """Re-raise the worker-side error as the same exception type the
    real provider would have raised. We map a small set of known
    types; everything else becomes a generic ``RuntimeError`` so the
    agent loop's existing classifier (``_classify_error`` in
    ``api/apps.py``) keeps routing the error correctly.

    Critical: ``QuotaExceededError`` MUST be restored as the typed
    class because the agent loop's retry path bails out only on an
    ``isinstance`` check. If we let it collapse to ``RuntimeError``,
    a quota-exhausted user gets 5 retries at 5/10/20/40/80 s before
    the error finally surfaces — and by then the surface error is a
    generic timeout, not the structured quota payload the frontend
    needs to render the upgrade toast.
    """
    name = (error_type or "").lower()
    body = (message or "").lower()
    # Defence in depth: providers wrap real errors inside generic SDK
    # exception classes (OpenAI's ``APIError`` envelopes every upstream
    # 4xx/5xx, including LiteLLM's structured 429 ``quota_exceeded``
    # body). When ``error_type`` is one of those generic wrappers, the
    # name-based classification above misses, the rehydrater falls
    # back to ``RuntimeError``, and the agent loop's retry path
    # treats a permanently-blown quota as transient — 5 retries at
    # 5/10/20/40/80 s, then a generic timeout surfaces instead of the
    # structured quota payload the frontend needs.
    #
    # Inspect the message body for canonical markers BEFORE the
    # name-based dispatch so the typed exception always wins.
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
        # Defence in depth: when the worker sees a 429 carrying the
        # gateway's structured quota_exceeded body, treat it as quota
        # (non-retriable) rather than a transient rate limit.
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

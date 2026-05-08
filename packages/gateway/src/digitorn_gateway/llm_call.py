"""LLM dispatch.

Single entry point for every chat completion: `dispatch()` resolves
the model alias, picks the backend (LiteLLM vs custom router), and
returns either an awaitable response or an async iterator of chunks
when `stream=True`.

LiteLLM is used as a LIBRARY (not as a proxy server). Operators get:
* 100+ providers
* OpenAI-compatible request/response shape
* Streaming
* Tool calls
* Cost calculation hooks

For models flagged `provider: custom` in the catalogue, the dispatch
hands the request to `custom_router.get_router().handle(...)`. The
gateway therefore is NOT bound to LiteLLM's coverage.
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator

from digitorn_gateway.custom_router import (
    CustomProviderNotImplemented,
    get_router as get_custom_router,
)
from digitorn_gateway.models import ModelEntry, get_catalog
from digitorn_gateway.quota import UsageRecord

logger = logging.getLogger(__name__)


class ModelNotConfigured(RuntimeError):
    """The requested alias is not in the catalogue. Pass-through to
    LiteLLM is allowed for unknown aliases, so this is raised only
    when even the pass-through is rejected (e.g., disallowed by
    config in a future hardening)."""


# ── Resolution ─────────────────────────────────────────────────────


def resolve_alias(alias: str) -> ModelEntry | None:
    """Look up an alias in the catalogue. Returns None when unknown.

    Caller decides whether to pass the unknown alias straight to
    LiteLLM (current behaviour) or to reject. The MVP forwards
    unknowns - it makes the catalogue declarative and not a
    gatekeeper, which is friendlier during early operations.
    """
    return get_catalog().get(alias)


# Mapping from canonical provider name → env variable LiteLLM reads
# to authenticate. Used by ``check_provider_supported()`` to fail
# fast with a clear ``model_not_provided_by_digitorn`` error instead
# of letting LiteLLM throw an opaque 401/403.
_PROVIDER_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "mistral": ("MISTRAL_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "cohere": ("COHERE_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "perplexity": ("PERPLEXITY_API_KEY", "PERPLEXITYAI_API_KEY"),
    "azure": ("AZURE_API_KEY", "AZURE_OPENAI_API_KEY"),
}


def _resolve_real_provider(alias: str, entry: ModelEntry | None) -> str:
    """Determine the canonical provider name behind an alias.

    Order:
      1. The catalogue entry's explicit ``provider`` field (most
         authoritative when ``models.yaml`` is configured).
      2. The ``provider/`` prefix in the alias (LiteLLM convention).
      3. A best-effort guess from the bare model name.
    """
    if entry is not None and entry.provider:
        return entry.provider.lower()
    if "/" in alias:
        return alias.split("/", 1)[0].lower()
    return _provider_from_model(alias).lower()


def check_provider_supported(alias: str) -> tuple[bool, str, str | None]:
    """Return ``(supported, provider, missing_env_key)``.

    Sub-microsecond pre-flight gating powered by the in-memory
    ``ConfigCache``: zero DB I/O, zero crypto. Falls back to the
    legacy ``_PROVIDER_ENV_KEYS`` table when the cache hasn't been
    populated yet (e.g. tests that don't run the lifespan hook).
    """
    from digitorn_gateway.config_cache import get_cache as _get_cache

    cache = _get_cache()

    # Cache-resident model first.
    m = cache.model(alias)
    if m is not None:
        if m.is_custom:
            return True, m.provider_slug, None
        if cache.is_provider_configured(m.provider_slug):
            return True, m.provider_slug, None
        env_var = cache.env_var_for(m.provider_slug)
        return False, m.provider_slug, env_var

    # Daemon-style ``<provider_slug>/<real_model_id>`` synthesis.
    # Mirror what ``resolve_dispatch`` does so the pre-flight 404 gate
    # stays consistent with what the dispatch path would actually
    # accept. Without this, the gate returns 404 even though the
    # dispatch path would have happily synthesised an alias.
    if "/" in alias:
        prefix, _suffix = alias.split("/", 1)
        if prefix and cache.has_provider(prefix):
            if cache.is_provider_configured(prefix):
                return True, prefix, None
            return False, prefix, cache.env_var_for(prefix)

    # Unknown alias: try to resolve provider from the alias prefix +
    # any cache entry, falling back to the static map for tests.
    entry = resolve_alias(alias)
    provider = _resolve_real_provider(alias, entry)

    if entry is not None and entry.is_custom:
        return True, provider, None

    if cache.has_provider(provider):
        if cache.is_provider_configured(provider):
            return True, provider, None
        return False, provider, cache.env_var_for(provider)

    env_keys = _PROVIDER_ENV_KEYS.get(provider)
    if env_keys is None:
        return False, provider, None
    import os
    for key in env_keys:
        if os.environ.get(key):
            return True, provider, None
    return False, provider, env_keys[0]


# ── Non-streaming ──────────────────────────────────────────────────


async def dispatch(
    *,
    body: dict[str, Any],
) -> tuple[dict[str, Any], UsageRecord]:
    """Run a non-streaming chat completion. Returns the OpenAI-shaped
    response dict + a UsageRecord pre-filled with provider/model and
    token counts (no user_id - the route fills that in)."""
    from digitorn_gateway.config_cache import get_cache as _get_cache

    alias = body.get("model")
    if not alias:
        raise ValueError("missing 'model' field in request body")

    t0 = time.monotonic()

    # Hot-path: pure dict lookup against the in-memory cache.
    resolved = _get_cache().resolve_dispatch(alias)
    entry = resolve_alias(alias)  # legacy YAML fallback (custom router)

    is_custom = (resolved is not None and resolved.is_custom) or (
        entry is not None and entry.is_custom
    )
    if is_custom:
        router = get_custom_router()
        try:
            resp = await router.handle(entry=entry, body=body)
        except CustomProviderNotImplemented:
            raise
        provider = (
            resolved.provider_slug if resolved
            else (entry.provider if entry else "custom")
        )
        provider_model = (
            resolved.real_model_id if resolved
            else (entry.model if entry else alias)
        )
    else:
        import litellm

        cache = _get_cache()
        # Failover loop: walk priority-ordered routes, retry next on
        # retriable upstream errors. Cache pre-filters unhealthy routes.
        passthrough = {
            k: v for k, v in body.items()
            if k in {
                "messages", "temperature", "max_tokens", "top_p",
                "stop", "frequency_penalty", "presence_penalty",
                "tools", "tool_choice", "response_format", "seed",
                "logprobs", "top_logprobs", "n",
            }
        }

        last_exc: Exception | None = None
        litellm_resp = None
        provider_slug_for_record: str = "unknown"
        provider_model: str = alias
        attempted: list[tuple[str | None, str]] = []
        cache_resolved = resolved is not None
        # Multi-account safe failover: track route ids we've already
        # tried in this dispatch so a credential going into 429
        # cooldown mid-loop doesn't leave the picker thinking the
        # next route at index N is gone (the filter just shifted the
        # list). The exclusion set is authoritative.
        tried_route_ids: set = set()

        for idx in range(8):  # bounded fan-out
            r_at = (
                cache.resolve_dispatch_excluding(alias, tried_route_ids)
                if cache_resolved else None
            )
            if r_at is None:
                if idx == 0 and not cache_resolved:
                    # Legacy fallback path - alias not in cache, no
                    # route table to walk. Try LiteLLM directly with
                    # the YAML entry.
                    litellm_model = (
                        entry.litellm_model_id() if entry else alias
                    )
                    provider_for_sanitize = _resolve_real_provider(alias, entry)
                    provider_slug_for_record = (
                        entry.provider if entry
                        else _provider_from_model(litellm_model)
                    )
                    provider_model = entry.model if entry else litellm_model
                    pt = {**passthrough}
                    if "tools" in pt:
                        pt["tools"] = _sanitize_tools_for_provider(
                            pt["tools"], provider_for_sanitize,
                        )
                    litellm_resp = await litellm.acompletion(
                        model=litellm_model, **pt,
                    )
                break  # exhausted candidates

            litellm_model = _litellm_model_from_compat(
                r_at.compat, r_at.provider_slug, r_at.real_model_id,
            )
            pt = {**passthrough}
            if "tools" in pt:
                pt["tools"] = _sanitize_tools_for_provider(
                    pt["tools"], r_at.provider_slug,
                )
            if r_at.api_key:
                pt["api_key"] = r_at.api_key
            if r_at.base_url:
                pt["api_base"] = r_at.base_url
            if r_at.extra_headers:
                pt["extra_headers"] = {
                    **(pt.get("extra_headers") or {}),
                    **r_at.extra_headers,
                }
            # AWS Bedrock smuggles its kwargs through extra_body under
            # a sentinel key (LiteLLM doesn't accept them as headers).
            # Lift them out into top-level kwargs.
            if r_at.extra_body and "_aws_bedrock_kwargs" in r_at.extra_body:
                aws_kwargs = r_at.extra_body["_aws_bedrock_kwargs"]
                if isinstance(aws_kwargs, dict):
                    pt.update(aws_kwargs)
            if r_at.extra_body and "_vertex_kwargs" in r_at.extra_body:
                vk = r_at.extra_body["_vertex_kwargs"]
                if isinstance(vk, dict):
                    pt.update(vk)
            if r_at.extra_body and "_azure_kwargs" in r_at.extra_body:
                az = r_at.extra_body["_azure_kwargs"]
                if isinstance(az, dict):
                    if "api_version" in az:
                        pt["api_version"] = az["api_version"]
                    deployment = az.get("_default_deployment")
                    if deployment and "/" not in litellm_model.split("azure/", 1)[-1]:
                        litellm_model = f"azure/{deployment}"

            attempted.append((
                str(r_at.route_id) if r_at.route_id else None,
                r_at.provider_slug,
            ))
            if r_at.route_id is not None:
                tried_route_ids.add(r_at.route_id)
            # Warm-pool: when the credential has live_pool=True we
            # hand LiteLLM a pre-built ``openai.AsyncOpenAI`` (or
            # ``anthropic.AsyncAnthropic``) whose internal httpx
            # client has been kept open across calls. LiteLLM uses
            # our client via the ``client=`` kwarg instead of building
            # a fresh one -> TCP + TLS handshake paid ONCE, not per
            # call (~150ms saved each dispatch).
            # Skipped for compats whose SDKs pool natively (bedrock,
            # vertex_ai) -- httpx isn't on their critical path.
            if r_at.live_pool and r_at.credential_id is not None:
                from digitorn_gateway.connection_pool import (
                    get_pool, kind_for_compat,
                )
                kind = kind_for_compat(r_at.compat)
                if kind is not None:
                    warm = await get_pool().ensure(
                        r_at.credential_id,
                        kind=kind,
                        api_key=r_at.api_key,
                        base_url=r_at.base_url,
                    )
                    if warm is not None:
                        pt["client"] = warm
            # Multi-account load balance: track in-flight per credential
            # so the resolver picks the least-loaded route on the next
            # call. Paired with mark_dispatch_finished in finally; if
            # the dispatch crashes mid-call, the counter still drops.
            inflight_cid = r_at.credential_id
            if inflight_cid is not None:
                cache.mark_dispatch_started(inflight_cid)
            try:
                from digitorn_gateway.responses_compat import (
                    is_responses_only,
                    chat_body_to_responses_kwargs,
                    responses_to_chat_completion,
                    looks_like_responses_only_error,
                    bump_max_tokens_for_reasoning,
                )

                # Reasoning models eat budget on the reasoning trace
                # before emitting visible content; bump if too low.
                # We mutate BOTH ``pt`` (chat path passthrough) AND the
                # caller's ``body`` so the responses-path conversion
                # below sees the bumped value too.
                bump_max_tokens_for_reasoning(pt, r_at.real_model_id)
                bump_max_tokens_for_reasoning(body, r_at.real_model_id)

                if is_responses_only(r_at.real_model_id):
                    # Auto-route: this model only exists on /responses.
                    rkw = chat_body_to_responses_kwargs(body)
                    rkw.update({k: v for k, v in pt.items() if k in (
                        "api_key", "api_base", "extra_headers", "client",
                        "api_version",
                    )})
                    litellm_resp_raw = await litellm.aresponses(
                        model=litellm_model, **rkw,
                    )
                    litellm_resp = responses_to_chat_completion(
                        litellm_resp_raw, fallback_model=r_at.real_model_id,
                    )
                else:
                    try:
                        litellm_resp = await litellm.acompletion(
                            model=litellm_model, **pt,
                        )
                    except Exception as upstream_exc:
                        # Some providers expose new models on /responses
                        # only AND we don't have them in the static set
                        # yet -- catch the explicit upstream marker and
                        # retry via aresponses() once.
                        if not looks_like_responses_only_error(upstream_exc):
                            raise
                        logger.info(
                            "auto-route: %s flagged as /responses-only "
                            "by upstream, retrying via aresponses()",
                            r_at.real_model_id,
                        )
                        rkw = chat_body_to_responses_kwargs(body)
                        rkw.update({k: v for k, v in pt.items() if k in (
                            "api_key", "api_base", "extra_headers", "client",
                            "api_version",
                        )})
                        litellm_resp_raw = await litellm.aresponses(
                            model=litellm_model, **rkw,
                        )
                        litellm_resp = responses_to_chat_completion(
                            litellm_resp_raw, fallback_model=r_at.real_model_id,
                        )
                if r_at.route_id is not None:
                    cache.mark_route_success(r_at.route_id)
                if inflight_cid is not None:
                    cache.mark_credential_success(inflight_cid)
                provider_slug_for_record = r_at.provider_slug
                provider_model = r_at.real_model_id
                resolved = r_at
                break
            except Exception as exc:
                last_exc = exc
                if r_at.route_id is not None:
                    cache.mark_route_failure(
                        r_at.route_id,
                        f"{type(exc).__name__}: {exc}"[:200],
                    )
                if inflight_cid is not None and _is_rate_limit_error(exc):
                    cache.mark_credential_429(
                        inflight_cid,
                        retry_after_s=_extract_retry_after(exc),
                    )
                if not _is_failover_eligible(exc):
                    raise
            finally:
                if inflight_cid is not None:
                    cache.mark_dispatch_finished(inflight_cid)

        if litellm_resp is None:
            if last_exc is not None:
                raise last_exc
            raise RuntimeError(
                f"no_route_for_model: alias={alias} attempts={attempted}",
            )

        if hasattr(litellm_resp, "model_dump"):
            resp = litellm_resp.model_dump()
        elif hasattr(litellm_resp, "dict"):
            resp = litellm_resp.dict()
        else:
            resp = dict(litellm_resp)
        # Recover content when LiteLLM / upstream returned an empty
        # ``choices[0].message.content`` despite producing tokens
        # (Gemini reasoning trace, Copilot length-truncated, ...).
        from digitorn_gateway.responses_compat import (
            recover_content_from_empty_response,
        )
        resp = recover_content_from_empty_response(resp)
        provider = provider_slug_for_record

    latency_ms = (time.monotonic() - t0) * 1000

    # Extract usage. OpenAI-shaped response has `.usage.prompt_tokens`
    # / `.completion_tokens`. LiteLLM mirrors this.
    usage = resp.get("usage") or {}
    in_tokens = int(usage.get("prompt_tokens") or 0)
    out_tokens = int(usage.get("completion_tokens") or 0)
    if resolved is not None:
        cost = round(
            (in_tokens / 1000.0) * resolved.cost_per_1k_input
            + (out_tokens / 1000.0) * resolved.cost_per_1k_output,
            6,
        )
    else:
        cost = _compute_cost(entry, in_tokens, out_tokens)
    record = UsageRecord(
        user_id="",  # route fills in
        model_alias=alias,
        provider=provider,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
        success=True,
    )
    return resp, record


# ── Streaming ──────────────────────────────────────────────────────


async def dispatch_stream(
    *,
    body: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    """Async generator yielding OpenAI-shaped streaming chunks.

    The gateway aggregates token counts across chunks for the
    post-call usage record - LiteLLM exposes the final usage on
    the last chunk for most providers; for those that don't, we
    estimate from chunk content (TODO: tokenize properly).
    """
    from digitorn_gateway.config_cache import get_cache as _get_cache

    alias = body.get("model")
    if not alias:
        raise ValueError("missing 'model' field in request body")

    cache = _get_cache()
    resolved = cache.resolve_dispatch(alias)
    entry = resolve_alias(alias)

    is_custom = (resolved is not None and resolved.is_custom) or (
        entry is not None and entry.is_custom
    )
    if is_custom:
        router = get_custom_router()
        async for chunk in router.handle_stream(entry=entry, body=body):
            yield chunk
        return

    import litellm

    # Multi-account load balance: track inflight for the duration of
    # the stream. The cleanup happens in the finally below; if the
    # caller cancels mid-stream the GeneratorExit / CancelledError
    # path still triggers it.
    inflight_cid = (
        resolved.credential_id if resolved is not None else None
    )

    if resolved is not None:
        litellm_model = _litellm_model_from_compat(
            resolved.compat,
            resolved.provider_slug,
            resolved.real_model_id,
        )
        api_key = resolved.api_key
        base_url = resolved.base_url
        provider_for_sanitize = resolved.provider_slug
    else:
        litellm_model = entry.litellm_model_id() if entry else alias
        api_key = None
        base_url = None
        provider_for_sanitize = _resolve_real_provider(alias, entry)

    passthrough = {
        k: v for k, v in body.items()
        if k in {
            "messages", "temperature", "max_tokens", "top_p",
            "stop", "frequency_penalty", "presence_penalty",
            "tools", "tool_choice", "response_format", "seed",
            "logprobs", "top_logprobs", "n",
        }
    }
    if "tools" in passthrough:
        passthrough["tools"] = _sanitize_tools_for_provider(
            passthrough["tools"], provider_for_sanitize,
        )
    if api_key:
        passthrough["api_key"] = api_key
    if base_url:
        passthrough["api_base"] = base_url
    if resolved is not None and resolved.extra_headers:
        passthrough["extra_headers"] = {
            **(passthrough.get("extra_headers") or {}),
            **resolved.extra_headers,
        }
    if (resolved is not None and resolved.extra_body
            and "_aws_bedrock_kwargs" in resolved.extra_body):
        aws_kwargs = resolved.extra_body["_aws_bedrock_kwargs"]
        if isinstance(aws_kwargs, dict):
            passthrough.update(aws_kwargs)
    if (resolved is not None and resolved.extra_body
            and "_vertex_kwargs" in resolved.extra_body):
        vk = resolved.extra_body["_vertex_kwargs"]
        if isinstance(vk, dict):
            passthrough.update(vk)
    if (resolved is not None and resolved.extra_body
            and "_azure_kwargs" in resolved.extra_body):
        az = resolved.extra_body["_azure_kwargs"]
        if isinstance(az, dict):
            if "api_version" in az:
                passthrough["api_version"] = az["api_version"]
            deployment = az.get("_default_deployment")
            if deployment and "/" not in litellm_model.split("azure/", 1)[-1]:
                litellm_model = f"azure/{deployment}"

    # Same pool injection as the non-streaming path.
    if (
        resolved is not None
        and getattr(resolved, "live_pool", False)
        and getattr(resolved, "credential_id", None) is not None
    ):
        from digitorn_gateway.connection_pool import (
            get_pool, kind_for_compat,
        )
        kind = kind_for_compat(resolved.compat)
        if kind is not None:
            warm = await get_pool().ensure(
                resolved.credential_id,
                kind=kind,
                api_key=resolved.api_key,
                base_url=resolved.base_url,
            )
            if warm is not None:
                passthrough["client"] = warm

    from digitorn_gateway.responses_compat import (
        is_responses_only,
        chat_body_to_responses_kwargs,
        stream_responses_as_chat_chunks,
        looks_like_responses_only_error,
        bump_max_tokens_for_reasoning,
    )

    real_model_id = (
        resolved.real_model_id if resolved is not None
        else (entry.model if entry is not None else "")
    )
    bump_max_tokens_for_reasoning(passthrough, real_model_id)
    use_responses = is_responses_only(real_model_id)

    if inflight_cid is not None:
        cache.mark_dispatch_started(inflight_cid)
    try:
        if use_responses:
            rkw = chat_body_to_responses_kwargs(body)
            rkw.update({k: v for k, v in passthrough.items() if k in (
                "api_key", "api_base", "extra_headers", "client", "api_version",
            )})
            stream = await litellm.aresponses(
                model=litellm_model, stream=True, **rkw,
            )
            async for chunk in stream_responses_as_chat_chunks(
                stream, model_hint=real_model_id,
            ):
                yield chunk
            if inflight_cid is not None:
                cache.mark_credential_success(inflight_cid)
            return

        try:
            stream = await litellm.acompletion(
                model=litellm_model, stream=True, **passthrough,
            )
        except Exception as upstream_exc:
            if not looks_like_responses_only_error(upstream_exc):
                raise
            # Auto-fallback: upstream told us this model lives on /responses.
            rkw = chat_body_to_responses_kwargs(body)
            rkw.update({k: v for k, v in passthrough.items() if k in (
                "api_key", "api_base", "extra_headers", "client", "api_version",
            )})
            stream = await litellm.aresponses(
                model=litellm_model, stream=True, **rkw,
            )
            async for chunk in stream_responses_as_chat_chunks(
                stream, model_hint=real_model_id,
            ):
                yield chunk
            if inflight_cid is not None:
                cache.mark_credential_success(inflight_cid)
            return

        async for chunk in stream:
            if hasattr(chunk, "model_dump"):
                yield chunk.model_dump()
            elif hasattr(chunk, "dict"):
                yield chunk.dict()
            else:
                yield dict(chunk)
        if inflight_cid is not None:
            cache.mark_credential_success(inflight_cid)
    except Exception as exc:
        if inflight_cid is not None and _is_rate_limit_error(exc):
            cache.mark_credential_429(
                inflight_cid,
                retry_after_s=_extract_retry_after(exc),
            )
        raise
    finally:
        if inflight_cid is not None:
            cache.mark_dispatch_finished(inflight_cid)


# ── Helpers ────────────────────────────────────────────────────────


def _compute_cost(
    entry: ModelEntry | None,
    input_tokens: int,
    output_tokens: int,
) -> float:
    if entry is None:
        return 0.0
    cost = (
        (input_tokens / 1000.0) * entry.cost_per_1k_input_tokens
        + (output_tokens / 1000.0) * entry.cost_per_1k_output_tokens
    )
    return round(cost, 6)


def _sanitize_tools_for_provider(
    tools: list[dict[str, Any]] | None,
    provider: str,
) -> list[dict[str, Any]] | None:
    """Strip ``strict: true`` + ``additionalProperties: false`` from tool
    schemas when the downstream provider chokes on them.

    Why this is needed:

      * The daemon (digitorn) emits tools with ``strict: true`` and
        ``additionalProperties: false`` to take advantage of OpenAI's
        strict mode + Anthropic's strict mode.
      * DeepSeek (and a few others) interpret these flags as
        "every property MUST be in ``required``" - even when some are
        explicitly optional via ``default``. They reject the call with
        ``Required properties must match all properties in the object``.
      * OpenAI accepts the same payload happily, so we keep strict mode
        for ``openai`` / ``azure`` and drop it everywhere else. This
        keeps the gateway provider-agnostic.

    The sanitization is shallow on purpose - we don't rewrite the
    nested schema, only the two top-level flags that trigger strict
    enforcement at the provider boundary.
    """
    if not tools:
        return tools
    if provider in {"openai", "azure"}:
        return tools
    cleaned: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            cleaned.append(tool)
            continue
        new_tool = dict(tool)
        fn = dict(tool.get("function") or {})
        fn.pop("strict", None)
        params = fn.get("parameters")
        if isinstance(params, dict):
            new_params = dict(params)
            new_params.pop("additionalProperties", None)
            fn["parameters"] = new_params
        new_tool["function"] = fn
        cleaned.append(new_tool)
    return cleaned


def _is_failover_eligible(exc: Exception) -> bool:
    """Decide whether to retry the next priority route on this exception.

    Eligible: anything that screams "this provider is having issues" -
    5xx, rate limits, timeouts, connection errors, auth (key may be
    revoked / quota exceeded). Bad request (400) is NOT eligible since
    the message itself is malformed and would fail on every provider.
    """
    cls = type(exc).__name__
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None)
    if status in (400,):
        return False
    if "BadRequest" in cls or "InvalidRequest" in cls:
        return False
    if "ContextWindowExceeded" in cls or "context_length" in msg:
        return False
    # Everything else (auth, rate limit, timeout, server, network, ...)
    # is worth trying the next provider.
    return True


def _is_rate_limit_error(exc: Exception) -> bool:
    """True when the upstream signalled HTTP 429 / rate-limit.

    Triggers per-credential cooldown so the rest of the same-tier
    accounts keep serving while the throttled one sits out the
    cooldown window. Detection is best-effort: HTTP 429 status,
    LiteLLM's RateLimitError class, or "rate limit" / "too many
    requests" in the message body.
    """
    if getattr(exc, "status_code", None) == 429:
        return True
    cls = type(exc).__name__
    if "RateLimit" in cls or "TooManyRequests" in cls:
        return True
    msg = str(exc).lower()
    return "rate limit" in msg or "too many requests" in msg


def _extract_retry_after(exc: Exception) -> float | None:
    """Pull the ``Retry-After`` hint from a 429 exception when the
    upstream provided one (Anthropic + OpenAI both expose this).

    Looks for ``retry_after`` attribute (LiteLLM normalises the
    header onto the exception) and falls back to scanning the
    message for a trailing ``retry after Ns`` pattern. Returns
    ``None`` when nothing is parseable; callers fall back to the
    exponential default in ``mark_credential_429``.
    """
    for attr in ("retry_after", "retry_after_seconds"):
        v = getattr(exc, attr, None)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    headers = getattr(exc, "response_headers", None) or getattr(
        exc, "headers", None,
    )
    if isinstance(headers, dict):
        for key in ("retry-after", "Retry-After", "RETRY-AFTER"):
            if key in headers:
                try:
                    return float(headers[key])
                except (TypeError, ValueError):
                    pass
    import re
    m = re.search(r"retry[-_ ]?after[:\s]+(\d+(?:\.\d+)?)", str(exc), re.I)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _litellm_model_from_compat(
    compat: str, provider_slug: str, real_model_id: str,
) -> str:
    """Decide the model id LiteLLM should see.

    LiteLLM has TWO registration paths:
      1. ``provider/model`` prefixes for ~50 vendors (deepseek, mistral,
         groq, cohere, perplexity, ``bedrock``, ``together_ai``,
         ``fireworks_ai``, ``replicate``, ``cerebras``, ``nvidia_nim``,
         ...).
      2. Bare ids for ``openai`` and ``anthropic`` only.

    We pick using BOTH the slug and the compat dialect:

      * Slug ``openai`` AND compat ``openai``  → bare.
      * compat ``anthropic`` (e.g. ``claude_code``) → bare - the auth
        lane wraps anthropic-shape calls under a custom identity, but
        LiteLLM still routes to api.anthropic.com.
      * compat ``bedrock`` → ``bedrock/<model>`` (LiteLLM native, AWS
        signs the request itself).
      * compat ``openai_compat`` → ``openai/<model>`` + api_base
        override (Together / Fireworks / Cerebras / SambaNova /
        Hyperbolic / NVIDIA NIM all share this dialect).
      * Anything else (deepseek/mistral/groq/replicate/...) → ``slug/model``.
    """
    slug = (provider_slug or "").lower()
    if compat == "bedrock":
        return f"bedrock/{real_model_id}"
    if compat == "vertex_ai":
        return f"vertex_ai/{real_model_id}"
    if compat == "azure":
        return f"azure/{real_model_id}"
    if compat == "openai_compat":
        return f"openai/{real_model_id}"
    if compat == "anthropic":
        return real_model_id
    if slug == "openai" and compat == "openai":
        return real_model_id
    return f"{slug}/{real_model_id}"


def _provider_from_model(model: str) -> str:
    """Best-effort guess at the canonical provider for an
    un-aliased LiteLLM model id, used only for usage tracking.
    """
    if "/" in model:
        return model.split("/", 1)[0]
    if model.startswith(("claude-", "anthropic")):
        return "anthropic"
    if model.startswith(("gpt-", "o1-", "o3-", "o4-")):
        return "openai"
    if model.startswith("gemini"):
        return "gemini"
    return "unknown"

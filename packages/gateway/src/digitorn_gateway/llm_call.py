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
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from digitorn_gateway.custom_router import (
    CustomProviderNotImplemented,
    get_router as get_custom_router,
)
from digitorn_gateway.models import ModelEntry, get_catalog
from digitorn_gateway.quota import UsageRecord

logger = logging.getLogger(__name__)


@dataclass
class DispatchTrace:
    """Per-request observability for the failover loop.

    Caller passes an empty instance, dispatch fills it in. The route
    handler turns it into ``X-Digitorn-Served-By`` /
    ``X-Digitorn-Attempts`` / ``X-Digitorn-Failover-Trail`` /
    ``X-Digitorn-Truncated`` response headers.

    ``truncated_dropped`` is non-zero when Mode 2 head_drop fired on
    the WINNING route - typically because a fallback's context was
    smaller than the request, so the gateway trimmed before retry.
    """

    served_by: str = ""
    route_id: str = ""
    attempts: int = 0
    trail: list[str] = field(default_factory=list)
    truncated_dropped: int = 0


class ModelNotConfigured(RuntimeError):
    """The requested alias is not in the catalogue. Pass-through to
    LiteLLM is allowed for unknown aliases, so this is raised only
    when even the pass-through is rejected (e.g., disallowed by
    config in a future hardening)."""


# ── Resolution ─────────────────────────────────────────────────────


def _normalize_alias(alias: str) -> str:
    """Strip the daemon-provided provider prefix when the bare suffix
    is a known catalog alias. The gateway treats the prefix as an
    informational hint; routing decisions come from the catalog's
    routes (priority + configured credentials).

    Examples (assume catalog contains ``copilot-claude-sonnet-4-5``):
      * ``copilot-claude-sonnet-4-5``            → unchanged
      * ``github_copilot/copilot-claude-sonnet-4-5`` → ``copilot-claude-sonnet-4-5``
      * ``anthropic/unknown-bare-model``         → unchanged (suffix not in catalog)

    Hot-path discipline:
      * Early-exit on the 99% case where alias has no ``/``: zero cache
        access, just a string contains check (single-digit nanoseconds).
      * O(1) lookups via the in-memory ``_models`` dict on the cache.
      * Sync, no awaits, no I/O. Safe to call on every dispatch.
    """
    if "/" not in alias:
        return alias
    from digitorn_gateway.config_cache import get_cache as _get_cache
    cache = _get_cache()
    # The literal "foo/bar" form is rare but legal in the catalog.
    if cache.model(alias) is not None:
        return alias
    _, suffix = alias.split("/", 1)
    if suffix and cache.model(suffix) is not None:
        return suffix
    return alias


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
    # Normalise first: strip provider/ prefix when the bare suffix is
    # a configured catalog alias. Lets the gateway honour its own
    # routing (priority + failover) instead of trusting the daemon's
    # provider hint blindly.
    alias = _normalize_alias(alias)

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
    trace: DispatchTrace | None = None,
    record_health: bool = True,
    pin_route_id: str | None = None,
) -> tuple[dict[str, Any], UsageRecord]:
    """Run a non-streaming chat completion. Returns the OpenAI-shaped
    response dict + a UsageRecord pre-filled with provider/model and
    token counts (no user_id - the route fills that in).

    When ``trace`` is supplied the dispatch fills in which route ended
    up serving + the failover trail. ``trace`` is optional so existing
    callers (tests) keep working without change.
    """
    from digitorn_gateway.config_cache import get_cache as _get_cache
    from digitorn_gateway.config import get_settings as _get_settings

    alias = body.get("model")
    if not alias:
        raise ValueError("missing 'model' field in request body")

    t0 = time.monotonic()

    # Optional pin: when pin_route_id is set we bypass the priority-based
    # resolver and target a specific route_id, with no failover. Used by
    # the diag panel to test individual routes (incl. fallbacks).
    pin_idx: int | None = None
    if pin_route_id is not None:
        _cache_pin = _get_cache()
        _target = next(
            (r for r in _cache_pin.all_routes() if str(r.id) == str(pin_route_id)),
            None,
        )
        if _target is None:
            raise RuntimeError(f"pin_route_not_found: {pin_route_id}")
        _alias_routes = _cache_pin._routes.get(_target.model_alias, [])  # type: ignore[attr-defined]
        try:
            pin_idx = _alias_routes.index(_target)
        except ValueError:
            pin_idx = 0

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

        # Kill-switch + cap come from settings. When failover_enabled
        # is False, ``max_attempts`` collapses to 1 so only the primary
        # healthy route is tried; on failure the user sees the upstream
        # error directly.
        _settings = _get_settings()
        max_attempts = (
            _settings.failover_max_attempts
            if _settings.failover_enabled else 1
        )
        # Pin mode disables failover: only the pinned route is tried.
        if pin_idx is not None:
            max_attempts = 1
        # Mode 2 head_drop state. Token count is computed lazily on the
        # first route that needs it (same model family - tokenizer cost
        # paid once per dispatch, not per attempt). Outside this loop
        # the value stays None and the tokenizer is never invoked.
        _truncate_on = _settings.truncate_enabled
        _truncate_max_out = (
            int(body.get("max_tokens") or
                _settings.truncate_default_max_output_tokens)
        )
        _cached_tokens: int | None = None
        _last_tokens_model: str = ""
        _winner_dropped: int = 0

        for idx in range(max_attempts):  # bounded fan-out
            if pin_idx is not None:
                r_at = cache.resolve_dispatch_at(alias, pin_idx) if idx == 0 else None
            else:
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
            # Mode 2: head_drop only when this route's actual context
            # window is smaller than the request. Lazy: the tokenizer
            # runs only when a route's catalog reports a smaller window
            # than the body could need; the primary path with a large
            # context never pays. Failures (unknown model, broken
            # tokenizer) silently skip - dispatch proceeds untouched.
            _route_dropped = 0
            if _truncate_on:
                try:
                    from digitorn_gateway.truncation import (
                        get_max_context_for_model as _gmc,
                        count_tokens as _ct,
                        head_drop as _hd,
                        can_skip_tokenization as _can_skip,
                    )
                    _route_max_ctx = _gmc(r_at.real_model_id)
                    if (_route_max_ctx
                            and not _can_skip(
                                body.get("messages") or [], _route_max_ctx,
                            )):
                        _budget = _route_max_ctx - _truncate_max_out
                        if _budget > 0:
                            if (_cached_tokens is None
                                    or _last_tokens_model != r_at.real_model_id):
                                _cached_tokens = _ct(
                                    r_at.real_model_id,
                                    body.get("messages") or [],
                                )
                                _last_tokens_model = r_at.real_model_id
                            if _cached_tokens > _budget:
                                trimmed, _route_dropped = _hd(
                                    body.get("messages") or [],
                                    _budget, r_at.real_model_id,
                                )
                                pt["messages"] = trimmed
                except Exception as _trim_exc:
                    logger.debug(
                        "truncation_route_skipped (%s)", _trim_exc,
                    )

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
                if record_health and r_at.route_id is not None:
                    cache.mark_route_success(r_at.route_id)
                if record_health and inflight_cid is not None:
                    cache.mark_credential_success(inflight_cid)
                provider_slug_for_record = r_at.provider_slug
                provider_model = r_at.real_model_id
                resolved = r_at
                _winner_dropped = _route_dropped
                break
            except Exception as exc:
                last_exc = exc
                is_balance = _is_balance_error(exc)
                if record_health and r_at.route_id is not None:
                    cooldown = (
                        float(_settings.balance_failover_cooldown_seconds)
                        if is_balance and _settings.balance_failover_cooldown_seconds > 0
                        else 30.0
                    )
                    cache.mark_route_failure(
                        r_at.route_id,
                        (
                            f"insufficient_balance: {exc}"
                            if is_balance else f"{type(exc).__name__}: {exc}"
                        )[:200],
                        cooldown_s=cooldown,
                    )
                if record_health and inflight_cid is not None and _is_rate_limit_error(exc):
                    cache.mark_credential_429(
                        inflight_cid,
                        retry_after_s=_extract_retry_after(exc),
                    )
                # Balance-specific failover gate: independent of the generic
                # ``failover_enabled`` so operators can keep cross-provider
                # cascade ON for outages but OFF for balance (cost-attribution).
                if is_balance and not _settings.balance_failover_enabled:
                    raise
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

    if trace is not None:
        trace.served_by = provider
        trace.attempts = len(attempted) if attempted else 1
        trace.trail = [a[1] for a in attempted] if attempted else [provider]
        if resolved is not None and resolved.route_id is not None:
            trace.route_id = str(resolved.route_id)
        trace.truncated_dropped = _winner_dropped

    latency_ms = (time.monotonic() - t0) * 1000

    # Extract usage. OpenAI-shaped response has `.usage.prompt_tokens`
    # / `.completion_tokens`. LiteLLM mirrors this. The local-tokenizer
    # fallback (when the provider didn't include a usage block) lives
    # in ``main._quota_record`` so it runs in the BackgroundTask AFTER
    # the response is on the wire to the client; doing it here would
    # add tokenizer CPU time to the user's hot-path latency.
    usage = resp.get("usage") or {}
    # Extract the 4-tier token counts (non-cached input, cache read,
    # cache write, output). Handles OpenAI / Anthropic / Gemini shapes
    # transparently. Cache counts are 0 for providers without cache
    # support OR when the upstream doesn't return the fields.
    from digitorn_gateway.cost import extract_tokens, compute_cost_for_resolved
    in_non_cached, cache_read, cache_write, out_tokens = extract_tokens(usage)
    # ``in_tokens`` we report up to the caller is the BILLED input
    # (= non_cached + cache_read + cache_write) so the daemon sees the
    # full upstream-counted volume. The breakdown lands in usage_events.
    in_tokens_total = in_non_cached + cache_read + cache_write
    if resolved is not None:
        cost = compute_cost_for_resolved(
            input_non_cached=in_non_cached,
            cache_read=cache_read,
            cache_write=cache_write,
            output=out_tokens,
            resolved=resolved,
        )
    else:
        # Legacy YAML path: alias not in the runtime cache. Use the
        # same 4-tier split so cache_read / cache_write are NOT silently
        # billed at the full input rate when the YAML didn't set cache
        # prices. ``_compute_cost`` honours the honest-zero default
        # exactly like ``compute_cost_for_resolved``.
        cost = _compute_cost(
            entry, in_non_cached, out_tokens,
            cache_read=cache_read, cache_write=cache_write,
        )
    record = UsageRecord(
        user_id="",  # route fills in
        model_alias=alias,
        provider=provider,
        input_tokens=in_tokens_total,
        output_tokens=out_tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
        success=True,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        # Pull the per-model Copilot-style multiplier from the resolved
        # dispatch (1.0 when alias was synthesised or legacy YAML). The
        # quota engine applies it post-call to token-shaped metrics.
        token_multiplier=(
            float(getattr(resolved, "token_multiplier", 1.0) or 1.0)
            if resolved is not None else 1.0
        ),
    )
    return resp, record


# ── Streaming ──────────────────────────────────────────────────────


async def dispatch_stream(
    *,
    body: dict[str, Any],
    trace: DispatchTrace | None = None,
    record_health: bool = True,
    pin_route_id: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Async generator yielding OpenAI-shaped streaming chunks.

    Opening failover: the OPEN of the upstream stream is wrapped in
    the same priority-walking loop as the non-streaming path. Once
    ``await litellm.acompletion(stream=True)`` returns successfully
    we commit to that route and start emitting bytes. Errors during
    the open phase (auth, 5xx, rate limit, network) trigger the next
    route. Errors AFTER the first chunk has been yielded propagate
    to the SSE error event - we cannot rewind bytes already on the
    wire.
    """
    from digitorn_gateway.config_cache import get_cache as _get_cache
    from digitorn_gateway.config import get_settings as _get_settings

    alias = body.get("model")
    if not alias:
        raise ValueError("missing 'model' field in request body")

    cache = _get_cache()

    # Optional pin (diag-only): force a specific route_id, disable failover.
    pin_idx: int | None = None
    if pin_route_id is not None:
        _target = next(
            (r for r in cache.all_routes() if str(r.id) == str(pin_route_id)),
            None,
        )
        if _target is None:
            raise RuntimeError(f"pin_route_not_found: {pin_route_id}")
        _alias_routes = cache._routes.get(_target.model_alias, [])  # type: ignore[attr-defined]
        try:
            pin_idx = _alias_routes.index(_target)
        except ValueError:
            pin_idx = 0

    initial = cache.resolve_dispatch(alias)
    entry = resolve_alias(alias)

    is_custom = (initial is not None and initial.is_custom) or (
        entry is not None and entry.is_custom
    )
    if is_custom:
        # Custom routers run their own retry policy. The trace records
        # a single attempt against the resolved provider so the response
        # headers stay populated even on this branch.
        provider_for_trace = (
            initial.provider_slug if initial is not None
            else (entry.provider if entry else "custom")
        )
        if trace is not None:
            trace.served_by = provider_for_trace
            trace.attempts = 1
            trace.trail = [provider_for_trace]
        router = get_custom_router()
        async for chunk in router.handle_stream(entry=entry, body=body):
            yield chunk
        return

    import litellm

    from digitorn_gateway.responses_compat import (
        is_responses_only,
        chat_body_to_responses_kwargs,
        stream_responses_as_chat_chunks,
        looks_like_responses_only_error,
        bump_max_tokens_for_reasoning,
    )

    passthrough_base = {
        k: v for k, v in body.items()
        if k in {
            "messages", "temperature", "max_tokens", "top_p",
            "stop", "frequency_penalty", "presence_penalty",
            "tools", "tool_choice", "response_format", "seed",
            "logprobs", "top_logprobs", "n",
        }
    }

    _settings = _get_settings()
    max_attempts = (
        _settings.failover_max_attempts
        if _settings.failover_enabled else 1
    )
    # Pin mode disables failover (only the pinned route is tried).
    if pin_idx is not None:
        max_attempts = 1
    cache_resolved = initial is not None
    tried_route_ids: set = set()
    attempted: list[tuple[str | None, str]] = []
    last_exc: Exception | None = None

    open_stream_obj: Any = None
    open_resolved: Any = None
    inflight_cid = None
    real_model_id_winner = ""
    use_responses_for_winner = False
    legacy_provider = ""

    # Mode 2 head_drop state. Same lazy pattern as the non-streaming
    # path: tokenizer is invoked only on the first route whose context
    # window is smaller than the request, then cached per-model.
    _truncate_on = _settings.truncate_enabled
    _truncate_max_out = (
        int(body.get("max_tokens") or
            _settings.truncate_default_max_output_tokens)
    )
    _cached_tokens: int | None = None
    _last_tokens_model: str = ""
    _winner_dropped: int = 0

    for idx in range(max_attempts):
        if pin_idx is not None:
            r_at = cache.resolve_dispatch_at(alias, pin_idx) if idx == 0 else None
        else:
            r_at = (
                cache.resolve_dispatch_excluding(alias, tried_route_ids)
                if cache_resolved else None
            )
        if r_at is None:
            if idx == 0 and not cache_resolved:
                # Legacy YAML-only path: alias unknown to cache. One
                # shot, no retry; the YAML entry doesn't carry route
                # metadata to walk.
                litellm_model = entry.litellm_model_id() if entry else alias
                provider_for_sanitize = _resolve_real_provider(alias, entry)
                legacy_provider = (
                    entry.provider if entry
                    else _provider_from_model(litellm_model)
                )
                pt = {**passthrough_base}
                if "tools" in pt:
                    pt["tools"] = _sanitize_tools_for_provider(
                        pt["tools"], provider_for_sanitize,
                    )
                so = pt.get("stream_options") or {}
                if isinstance(so, dict):
                    pt["stream_options"] = {**so, "include_usage": True}
                bump_max_tokens_for_reasoning(pt, "")
                attempted.append((None, legacy_provider))
                try:
                    open_stream_obj = await litellm.acompletion(
                        model=litellm_model, stream=True, **pt,
                    )
                    use_responses_for_winner = False
                    real_model_id_winner = entry.model if entry else ""
                except Exception as exc:
                    last_exc = exc
                    raise
            break  # exhausted candidates

        # Build per-route kwargs.
        litellm_model = _litellm_model_from_compat(
            r_at.compat, r_at.provider_slug, r_at.real_model_id,
        )
        pt = {**passthrough_base}
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

        # Force usage block in stream.
        so = pt.get("stream_options") or {}
        if isinstance(so, dict):
            pt["stream_options"] = {**so, "include_usage": True}

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

        bump_max_tokens_for_reasoning(pt, r_at.real_model_id)
        bump_max_tokens_for_reasoning(body, r_at.real_model_id)
        use_responses = is_responses_only(r_at.real_model_id)

        # Mode 2: head_drop on this route only when its catalog
        # context is smaller than the request would carry. Tokenizer
        # cost is paid lazily; primary path with a large window stays
        # at zero overhead.
        _route_dropped = 0
        if _truncate_on:
            try:
                from digitorn_gateway.truncation import (
                    get_max_context_for_model as _gmc,
                    count_tokens as _ct,
                    head_drop as _hd,
                )
                _route_max_ctx = _gmc(r_at.real_model_id)
                if _route_max_ctx:
                    _budget = _route_max_ctx - _truncate_max_out
                    if _budget > 0:
                        if (_cached_tokens is None
                                or _last_tokens_model != r_at.real_model_id):
                            _cached_tokens = _ct(
                                r_at.real_model_id,
                                body.get("messages") or [],
                            )
                            _last_tokens_model = r_at.real_model_id
                        if _cached_tokens > _budget:
                            trimmed, _route_dropped = _hd(
                                body.get("messages") or [],
                                _budget, r_at.real_model_id,
                            )
                            pt["messages"] = trimmed
            except Exception as _trim_exc:
                logger.debug(
                    "stream_truncation_skipped (%s)", _trim_exc,
                )

        attempted.append((
            str(r_at.route_id) if r_at.route_id else None,
            r_at.provider_slug,
        ))
        if r_at.route_id is not None:
            tried_route_ids.add(r_at.route_id)
        cur_inflight_cid = r_at.credential_id
        if cur_inflight_cid is not None:
            cache.mark_dispatch_started(cur_inflight_cid)

        try:
            if use_responses:
                rkw = chat_body_to_responses_kwargs(body)
                rkw.update({k: v for k, v in pt.items() if k in (
                    "api_key", "api_base", "extra_headers", "client",
                    "api_version",
                )})
                open_stream_obj = await litellm.aresponses(
                    model=litellm_model, stream=True, **rkw,
                )
                use_responses_for_winner = True
            else:
                try:
                    open_stream_obj = await litellm.acompletion(
                        model=litellm_model, stream=True, **pt,
                    )
                    use_responses_for_winner = False
                except Exception as upstream_exc:
                    if not looks_like_responses_only_error(upstream_exc):
                        raise
                    rkw = chat_body_to_responses_kwargs(body)
                    rkw.update({k: v for k, v in pt.items() if k in (
                        "api_key", "api_base", "extra_headers", "client",
                        "api_version",
                    )})
                    open_stream_obj = await litellm.aresponses(
                        model=litellm_model, stream=True, **rkw,
                    )
                    use_responses_for_winner = True
            # OPEN succeeded - commit this route. Inflight stays held
            # until the iteration phase's finally releases it.
            open_resolved = r_at
            inflight_cid = cur_inflight_cid
            real_model_id_winner = r_at.real_model_id
            _winner_dropped = _route_dropped
            break
        except Exception as exc:
            last_exc = exc
            is_balance = _is_balance_error(exc)
            if record_health and r_at.route_id is not None:
                cooldown = (
                    float(_settings.balance_failover_cooldown_seconds)
                    if is_balance and _settings.balance_failover_cooldown_seconds > 0
                    else 30.0
                )
                cache.mark_route_failure(
                    r_at.route_id,
                    (
                        f"insufficient_balance: {exc}"
                        if is_balance else f"{type(exc).__name__}: {exc}"
                    )[:200],
                    cooldown_s=cooldown,
                )
            if record_health and cur_inflight_cid is not None and _is_rate_limit_error(exc):
                cache.mark_credential_429(
                    cur_inflight_cid,
                    retry_after_s=_extract_retry_after(exc),
                )
            if cur_inflight_cid is not None:
                cache.mark_dispatch_finished(cur_inflight_cid)
            # Balance-specific gate: see dispatch() for rationale.
            if is_balance and not _settings.balance_failover_enabled:
                raise
            if not _is_failover_eligible(exc):
                raise

    if open_stream_obj is None:
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(
            f"no_route_for_model: alias={alias} attempts={attempted}",
        )

    # Fill the trace on the winning route.
    if trace is not None:
        if open_resolved is not None:
            trace.served_by = open_resolved.provider_slug
            if open_resolved.route_id is not None:
                trace.route_id = str(open_resolved.route_id)
        else:
            trace.served_by = legacy_provider or (
                attempted[-1][1] if attempted else "unknown"
            )
        trace.attempts = len(attempted) if attempted else 1
        trace.trail = (
            [a[1] for a in attempted] if attempted else [trace.served_by]
        )
        trace.truncated_dropped = _winner_dropped

    # Iterate. Errors here can no longer trigger a route swap (state
    # is committed; first chunk may already be on the wire). They are
    # propagated up to ``_stream_response`` which emits a single SSE
    # ``error`` chunk and closes cleanly.
    #
    # We additionally track two things to detect SILENT TRUNCATION (the
    # provider closes its connection mid-stream without sending a final
    # ``finish_reason`` chunk - common with Copilot per-minute throttle,
    # OpenAI content filter mid-response, transient network drop):
    #   * ``chunk_count``: zero means we never got a single token. The
    #     client would see a blank response with no error. We raise so
    #     the HTTP route surfaces it as an upstream error.
    #   * ``last_finish_reason``: if the stream ends without any chunk
    #     having a non-null finish_reason, we emit a synthetic chunk
    #     with ``finish_reason: "stop"`` so the client receives a
    #     proper end-of-stream event AND we log it for observability.
    chunk_count = 0
    last_finish_reason: str | None = None
    try:
        if use_responses_for_winner:
            async for chunk in stream_responses_as_chat_chunks(
                open_stream_obj, model_hint=real_model_id_winner,
            ):
                chunk_count += 1
                try:
                    choices = chunk.get("choices") or []
                    if choices:
                        fr = choices[0].get("finish_reason")
                        if fr:
                            last_finish_reason = fr
                except Exception:
                    pass
                yield chunk
        else:
            async for chunk in open_stream_obj:
                if hasattr(chunk, "model_dump"):
                    chunk_dict = chunk.model_dump()
                elif hasattr(chunk, "dict"):
                    chunk_dict = chunk.dict()
                else:
                    chunk_dict = dict(chunk)
                chunk_count += 1
                try:
                    choices = chunk_dict.get("choices") or []
                    if choices:
                        fr = choices[0].get("finish_reason")
                        if fr:
                            last_finish_reason = fr
                except Exception:
                    pass
                yield chunk_dict

        # Post-loop truncation detection.
        if chunk_count == 0:
            logger.warning(
                "stream_empty served_by=%s route_id=%s - raising upstream_returned_empty_stream",
                open_resolved.provider_slug if open_resolved else "unknown",
                str(open_resolved.route_id) if open_resolved and open_resolved.route_id else None,
            )
            raise RuntimeError("upstream_returned_empty_stream")
        if last_finish_reason is None:
            logger.warning(
                "stream_truncated_no_finish_reason served_by=%s route_id=%s "
                "chunks=%d - emitting synthetic stop chunk",
                open_resolved.provider_slug if open_resolved else "unknown",
                str(open_resolved.route_id) if open_resolved and open_resolved.route_id else None,
                chunk_count,
            )
            yield {
                "id": f"chatcmpl-truncated-{int(time.monotonic()*1000)}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": real_model_id_winner or "unknown",
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",  # safe for all OpenAI-compat clients
                }],
                "digitorn_truncated": True,  # custom flag for observability
            }

        if record_health and inflight_cid is not None:
            cache.mark_credential_success(inflight_cid)
        if record_health and open_resolved is not None and open_resolved.route_id is not None:
            cache.mark_route_success(open_resolved.route_id)
    except Exception as exc:
        if record_health and inflight_cid is not None and _is_rate_limit_error(exc):
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
    *,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float:
    """Legacy YAML-path cost.

    Mirrors the 4-tier ``compute_cost`` in :mod:`cost`. Kept here so the
    legacy path (alias not in the runtime cache, only declared in
    ``models.yaml``) honours the same honest-default policy: cache
    prices that the YAML doesn't set contribute 0 instead of being
    silently billed at the full input rate.
    """
    if entry is None:
        return 0.0
    cost = (
        (max(0, input_tokens) / 1000.0) * float(
            entry.cost_per_1k_input_tokens or 0
        )
        + (max(0, cache_read) / 1000.0) * float(
            getattr(entry, "cost_per_1k_cache_read_tokens", 0) or 0
        )
        + (max(0, cache_write) / 1000.0) * float(
            getattr(entry, "cost_per_1k_cache_write_tokens", 0) or 0
        )
        + (max(0, output_tokens) / 1000.0) * float(
            entry.cost_per_1k_output_tokens or 0
        )
    )
    return round(cost, 8)


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


def classify_upstream_error(exc: Exception) -> tuple[int, str, str]:
    """Map an upstream exception to ``(http_status, error_class, hint)``.

    Returns the HTTP status the gateway SHOULD send back to its caller,
    plus a short error_class tag for usage_events analytics, plus a
    human-readable hint. Replaces the previous catch-all that turned
    every dispatch failure into HTTP 502 - that polluted logs with
    "upstream_error" entries that were really 4xx-class problems on
    the caller's side.

    Mapping:
      * 400 - bad request body (invalid messages, unsupported param,
              schema validation failure, malformed json from the model)
      * 401 / 403 - upstream authentication failed (the credential is
              the gateway's problem, but we surface the underlying code
              so dashboards can flag it as a credential issue)
      * 404 - model not found at the upstream
      * 413 - context window exceeded (caller sent too many tokens)
      * 429 - upstream rate-limit
      * 502 - upstream 5xx, network, timeout, anything we genuinely
              couldn't route through
    """
    cls = type(exc).__name__
    msg = str(exc)
    msg_lc = msg.lower()
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)

    # Context window first - upstream returns 400 with a specific
    # marker; we elevate it to 413 since "too big" is a request-side
    # constraint the caller can act on.
    if (
        "ContextWindowExceeded" in cls
        or "context_length" in msg_lc
        or "context window" in msg_lc
        or "maximum context length" in msg_lc
    ):
        return 413, "context_window_exceeded", msg

    # Bad request: caller's body is malformed.
    if (
        status == 400
        or "BadRequest" in cls
        or "InvalidRequest" in cls
        or "ValidationError" in cls
        or "UnsupportedParam" in cls
    ):
        return 400, "bad_request", msg

    # LiteLLM wraps client-side payload bugs in APIConnectionError
    # even when no connection failed. Detect known caller-side error
    # signatures so they don't masquerade as 502 (upstream unreachable).
    if (
        # Python TypeError/AttributeError leaking from LiteLLM internals
        "has no attribute" in msg_lc
        or "object is not subscriptable" in msg_lc
        or "object is not iterable" in msg_lc
        or "typeerror" in msg_lc
        or "attributeerror" in msg_lc
        # OpenAI semantic 400 returned via LiteLLM's APIConnectionError wrapper
        or "invalid user message" in msg_lc
        or "invalid message" in msg_lc
        or "invalid request" in msg_lc
        or "invalid_request_error" in msg_lc
        or "unsupported parameter" in msg_lc
        or "invalid content" in msg_lc
        or "invalid image" in msg_lc
        or "unsupported image" in msg_lc
    ):
        return 400, "bad_request_internal", msg

    # Auth / quota at upstream.
    if status in (401, 403) or "Authentication" in cls or "Permission" in cls:
        return 502, "upstream_auth_failed", msg

    # Not found (model unknown at upstream).
    if status == 404 or "NotFound" in cls:
        return 404, "model_not_found_upstream", msg

    # Rate limit (upstream says slow down).
    if status == 429 or "RateLimit" in cls or "TooManyRequests" in cls:
        return 429, "upstream_rate_limit", msg

    # Timeout / network.
    if "Timeout" in cls or "ConnectError" in cls or "Connection" in cls:
        return 502, "upstream_unreachable", msg

    # Default: server error.
    return 502, "upstream_error", f"{cls}: {msg}"


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


def _is_balance_error(exc: Exception) -> bool:
    """True when the upstream signalled insufficient credit / balance.

    Detects HTTP 402 (Payment Required) and the common error wordings:
    DeepSeek ("Account balance too low", "Please add credits"), OpenAI
    ("insufficient_quota"), Anthropic ("credit balance is too low"),
    Together / Groq / Mistral variants. The check is run only in the
    failover loop's error branch, so the per-call cost is paid only
    when something failed already.
    """
    if getattr(exc, "status_code", None) == 402:
        return True
    msg = str(exc).lower()
    return (
        "insufficient_quota" in msg          # OpenAI
        or "insufficient balance" in msg     # DeepSeek
        or "balance too low" in msg          # DeepSeek wording variant
        or "account balance" in msg          # DeepSeek
        or "add credits" in msg              # DeepSeek
        or "out of credits" in msg
        or "insufficient credit" in msg
        or "credit balance" in msg           # Anthropic
    )


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

"""Audio transcription dispatch.

Fully isolated from the chat-completions dispatch path on purpose:
a bug in this module must NOT take down `/v1/chat/completions`. The
audio router catches every exception this function might raise and
maps it to an HTTP error specific to the audio endpoint.

Resolution mirrors the chat path one-to-one:
  1. Alias lookup -> ``ResolvedDispatch`` (reuses the same cache,
     same routes table, same credential injection).
  2. Failover loop: walk priority-ordered routes, retry next on
     retriable upstream errors. Cache pre-filters unhealthy routes,
     just like for chat.

Only the LiteLLM call shape differs. Where chat uses
``litellm.acompletion(messages=..., model=...)`` we use
``litellm.atranscription(file=..., model=..., response_format=...)``
with the in-memory audio bytes.

The chat side's complexity (truncation, response cache, streaming,
tool calls, reasoning bumps, image aging) does not apply here -
transcription is a single in -> single out request.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, BinaryIO

logger = logging.getLogger(__name__)


@dataclass
class AudioDispatchTrace:
    """Per-request observability for audio failover. Mirrors
    ``DispatchTrace`` from the chat path so the response-header layer
    can format X-Digitorn-* headers identically."""

    served_by: str = ""
    route_id: str = ""
    attempts: int = 0
    trail: list[str] = field(default_factory=list)


class AudioModelNotConfigured(RuntimeError):
    """The requested alias has no audio-enabled route."""


def _is_failover_eligible(exc: Exception) -> bool:
    """Same decision as the chat path: anything that screams "provider
    issue" (5xx, rate limit, timeout, connection) retries next route.
    Caller-side bugs (400 / bad request) do NOT retry - they would
    fail identically on every provider."""
    cls = type(exc).__name__
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None)
    if status == 400 or "BadRequest" in cls or "InvalidRequest" in cls:
        return False
    return True


def _is_rate_limit_error(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 429:
        return True
    cls = type(exc).__name__
    if "RateLimit" in cls or "TooManyRequests" in cls:
        return True
    msg = str(exc).lower()
    return "rate limit" in msg or "too many requests" in msg


def _litellm_model_from_compat(compat: str, provider_slug: str, real: str) -> str:
    """Same convention as chat: pick the right LiteLLM identifier
    based on compat. Whisper is mostly served via the OpenAI shape
    so most cases hit the ``slug/real`` branch (openai/whisper-1,
    groq/whisper-large-v3, azure/whisper-1, ...)."""
    slug = (provider_slug or "").lower()
    if compat == "bedrock":
        return f"bedrock/{real}"
    if compat == "azure":
        return f"azure/{real}"
    if compat == "openai_compat":
        return f"openai/{real}"
    if slug == "openai" and compat == "openai":
        return real
    return f"{slug}/{real}"


async def dispatch_transcription(
    *,
    audio_bytes: bytes,
    filename: str,
    alias: str,
    language: str | None = None,
    prompt: str | None = None,
    response_format: str = "verbose_json",
    temperature: float | None = None,
    trace: AudioDispatchTrace | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run an audio transcription. Returns ``(response_dict, telemetry)``.

    ``telemetry`` carries metadata the caller's accounting layer needs:
      - provider: canonical slug we ended up on
      - real_model: the upstream model id
      - cost_per_minute: per-minute price from the catalogue
      - latency_ms: dispatch latency

    Raises ``AudioModelNotConfigured`` if no route is configured for
    the alias, or the underlying LiteLLM exception on definitive
    upstream failure (caller maps to HTTP via ``classify_upstream_error``).
    """
    from digitorn_gateway.config_cache import get_cache as _get_cache
    from digitorn_gateway.config import get_settings as _get_settings

    if not alias:
        raise ValueError("missing model alias")

    cache = _get_cache()
    settings = _get_settings()
    t0 = time.monotonic()

    resolved = cache.resolve_dispatch(alias)
    if resolved is None or resolved.is_custom:
        # Audio has no custom-router escape hatch. If you want to plug
        # in faster-whisper-as-a-service or a self-hosted Whisper API,
        # configure it as an openai_compat provider with a base_url.
        raise AudioModelNotConfigured(
            f"audio_alias_not_configured: {alias} "
            "(no priority-0 route or alias missing from catalog)"
        )

    import litellm

    max_attempts = (
        settings.failover_max_attempts if settings.failover_enabled else 1
    )
    tried_route_ids: set = set()
    attempted: list[tuple[str | None, str]] = []
    last_exc: Exception | None = None
    upstream_resp = None

    for idx in range(max_attempts):
        r_at = cache.resolve_dispatch_excluding(alias, tried_route_ids)
        if r_at is None:
            break

        litellm_model = _litellm_model_from_compat(
            r_at.compat, r_at.provider_slug, r_at.real_model_id,
        )
        kwargs: dict[str, Any] = {"model": litellm_model}
        if r_at.api_key:
            kwargs["api_key"] = r_at.api_key
        if r_at.base_url:
            kwargs["api_base"] = r_at.base_url
        if r_at.extra_headers:
            kwargs["extra_headers"] = dict(r_at.extra_headers)
        if language:
            kwargs["language"] = language
        if prompt:
            kwargs["prompt"] = prompt
        if temperature is not None:
            kwargs["temperature"] = float(temperature)
        # verbose_json is the format that carries ``duration`` for cost
        # computation. Caller can still expose the raw response shape
        # to the client.
        kwargs["response_format"] = response_format

        attempted.append((
            str(r_at.route_id) if r_at.route_id else None,
            r_at.provider_slug,
        ))
        if r_at.route_id is not None:
            tried_route_ids.add(r_at.route_id)

        inflight_cid = r_at.credential_id
        if inflight_cid is not None:
            cache.mark_dispatch_started(inflight_cid)

        # LiteLLM's atranscription wants a file-like object; an in-memory
        # BufferedReader satisfies the contract without touching disk.
        from io import BytesIO
        buf = BytesIO(audio_bytes)
        buf.name = filename or "audio.webm"
        try:
            litellm_resp = await litellm.atranscription(
                file=buf, **kwargs,
            )
            if r_at.route_id is not None:
                cache.mark_route_success(r_at.route_id)
            if inflight_cid is not None:
                cache.mark_credential_success(inflight_cid)
            upstream_resp = (
                litellm_resp.model_dump() if hasattr(litellm_resp, "model_dump")
                else (litellm_resp.dict() if hasattr(litellm_resp, "dict")
                      else dict(litellm_resp))
            )
            resolved = r_at
            break
        except Exception as exc:
            last_exc = exc
            if r_at.route_id is not None:
                cache.mark_route_failure(
                    r_at.route_id, f"{type(exc).__name__}: {exc}"[:200],
                )
            if inflight_cid is not None and _is_rate_limit_error(exc):
                cache.mark_credential_429(inflight_cid, retry_after_s=None)
            if not _is_failover_eligible(exc):
                # Bad request side; propagate so the API layer returns 4xx.
                raise
        finally:
            if inflight_cid is not None:
                cache.mark_dispatch_finished(inflight_cid)

    if upstream_resp is None:
        if last_exc is not None:
            raise last_exc
        raise AudioModelNotConfigured(
            f"no_route_for_alias: {alias} attempts={attempted}",
        )

    if trace is not None:
        trace.served_by = resolved.provider_slug
        trace.attempts = len(attempted) or 1
        trace.trail = [a[1] for a in attempted] if attempted else [resolved.provider_slug]
        if resolved.route_id is not None:
            trace.route_id = str(resolved.route_id)

    telemetry = {
        "provider": resolved.provider_slug,
        "real_model": resolved.real_model_id,
        "cost_per_minute": float(
            getattr(resolved, "cost_per_minute_audio", 0) or 0
        ),
        "latency_ms": (time.monotonic() - t0) * 1000.0,
    }
    return upstream_resp, telemetry

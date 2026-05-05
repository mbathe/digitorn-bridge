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


# ── Non-streaming ──────────────────────────────────────────────────


async def dispatch(
    *,
    body: dict[str, Any],
) -> tuple[dict[str, Any], UsageRecord]:
    """Run a non-streaming chat completion. Returns the OpenAI-shaped
    response dict + a UsageRecord pre-filled with provider/model and
    token counts (no user_id - the route fills that in)."""
    alias = body.get("model")
    if not alias:
        raise ValueError("missing 'model' field in request body")

    entry = resolve_alias(alias)
    t0 = time.monotonic()

    if entry is not None and entry.is_custom:
        router = get_custom_router()
        try:
            resp = await router.handle(entry=entry, body=body)
        except CustomProviderNotImplemented:
            raise
        provider = entry.provider
        provider_model = entry.model
    else:
        # LiteLLM path. We import lazily so the rest of the package
        # is testable without LiteLLM installed.
        import litellm

        litellm_model = entry.litellm_model_id() if entry else alias
        # Build a clean kwargs dict from the OpenAI request body.
        # LiteLLM accepts: messages, temperature, max_tokens,
        # tools, tool_choice, response_format, stop, top_p,
        # frequency_penalty, presence_penalty, user.
        passthrough = {
            k: v for k, v in body.items()
            if k in {
                "messages", "temperature", "max_tokens", "top_p",
                "stop", "frequency_penalty", "presence_penalty",
                "tools", "tool_choice", "response_format", "seed",
                "logprobs", "top_logprobs", "n",
            }
        }
        litellm_resp = await litellm.acompletion(
            model=litellm_model,
            **passthrough,
        )
        # litellm returns its own response object; convert to dict
        # so the route can json-serialise. ModelResponse exposes
        # .json() / .model_dump() depending on the version.
        if hasattr(litellm_resp, "model_dump"):
            resp = litellm_resp.model_dump()
        elif hasattr(litellm_resp, "dict"):
            resp = litellm_resp.dict()
        else:
            resp = dict(litellm_resp)
        provider = (entry.provider if entry else _provider_from_model(litellm_model))
        provider_model = (entry.model if entry else litellm_model)

    latency_ms = (time.monotonic() - t0) * 1000

    # Extract usage. OpenAI-shaped response has `.usage.prompt_tokens`
    # / `.completion_tokens`. LiteLLM mirrors this.
    usage = resp.get("usage") or {}
    record = UsageRecord(
        user_id="",  # route fills in
        model_alias=alias,
        provider=provider,
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        cost_usd=_compute_cost(
            entry,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        ),
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
    alias = body.get("model")
    if not alias:
        raise ValueError("missing 'model' field in request body")

    entry = resolve_alias(alias)

    if entry is not None and entry.is_custom:
        router = get_custom_router()
        async for chunk in router.handle_stream(entry=entry, body=body):
            yield chunk
        return

    import litellm

    litellm_model = entry.litellm_model_id() if entry else alias
    passthrough = {
        k: v for k, v in body.items()
        if k in {
            "messages", "temperature", "max_tokens", "top_p",
            "stop", "frequency_penalty", "presence_penalty",
            "tools", "tool_choice", "response_format", "seed",
            "logprobs", "top_logprobs", "n",
        }
    }
    stream = await litellm.acompletion(
        model=litellm_model,
        stream=True,
        **passthrough,
    )
    async for chunk in stream:
        if hasattr(chunk, "model_dump"):
            yield chunk.model_dump()
        elif hasattr(chunk, "dict"):
            yield chunk.dict()
        else:
            yield dict(chunk)


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

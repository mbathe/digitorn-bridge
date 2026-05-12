"""Cache-aware cost computation.

One module, one truth. Every code path that needs to turn (tokens,
prices) into a dollar amount goes through ``compute_cost()``. The
billing policy lives here, not scattered across dispatch / streaming
/ BackgroundTask.

Policy (validated against current provider docs as of 2026-05):

  * OpenAI returns ``usage.prompt_tokens_details.cached_tokens``: a
    subset of ``prompt_tokens`` that hit OpenAI's automatic prompt
    cache. Charged at 50 percent (gpt-4o family) or 25 percent (older).
    No explicit "cache write" - cache writes are part of normal input.
  * Anthropic returns ``usage.cache_read_input_tokens`` and
    ``usage.cache_creation_input_tokens`` AS SEPARATE FIELDS that are
    NOT subtracted from ``prompt_tokens`` in their /v1/messages API.
    LiteLLM normalises this so we read them from the same usage dict
    when present.
  * Gemini returns ``usage.cache_create_input_tokens`` and
    ``usage.cached_content_token_count`` via Vertex/AI Studio.
  * Providers without prompt cache (DeepSeek, Mistral, xAI, Cohere,
    Groq, ...) simply do not return these fields; we treat the cache
    counts as 0.

Honest defaults: every cache price column starts at 0 in the catalog.
If the operator has not seeded prices, cache tokens contribute 0 to
cost (under-bill rather than guess). We never silently fall back to
the regular input price - that would hide the operator's omission.

Token-accounting subtleties:
  * For OpenAI, cached_tokens is a SUBSET of prompt_tokens. So the
    "non-cached input" is ``prompt_tokens - cached_tokens``.
  * For Anthropic, cache_read_input_tokens and cache_creation_input_tokens
    are NOT in prompt_tokens. So the non-cached input is just
    ``prompt_tokens`` and cache tokens are billed separately on top.

Our extractor returns ``(prompt_tokens_non_cached, cache_read,
cache_write)`` already disambiguated, so ``compute_cost`` just adds
the three pieces. No conditional logic per provider.
"""
from __future__ import annotations

from typing import Any


def extract_tokens(usage: dict[str, Any] | None) -> tuple[int, int, int, int]:
    """Return ``(input_non_cached, cache_read, cache_write, output)``.

    Reads every shape we have seen across providers. Missing fields
    default to 0. Always returns non-negative ints.
    """
    if not isinstance(usage, dict):
        return 0, 0, 0, 0

    prompt_total = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)

    # OpenAI: ``prompt_tokens_details.cached_tokens`` is a SUBSET of
    # prompt_tokens. (Not present on older models.)
    openai_cached = 0
    details = usage.get("prompt_tokens_details") or {}
    if isinstance(details, dict):
        openai_cached = int(details.get("cached_tokens") or 0)

    # Anthropic / Gemini: standalone keys NOT included in prompt_tokens.
    anthropic_read = int(usage.get("cache_read_input_tokens") or 0)
    anthropic_write = int(usage.get("cache_creation_input_tokens") or 0)
    # Vertex / AI Studio Gemini uses different names.
    gemini_read = int(usage.get("cached_content_token_count") or 0)
    gemini_write = int(usage.get("cache_create_input_tokens") or 0)

    # Pick whichever pair is present. Both providers don't return both
    # families in the same response, so the max() is safe (and protects
    # if litellm normalises by copying values across keys).
    cache_read = max(openai_cached, anthropic_read, gemini_read)
    cache_write = max(anthropic_write, gemini_write)

    # OpenAI's cached_tokens IS inside prompt_tokens - subtract.
    # For Anthropic/Gemini, prompt_tokens does NOT include cache; we
    # don't subtract. Heuristic: if cache_read > prompt_total it's
    # the Anthropic shape (or a wonky payload); if cache_read <=
    # prompt_total it's OpenAI shape and we subtract.
    if openai_cached and cache_read <= prompt_total:
        non_cached_input = prompt_total - cache_read
    else:
        non_cached_input = prompt_total

    return (
        max(0, non_cached_input),
        max(0, cache_read),
        max(0, cache_write),
        max(0, completion),
    )


def compute_cost(
    *,
    input_non_cached: int,
    cache_read: int,
    cache_write: int,
    output: int,
    price_per_1k_input: float,
    price_per_1k_output: float,
    price_per_1k_cache_read: float = 0.0,
    price_per_1k_cache_write: float = 0.0,
) -> float:
    """Apply the 4-tier cost formula. Returns USD rounded to 8 decimals
    (one extra over the catalog's 6 to keep cumulative rounding noise
    below $0.01 over a million calls).

    Any price set to 0 in the catalog contributes 0 - honest default
    for providers without cache or models we haven't priced yet.
    """
    cost = (
        (max(0, input_non_cached) / 1000.0) * float(price_per_1k_input or 0)
        + (max(0, cache_read) / 1000.0) * float(price_per_1k_cache_read or 0)
        + (max(0, cache_write) / 1000.0) * float(price_per_1k_cache_write or 0)
        + (max(0, output) / 1000.0) * float(price_per_1k_output or 0)
    )
    return round(cost, 8)


def compute_cost_for_resolved(
    *,
    input_non_cached: int,
    cache_read: int,
    cache_write: int,
    output: int,
    resolved: Any,
) -> float:
    """Convenience wrapper that reads prices from a ``ResolvedDispatch``
    (or ``CachedModel``) object. Used by the dispatch hot path."""
    return compute_cost(
        input_non_cached=input_non_cached,
        cache_read=cache_read,
        cache_write=cache_write,
        output=output,
        price_per_1k_input=getattr(resolved, "cost_per_1k_input", 0) or 0,
        price_per_1k_output=getattr(resolved, "cost_per_1k_output", 0) or 0,
        price_per_1k_cache_read=getattr(resolved, "cost_per_1k_cache_read", 0) or 0,
        price_per_1k_cache_write=getattr(resolved, "cost_per_1k_cache_write", 0) or 0,
    )

"""Auto-truncation safety net for /v1/chat/completions.

Two consumers share this module:

  * ``main.chat_completions`` calls ``count_tokens`` + ``check_overflow``
    once pre-dispatch. When the request would overflow the resolved
    model's context window, it short-circuits into a clean 413 with
    a trim instruction instead of letting the provider return an
    opaque 400 after a full RTT.

  * ``llm_call.dispatch`` / ``dispatch_stream`` call ``head_drop`` per
    route during the failover loop. The routing layer already walks
    multiple providers; when the loop lands on a route whose
    ``max_context`` is smaller than the request, head_drop trims the
    oldest non-system messages so the fallback succeeds.

Design constraints:

  * Tokenizer (``litellm.token_counter``) is invoked ONLY when
    ``settings.truncate_enabled=True`` is honoured by the caller. The
    cost of being off is one ``if`` check.
  * ``head_drop`` preserves invariants any sane provider relies on:
      - all ``role=system`` messages stay (always at the top);
      - the LAST user message stays (it's the actual question);
      - assistant-with-tool_calls + immediately-following tool messages
        travel as an atomic block - we never split a tool pair, since
        every provider rejects an orphan tool_response with a 400.
  * No exceptions escape: tokenizer failure (unknown model, malformed
    message) falls back to a 4-chars-per-token estimate. The caller
    treats the result as best-effort.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ── Tokenization ─────────────────────────────────────────────────────


def count_tokens(model: str, messages: list[dict[str, Any]]) -> int:
    """Best-effort token count for a chat completion body.

    Uses ``litellm.token_counter`` (BPE-identical to what the provider
    will count). Falls back to a 4-chars-per-token estimate when the
    tokenizer doesn't know the model or the message shape is exotic.
    Never raises - the caller treats the return as advisory.
    """
    if not messages:
        return 0
    try:
        import litellm
        return int(litellm.token_counter(model=model, messages=messages))
    except Exception as exc:
        logger.debug("token_counter_failed model=%s (%s)", model, exc)
        return _estimate_tokens(messages)


def can_skip_tokenization(
    messages: list[dict[str, Any]],
    max_context: int | None,
) -> bool:
    """Fast byte-size pre-check that proves the request CANNOT overflow
    a given context window without invoking the tokenizer.

    Worst-case BPE encoding is ~1 token per byte of UTF-8 (single-byte
    ASCII punctuation, no merges). If the total byte length of the
    messages stays under ``max_context * 3`` we have a comfortable
    margin: no realistic prompt overflows at that ratio. The check
    runs in O(N) with len() over strings, dominated by Python loop
    overhead - microseconds for typical bodies.

    The point: when truncate_enabled=True, the small-request hot path
    pays the cost of THIS function (a sum-of-lens, free) and skips
    the real tokenizer. Only requests that COULD overflow pay for
    real BPE counting.
    """
    if not max_context or max_context <= 0:
        return True
    total_chars = 0
    threshold = max_context * 3
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total_chars += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total_chars += len(part["text"])
                else:
                    # Vision parts: assume 1k chars worth of tokens.
                    total_chars += 4_000
        if total_chars >= threshold:
            return False
    return True


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Coarse fallback when litellm can't tokenise. 4 chars/token is
    the OpenAI-published rule of thumb. Errs on the high side for
    multimodal so the safety net stays conservative."""
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += (len(content) // 4) + 8
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    total += 8
                    continue
                if part.get("type") == "text":
                    total += (len(part.get("text") or "") // 4) + 8
                elif part.get("type") in ("image_url", "image"):
                    # Vision tokens vary widely by detail; budget 1k as
                    # a safe upper bound.
                    total += 1000
                else:
                    total += 16
        else:
            total += 16
        # Role + framing overhead per message (matches OpenAI's
        # ChatML accounting roughly).
        total += 4
    return total


# ── Pre-flight overflow check (Mode 1) ───────────────────────────────


def get_max_context_for_model(model: str) -> int | None:
    """Look up the upstream context window for a route's real_model_id.

    Used by Mode 2 (head_drop on fallback): the alias's metadata
    advertises the PRIMARY route's max_context (often Claude 200k),
    which over-states the budget for a fallback route on a smaller
    model. We need the actual model's window. LiteLLM ships a
    catalogue keyed by canonical name OR ``provider/model``; we try
    both shapes and prefer ``max_input_tokens`` when present.

    Returns ``None`` when the model is unknown - the caller skips
    head_drop and lets dispatch handle whatever the provider returns.
    """
    if not model:
        return None
    try:
        import litellm
        catalog = getattr(litellm, "model_cost", None) or {}
        # Direct hit by canonical name.
        info = catalog.get(model)
        if not info:
            # Walk the catalog for a suffix or stripped match. Cheap:
            # this only fires when the dispatch loop is already in a
            # degraded path.
            for k, v in catalog.items():
                if k == model or k.endswith("/" + model):
                    info = v
                    break
        if not info:
            return None
        for key in ("max_input_tokens", "max_tokens"):
            v = info.get(key)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
    except Exception as exc:
        logger.debug("max_context_lookup_failed model=%s (%s)", model, exc)
    return None


def check_overflow(
    request_tokens: int,
    max_context: int | None,
    max_output_tokens: int,
) -> tuple[bool, int]:
    """Return ``(overflows, budget)`` for the given counts.

    ``overflows=True`` when the request alone consumes more than the
    available input budget. ``budget`` is ``max_context -
    max_output_tokens`` (clamped to >=0) so the caller can quote it in
    the error.

    Returns ``(False, 0)`` when ``max_context`` is unknown - the
    catalog row hasn't filled it in. Better to dispatch and let the
    provider answer than to reject blindly.
    """
    if not max_context or max_context <= 0:
        return False, 0
    budget = max(0, int(max_context) - int(max_output_tokens))
    return request_tokens > budget, budget


# ── Head-drop (Mode 2) ───────────────────────────────────────────────


def head_drop(
    messages: list[dict[str, Any]],
    max_input_tokens: int,
    model: str,
) -> tuple[list[dict[str, Any]], int]:
    """Drop oldest non-system messages until the body fits in
    ``max_input_tokens``. Returns ``(truncated_messages, dropped)``.

    Invariants preserved:
      * Every system message stays.
      * The last "turn block" (most recent assistant+tool group, or
        the last standalone message) always stays. If even that one
        block alone exceeds the budget, we return what we have - the
        caller's pre-flight reject (Mode 1) already prevented this
        case, but defensive code keeps things sane in tests.
      * Tool turns are atomic: an assistant message carrying
        ``tool_calls`` is glued to every immediately-following
        ``role=tool`` response; the group is kept or dropped together,
        never split.

    The walk is FROM THE END BACKWARDS so newer (more relevant)
    messages are kept first. On the first block that wouldn't fit we
    stop - skipping mid-history to keep older messages would create
    confusing gaps.
    """
    if not messages:
        return messages, 0

    system_msgs: list[dict[str, Any]] = []
    non_system: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "system":
            system_msgs.append(m)
        else:
            non_system.append(m)

    if not non_system:
        return messages, 0

    blocks = _group_turn_blocks(non_system)
    if not blocks:
        return system_msgs, len(messages) - len(system_msgs)

    sys_tokens = count_tokens(model, system_msgs) if system_msgs else 0
    last_block = blocks[-1]
    last_tokens = count_tokens(model, last_block)

    # Always keep the last block. If sys + last already exceeds the
    # budget there is nothing safe to drop - return what we have.
    keep: list[list[dict[str, Any]]] = [last_block]
    remaining = max_input_tokens - sys_tokens - last_tokens
    if remaining <= 0:
        kept = system_msgs + [m for blk in keep for m in blk]
        return kept, len(messages) - len(kept)

    # Walk older blocks newest-first, keep while they fit.
    for block in reversed(blocks[:-1]):
        bt = count_tokens(model, block)
        if bt > remaining:
            break
        keep.insert(0, block)
        remaining -= bt

    kept = system_msgs + [m for blk in keep for m in blk]
    return kept, len(messages) - len(kept)


def _group_turn_blocks(
    messages: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Split a non-system message list into atomic blocks where each
    block is either:

      * one single user / assistant-without-tools message, or
      * one assistant-with-tool_calls glued to every immediately
        following ``role=tool`` response.

    Splitting an assistant tool_calls turn from its tool responses
    triggers ``invalid_request_error: tool_call_id <X> not found`` on
    every provider, so the grouping rule is non-negotiable.
    """
    blocks: list[list[dict[str, Any]]] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if (
            msg.get("role") == "assistant"
            and msg.get("tool_calls")
        ):
            block = [msg]
            j = i + 1
            while j < n and messages[j].get("role") == "tool":
                block.append(messages[j])
                j += 1
            blocks.append(block)
            i = j
        else:
            blocks.append([msg])
            i += 1
    return blocks

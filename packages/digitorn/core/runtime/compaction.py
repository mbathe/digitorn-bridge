"""Context compaction - emergency overflow handling and truncation."""

from __future__ import annotations

import logging
from typing import Any

from digitorn.core.runtime.types import AgentContext

logger = logging.getLogger(__name__)


def is_context_overflow(exc: Exception) -> bool:
    """Detect if an exception is a context window overflow error."""
    msg = str(exc).lower()
    return any(p in msg for p in (
        "maximum context length",
        "context_length_exceeded",
        "context window",
        "reduce the length of the messages",
        "too many tokens",
        "token limit",
    ))


async def aestimate_tokens(
    messages: list[dict[str, Any]],
    *,
    provider: Any = None,
    model: str | None = None,
) -> int:
    """Async wrapper around :func:`estimate_tokens`.

    The provider tokenizers (litellm + tiktoken / Anthropic offline /
    HuggingFace) are CPU-bound and the first call also triggers a model
    load (10s+ for HF). Off-loaded so the event loop keeps serving
    Socket.IO pings under load. Use this from any async context;
    :func:`estimate_tokens` stays for sync callers.
    """
    import asyncio as _asyncio
    return await _asyncio.to_thread(
        estimate_tokens, messages, provider=provider, model=model,
    )


def estimate_tokens(
    messages: list[dict[str, Any]],
    *,
    provider: Any = None,
    model: str | None = None,
) -> int:
    """Real token count for ``messages``.

    Resolution order:

    1. ``provider.count_message_tokens(messages)`` when a provider is
       passed - uses the provider's exact tokenizer (litellm under the
       hood, which routes to ``tiktoken`` for OpenAI / DeepSeek, the
       Anthropic offline tokenizer for Claude 3+, HuggingFace for
       Mistral / Llama / Qwen / Gemini). Cached internally.
    2. ``litellm.token_counter`` directly when ``model`` is known.
       Falls back to ``cl100k_base`` for unknown models.
    3. The crude ``len(text) // 4`` heuristic ONLY when both lookups
       fail (litellm import error, network-resolved tokenizer
       unreachable). Logged at WARNING so the operator sees it -
       silently drifting between heuristic and real counts skews every
       UI pressure indicator built on top.

    Pass ``provider=ctx.provider`` from any agent_loop call site so the
    count uses the live model's tokenizer instead of a generic 4-char
    rule.
    """
    # Path 1: provider tokenizer (preferred - handles overhead / per-message
    # boilerplate / tool definitions exactly the way the API will).
    if provider is not None and hasattr(provider, "count_message_tokens"):
        try:
            return int(provider.count_message_tokens(messages))
        except Exception as exc:
            logger.debug(
                "estimate_tokens: provider.count_message_tokens failed (%s); "
                "falling through to litellm",
                exc,
            )

    # Path 2: litellm direct (when caller knows the model name).
    if model:
        try:
            from litellm import token_counter
            return int(token_counter(model=model, messages=messages))
        except Exception as exc:
            logger.debug(
                "estimate_tokens: litellm.token_counter(model=%s) failed (%s); "
                "falling back to crude estimate",
                model, exc,
            )

    # Path 3: last-resort heuristic. Logged at WARNING because every
    # call here means a context-pressure indicator is using a fake
    # number - we want this visible, not silent.
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += len(part.get("text", ""))
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            total += len(fn.get("name", ""))
            args = fn.get("arguments", "")
            total += len(args) if isinstance(args, str) else len(str(args))
    if provider is not None or model is not None:
        # Caller TRIED to use a real tokenizer and we ended up here -
        # that's worth knowing.
        logger.warning(
            "estimate_tokens: real tokenizer unavailable, returned crude "
            "char/4 heuristic for %d messages", len(messages),
        )
    return total // 4


async def emergency_compact(
    ctx: AgentContext,
    messages: list[dict[str, Any]],
    *,
    reason: str = "context_overflow",
    event_bus: Any = None,
    app_id: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Emergency compaction when the LLM returns a context overflow error.

    Uses truncate strategy (no LLM call needed - the LLM is refusing).
    Aggressively reduces context to ~50% of max.

    ``reason`` is stamped on the durable ``compaction`` event persisted
    to ``history_log``: ``context_overflow`` (agent_loop auto-trigger),
    ``manual`` (``POST /sessions/{sid}/compact``), or any caller-supplied
    label. It doesn't affect the compaction algorithm - only the audit
    trail and the resume-time telemetry.

    ``event_bus`` / ``app_id`` / ``session_id`` / ``user_id`` override
    the corresponding fields when ``ctx`` is a shared template (e.g.
    the manual API endpoint uses ``deployed.entry_context`` which
    isn't wired to a specific session). Pass them explicitly whenever
    the call site isn't inside ``_chat_locked``.
    """
    from digitorn.core.runtime.hooks import (
        TurnState,
        _build_context_reminder,
        _do_truncate,
        _find_safe_split_point,
    )
    from digitorn.core.runtime.compaction_persistence import (
        emit_compaction_event,
    )

    cc = ctx.context_config
    keep_recent = max(cc.keep_recent // 2, 4)

    system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
    conversation = messages[1:] if system_msg else messages[:]

    if len(conversation) <= keep_recent:
        truncate_oversized_messages(messages, cc.max_tokens)
        logger.warning("emergency_compact: truncated oversized messages (%d msgs)", len(conversation))
        return

    tokens_before = await aestimate_tokens(
        messages, provider=getattr(ctx, "provider", None),
    )

    try:
        safe_keep = _find_safe_split_point(conversation, keep_recent)
    except Exception as exc:
        logger.warning("safe_split_point_failed: %s, using keep_recent=%d", exc, keep_recent)
        safe_keep = keep_recent
    if safe_keep <= 0 or safe_keep > len(conversation):
        to_compact: list[dict] = []
        to_keep = conversation
    else:
        to_compact = conversation[:-safe_keep]
        to_keep = conversation[-safe_keep:]

    # RT6: if the safe split would compact nothing (edge case where the
    # entire conversation is "in flight" tool calls/results that can't be
    # split), force aggressive truncation. We MUST reduce context - the
    # alternative is an infinite retry loop with the same overflow error.
    if not to_compact:
        logger.warning(
            "emergency_compact: safe split returned empty, forcing aggressive "
            "truncation of oversized messages and dropping oldest half",
        )
        # First try the per-message truncation
        truncate_oversized_messages(messages, cc.max_tokens)
        # If conversation is still long, drop the oldest half regardless of pairing
        # (we'll lose tool_call/tool_result coherence but the LLM will adapt)
        if len(conversation) > 4:
            half = len(conversation) // 2
            to_compact = conversation[:half]
            to_keep = conversation[half:]
            recent_messages_before = list(to_keep)
            context_reminder = _build_context_reminder(
                ctx.context_builder,
                ctx.tool_injection,
                memory_module=ctx.memory_module,
                agent_context=ctx,
                recent_messages=to_keep,
            )
            summary_text = _do_truncate(
                messages, system_msg, to_compact, to_keep, context_reminder,
            )
            logger.warning(
                "emergency_compact: forced drop of %d oldest messages",
                len(to_compact),
            )
            await emit_compaction_event(
                ctx,
                reason=reason,
                strategy="truncate",
                summary_text=summary_text,
                tokens_before=tokens_before,
                tokens_after=await aestimate_tokens(
                    messages, provider=getattr(ctx, "provider", None),
                ),
                to_keep_count=len(to_keep),
                recent_messages_before=recent_messages_before,
                event_bus=event_bus,
                app_id=app_id,
                session_id=session_id,
                user_id=user_id,
            )
        return

    recent_messages_before = list(to_keep)
    context_reminder = _build_context_reminder(
        ctx.context_builder,
        ctx.tool_injection,
        memory_module=ctx.memory_module,
        agent_context=ctx,
        recent_messages=to_keep,
    )

    summary_text = _do_truncate(
        messages, system_msg, to_compact, to_keep, context_reminder,
    )
    truncate_oversized_messages(messages, cc.max_tokens)

    logger.warning(
        "emergency_compact: truncated %d messages, kept %d",
        len(to_compact), len(to_keep),
    )

    await emit_compaction_event(
        ctx,
        reason=reason,
        strategy="truncate",
        summary_text=summary_text,
        tokens_before=tokens_before,
        tokens_after=await aestimate_tokens(
            messages, provider=getattr(ctx, "provider", None),
        ),
        to_keep_count=len(to_keep),
        recent_messages_before=recent_messages_before,
        event_bus=event_bus,
        app_id=app_id,
        session_id=session_id,
        user_id=user_id,
    )


def truncate_oversized_messages(
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> None:
    """Truncate individual messages whose content exceeds a safe size."""
    max_chars = max((max_tokens // 4) * 4, 4000)

    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) > max_chars:
            role = msg.get("role", "unknown")
            cut = max_chars - 500
            msg["content"] = content[:cut] + (
                f"\n\n EMERGENCY TRUNCATION: this {role} message was "
                f"{len(content)} characters but exceeded the context window. "
                f"Only the first {cut} characters are shown.\n"
                f"If this was a tool result, the data above is partial. "
                f"Do NOT guess or invent the missing content. "
                f"Use a more specific query to get the information you need."
            )


def snip_oversized_messages(
    messages: list[dict[str, Any]],
    threshold_chars: int = 16000,
) -> int:
    """Proactive snip: trim individual messages > threshold without losing key info.

    Unlike emergency truncation, this is gentle:
    - Tool results: keep first + last 200 lines, note middle was snipped
    - Long assistant content: keep first 2000 + last 500 chars
    - System messages: never touched
    - Recent 4 messages: never touched (may be in-progress)

    Returns number of messages snipped.
    """
    snipped = 0
    # Don't snip the last 4 messages (in-progress conversation)
    safe_end = max(len(messages) - 4, 1)

    for i in range(safe_end):
        msg = messages[i]
        if msg.get("role") == "system":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or len(content) <= threshold_chars:
            continue

        role = msg.get("role", "")
        original_len = len(content)

        if role == "tool":
            # Tool result: keep first 6000 + last 2000 chars
            head = content[:6000]
            tail = content[-2000:]
            msg["content"] = (
                f"{head}\n\n"
                f"[... {original_len - 8000} characters snipped from tool result ...]\n\n"
                f"{tail}"
            )
        else:
            # Assistant/user: keep first 4000 + last 1000 chars
            head = content[:4000]
            tail = content[-1000:]
            msg["content"] = (
                f"{head}\n\n"
                f"[... {original_len - 5000} characters snipped ...]\n\n"
                f"{tail}"
            )
        snipped += 1

    if snipped:
        logger.info("snip_oversized: snipped %d messages (threshold=%d chars)", snipped, threshold_chars)
    return snipped

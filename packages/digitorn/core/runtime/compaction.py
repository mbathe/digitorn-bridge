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
    """Async wrapper around estimate_tokens (off-loaded; tokenizers are CPU-bound)."""
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
    """Token count: provider tokenizer, then litellm, then char/4 fallback."""
    if provider is not None and hasattr(provider, "count_message_tokens"):
        try:
            return int(provider.count_message_tokens(messages))
        except Exception as exc:
            logger.debug(
                "estimate_tokens: provider.count_message_tokens failed (%s); "
                "falling through to litellm",
                exc,
            )

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
        logger.warning(
            "estimate_tokens: real tokenizer unavailable, returned crude "
            "char/4 heuristic for %d messages", len(messages),
        )
    # crude char/4 heuristic
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
) -> dict[str, Any]:
    """Emergency truncate-strategy compaction on context overflow."""
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
        return {
            "compacted": False,
            "tokens_before": 0,
            "tokens_after": 0,
            "to_compact_count": 0,
            "to_keep_count": len(conversation),
            "reason": f"{reason}_too_short",
        }

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

    # safe split empty (in-flight tool pairs) => force truncation to break overflow retry loop
    if not to_compact:
        logger.warning(
            "emergency_compact: safe split returned empty, forcing aggressive "
            "truncation of oversized messages and dropping oldest half",
        )
        truncate_oversized_messages(messages, cc.max_tokens)
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
            summary_text, full_note = _do_truncate(
                messages, system_msg, to_compact, to_keep, context_reminder,
            )
            logger.warning(
                "emergency_compact: forced drop of %d oldest messages",
                len(to_compact),
            )
            _ta_forced = await aestimate_tokens(
                messages, provider=getattr(ctx, "provider", None),
            )
            await emit_compaction_event(
                ctx,
                reason=reason,
                strategy="truncate",
                summary_text=summary_text,
                tokens_before=tokens_before,
                tokens_after=_ta_forced,
                to_keep_count=len(to_keep),
                recent_messages_before=recent_messages_before,
                event_bus=event_bus,
                app_id=app_id,
                session_id=session_id,
                user_id=user_id,
            )
            await _emit_compaction_directive(
                ctx, full_note, reason=reason, compacted=len(to_compact),
            )
            return {
                "compacted": True,
                "tokens_before": tokens_before,
                "tokens_after": _ta_forced,
                "to_compact_count": len(to_compact),
                "to_keep_count": len(to_keep),
                "reason": reason,
            }
        return {
            "compacted": False,
            "tokens_before": tokens_before,
            "tokens_after": tokens_before,
            "to_compact_count": 0,
            "to_keep_count": len(conversation),
            "reason": f"{reason}_no_split_possible",
        }

    recent_messages_before = list(to_keep)
    # snapshot pre-compaction state for revert if no strategy reduces tokens
    messages_snapshot = [dict(m) for m in messages]

    context_reminder = _build_context_reminder(
        ctx.context_builder,
        ctx.tool_injection,
        memory_module=ctx.memory_module,
        agent_context=ctx,
        recent_messages=to_keep,
    )

    summary_text, full_note = _do_truncate(
        messages, system_msg, to_compact, to_keep, context_reminder,
    )
    truncate_oversized_messages(messages, cc.max_tokens)

    tokens_after = await aestimate_tokens(
        messages, provider=getattr(ctx, "provider", None),
    )

    if tokens_after >= tokens_before:
        logger.warning(
            "emergency_compact: full-reminder bloated context "
            "(before=%d after=%d); retrying without context_reminder",
            tokens_before, tokens_after,
        )
        messages.clear()
        messages.extend(messages_snapshot)
        summary_text, full_note = _do_truncate(
            messages, system_msg, to_compact, to_keep, "",
        )
        truncate_oversized_messages(messages, cc.max_tokens)
        tokens_after = await aestimate_tokens(
            messages, provider=getattr(ctx, "provider", None),
        )

    if tokens_after >= tokens_before:
        logger.warning(
            "emergency_compact: reverting -- compaction cannot reduce "
            "tokens (before=%d after=%d). `to_compact` slice was "
            "shorter than the summary note itself.",
            tokens_before, tokens_after,
        )
        messages.clear()
        messages.extend(messages_snapshot)
        await emit_compaction_event(
            ctx,
            reason=f"{reason}_noop_reverted",
            strategy="truncate",
            summary_text="(reverted: nothing to compact)",
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            to_keep_count=len(to_keep),
            recent_messages_before=recent_messages_before,
            event_bus=event_bus,
            app_id=app_id,
            session_id=session_id,
            user_id=user_id,
        )
        return {
            "compacted": False,
            "tokens_before": tokens_before,
            "tokens_after": tokens_before,
            "to_compact_count": 0,
            "to_keep_count": len(to_keep),
            "reason": f"{reason}_noop_reverted",
        }

    logger.warning(
        "emergency_compact: truncated %d messages, kept %d, "
        "tokens %d -> %d",
        len(to_compact), len(to_keep), tokens_before, tokens_after,
    )

    await emit_compaction_event(
        ctx,
        reason=reason,
        strategy="truncate",
        summary_text=summary_text,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        to_keep_count=len(to_keep),
        recent_messages_before=recent_messages_before,
        event_bus=event_bus,
        app_id=app_id,
        session_id=session_id,
        user_id=user_id,
    )
    await _emit_compaction_directive(
        ctx, full_note, reason=reason, compacted=len(to_compact),
    )
    return {
        "compacted": True,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "to_compact_count": len(to_compact),
        "to_keep_count": len(to_keep),
        "reason": reason,
    }


async def _emit_compaction_directive(
    ctx: Any,
    full_note: str,
    *,
    reason: str,
    compacted: int,
) -> None:
    if not full_note:
        return
    try:
        from digitorn.core.runtime.system_directive import inject_system_directive
        await inject_system_directive(
            ctx,
            content=full_note,
            source="compaction_summary",
            metadata={"strategy": "truncate", "compacted": compacted, "reason": reason},
        )
    except Exception as exc:
        logger.debug("emergency_compact directive emit failed: %s", exc)


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
    """Trim messages > threshold while preserving head/tail."""
    snipped = 0
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
            head = content[:6000]
            tail = content[-2000:]
            msg["content"] = (
                f"{head}\n\n"
                f"[... {original_len - 8000} characters snipped from tool result ...]\n\n"
                f"{tail}"
            )
        else:
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

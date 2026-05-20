"""Generate a short semantic title for a session from its first turn."""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_MAX_TITLE_CHARS = 80
_FIRST_MSG_CLIP = 2000
_FIRST_REPLY_CLIP = 800

_SYSTEM_PROMPT = (
    "You are a title generator. Given the first exchange of a conversation, "
    "return ONE short title (3-7 words, no quotes, no trailing period) that "
    "captures what the USER is trying to do. Reply with just the title - "
    "nothing else, no preamble, no explanation."
)


def _clean_title(raw: str) -> str:
    if not raw:
        return ""
    # Drop any DeepSeek-R1 <think> blocks that may leak through.
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE).strip()
    # Some providers return "Title: X" or numbered lists - strip the prefix.
    raw = re.sub(r"^\s*(?:title|sujet|topic)\s*:\s*", "", raw, flags=re.IGNORECASE)
    # Collapse to a single line, trim quotes/punctuation.
    first_line = raw.splitlines()[0].strip()
    first_line = first_line.strip("\"'`“”«» ")
    first_line = re.sub(r"[.?!]+$", "", first_line).strip()
    return first_line[:_MAX_TITLE_CHARS]


def _extract_text(response: Any) -> str:
    """Pull the plain text out of a ChatResponse."""
    if response is None:
        return ""
    # Direct string attributes
    for attr in ("content", "text"):
        val = getattr(response, attr, None)
        if isinstance(val, str) and val.strip():
            return val
    # Nested message.content
    msg = getattr(response, "message", None)
    if msg is not None:
        for attr in ("content", "text"):
            val = getattr(msg, attr, None)
            if isinstance(val, str) and val.strip():
                return val
            # List of blocks (Anthropic native shape)
            if isinstance(val, list):
                parts = []
                for block in val:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif hasattr(block, "text"):
                        parts.append(getattr(block, "text", ""))
                joined = " ".join(p for p in parts if p)
                if joined.strip():
                    return joined
    return ""


async def generate_semantic_title(
    ctx: Any,
    user_message: str,
    assistant_reply: str = "",
) -> str | None:
    """Ask the LLM for a 3-7 word title"""
    provider = getattr(ctx, "fallback_provider", None) or getattr(ctx, "provider", None)
    if provider is None:
        return None

    um = (user_message or "")[:_FIRST_MSG_CLIP]
    ar = (assistant_reply or "")[:_FIRST_REPLY_CLIP]
    if not um.strip():
        return None

    try:
        from digitorn.modules.llm_provider.providers.base import ChatMessage
    except Exception as exc:
        logger.debug("title_gen_import_failed: %s", exc)
        return None

    user_block = f"User's first message:\n{um}"
    if ar.strip():
        user_block += f"\n\nAssistant's reply:\n{ar}"

    messages = [
        ChatMessage(role="system", content=_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_block),
    ]

    try:
        response = await provider.chat(
            messages,
            temperature=0.3,
            max_tokens=32,
        )
    except Exception as exc:
        logger.debug("title_gen_chat_failed: %s", exc)
        return None

    raw = _extract_text(response)
    cleaned = _clean_title(raw)
    return cleaned or None


async def maybe_update_session_title(
    ctx: Any,
    session: Any,
    session_store: Any = None,
) -> None:
    """Background-safe: generate + persist a semantic title."""
    try:
        msgs = getattr(session, "messages", None) or []
        first_user = ""
        first_assistant = ""
        for m in msgs:
            role = m.get("role", "") if isinstance(m, dict) else ""
            content = m.get("content", "") if isinstance(m, dict) else ""
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            if role == "user" and not first_user:
                first_user = content
            elif role == "assistant" and not first_assistant:
                first_assistant = content
            if first_user and first_assistant:
                break

        if not first_user:
            return

        title = await generate_semantic_title(ctx, first_user, first_assistant)
        if not title:
            return

        session.title = title

        # Persist via the session_store so the new title survives
        # summary() reads + daemon restarts.
        if session_store is not None and hasattr(session_store, "put"):
            try:
                import asyncio
                await asyncio.to_thread(session_store.put, session)
            except Exception as exc:
                logger.debug("title_persist_via_store_failed: %s", exc)

        logger.info(
            "session_title_generated app=%s session=%s title=%r",
            getattr(session, "app_id", ""),
            getattr(session, "session_id", ""),
            title,
        )
    except Exception as exc:
        logger.warning("title_gen_outer_failed: %s", exc, exc_info=True)

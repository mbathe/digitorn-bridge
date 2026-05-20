"""Async wrappers around `litellm.token_counter`."""

from __future__ import annotations

import asyncio
from typing import Any


async def count_message_tokens(
    *,
    model: str,
    messages: list[dict[str, Any]],
) -> int:
    """Real token count for an OpenAI-style messages array."""
    try:
        import litellm
    except ImportError as exc:
        raise ImportError(
            "litellm required for accurate token counting "
            "(no len/4 fallback by design)"
        ) from exc
    return int(await asyncio.to_thread(
        litellm.token_counter, model=model, messages=messages,
    ))


async def count_text_tokens(
    *,
    model: str,
    text: str,
) -> int:
    """Real token count for raw text. Same threadpool dispatch as"""
    try:
        import litellm
    except ImportError as exc:
        raise ImportError(
            "litellm required for accurate token counting "
            "(no len/4 fallback by design)"
        ) from exc
    return int(await asyncio.to_thread(
        litellm.token_counter, model=model, text=text,
    ))

"""Async wrappers around ``litellm.token_counter``.

Critical guarantee: the agent loop is NEVER blocked by token-counting
work. ``asyncio.to_thread`` dispatches the sync tokenizer call to a
worker thread; the event loop continues serving other tasks while
the worker runs.

Use these helpers from any async path -- including the hot path of
an agent turn -- without worrying about loop starvation. Typical
latency on a warm tokenizer cache: 1-10ms for a full conversation.
First call may be slow (10s+) for HuggingFace model loads; subsequent
calls hit the cache.

NO heuristic fallback (``len(text) // 4``). If litellm is unavailable
the helper raises ImportError -- the caller decides how to handle the
absence of accurate token counts. The user explicitly required real
values: never silently lie about token usage.
"""

from __future__ import annotations

import asyncio
from typing import Any


async def count_message_tokens(
    *,
    model: str,
    messages: list[dict[str, Any]],
) -> int:
    """Real token count for an OpenAI-style messages array.

    Runs the tokenizer on a worker thread; the event loop is free
    while the work happens. Caller awaits the result.

    Raises ImportError if litellm is not installed.
    """
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
    """Real token count for raw text. Same threadpool dispatch as
    ``count_message_tokens``."""
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

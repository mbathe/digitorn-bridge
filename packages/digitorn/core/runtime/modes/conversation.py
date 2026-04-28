"""Conversation mode - interactive readline loop.

The user sends messages, the agent responds, history persists.

Background task notifications are delivered automatically:
when a background task completes while waiting for user input,
a new agent turn is triggered without user intervention.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from digitorn.core.runtime.agent_loop import agent_turn
from digitorn.core.runtime.types import AgentContext

logger = logging.getLogger(__name__)

_BG_POLL_INTERVAL = 0.5


async def run_conversation(
    ctx: AgentContext,
    *,
    greeting: str = "",
    max_turns: int = 30,
    timeout: float = 120.0,
    on_tool_call: Any | None = None,
    on_tool_start: Any | None = None,
    on_thinking: Any | None = None,
    on_token: Any | None = None,
    on_stream_done: Any | None = None,
    on_out_token: Any | None = None,
    input_fn: Any | None = None,
    output_fn: Any | None = None,
    hook_runner: Any | None = None,
    on_turn_result: Any | None = None,
) -> None:
    """Run the agent in conversation mode."""
    _input = input_fn or _default_input
    _output = output_fn or _default_output

    turn_kwargs: dict[str, Any] = dict(
        max_turns=max_turns,
        timeout=timeout,
        on_tool_call=on_tool_call,
        on_tool_start=on_tool_start,
        on_thinking=on_thinking,
        hook_runner=hook_runner,
        on_token=on_token,
        on_stream_done=on_stream_done,
        on_out_token=on_out_token,
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": ctx.system_prompt},
    ]

    if greeting:
        _output(f"\n{greeting}\n")

    cb = ctx.context_builder

    pending_input: asyncio.Task[str | None] | None = None

    while True:
        user_input = await _get_next_input(
            ctx, messages, cb, _input, _output, pending_input, **turn_kwargs,
        )
        pending_input = None

        if user_input is None:
            _output("\nGoodbye.")
            break

        if user_input == "":
            continue

        stripped = user_input.strip()
        if stripped.lower() in ("/quit", "/exit", "quit", "exit"):
            _output("Goodbye.")
            break

        if not stripped:
            continue

        messages.append({"role": "user", "content": user_input})

        result = await agent_turn(ctx, messages, **turn_kwargs)

        if result.error and not result.content:
            _output(f"\n  [Error: {result.error}]\n")
        elif result.content:
            _output(f"\n  {result.content}\n")

        if on_turn_result is not None:
            try:
                await on_turn_result(result)
            except Exception:
                pass  # Callback failure must not break conversation loop

        if result.content:
            messages.append({"role": "assistant", "content": result.content})


async def _get_next_input(
    ctx: AgentContext,
    messages: list[dict[str, Any]],
    cb: Any,
    input_fn: Any,
    output_fn: Any,
    existing_input_task: asyncio.Task[str | None] | None,
    **turn_kwargs: Any,
) -> str | None:
    """Wait for user input while delivering background notifications proactively.

    Key design: we NEVER cancel the input task. Blocking input() in a thread
    cannot be interrupted by asyncio cancellation. Instead, we keep the same
    input task alive and reuse it across notification handling cycles.

    Returns:
        - User input string (normal case)
        - "" if a notification-triggered turn was handled (caller loops back)
        - None if the user wants to exit (EOF/Ctrl-C)
    """
    if cb is None or not hasattr(cb, "drain_bg_notifications"):
        logger.debug("_get_next_input: no context_builder, simple input")
        try:
            return await input_fn("you > ")
        except (EOFError, KeyboardInterrupt):
            return None

    if not cb.has_active_bg_tasks():
        try:
            return await input_fn("you > ")
        except (EOFError, KeyboardInterrupt):
            return None

    logger.info("Background tasks active - entering notification polling mode")
    print("\n  [Background tasks active - watching for completions...]", flush=True)

    input_task = existing_input_task or asyncio.create_task(
        _safe_input(input_fn, "you > ")
    )

    try:
        while True:
            done, _ = await asyncio.wait(
                {input_task}, timeout=_BG_POLL_INTERVAL,
            )

            if done:
                return input_task.result()

            notifications = cb.drain_bg_notifications()
            if notifications:
                await _handle_bg_notifications(
                    ctx, messages, notifications, output_fn, **turn_kwargs,
                )

                if input_task.done():
                    return input_task.result()

                if not cb.has_active_bg_tasks():
                    try:
                        return await input_task
                    except (EOFError, KeyboardInterrupt):
                        return None

                continue

    except asyncio.CancelledError:
        raise


async def _handle_bg_notifications(
    ctx: AgentContext,
    messages: list[dict[str, Any]],
    notifications: list[dict[str, Any]],
    output_fn: Any,
    **turn_kwargs: Any,
) -> None:
    """Inject background/watcher notifications and trigger a new agent turn."""
    from digitorn.core.runtime.agent_loop import (
        _format_bg_task_notification,
        _format_watcher_notification,
    )

    for notif in notifications:
        if notif.get("type") == "watcher":
            text = _format_watcher_notification(notif)
        else:
            text = _format_bg_task_notification(notif)
        messages.append({"role": "system", "content": text})

    logger.info(
        "Background notification wake-up: %d task(s) completed, "
        "triggering agent turn",
        len(notifications),
    )

    result = await agent_turn(ctx, messages, **turn_kwargs)

    if result.error and not result.content:
        output_fn(f"\n  [Error: {result.error}]\n")
    elif result.content:
        output_fn(f"\n  {result.content}\n")

    if result.content:
        messages.append({"role": "assistant", "content": result.content})


async def _safe_input(input_fn: Any, prompt: str) -> str | None:
    """Wrap input_fn to return None on EOF/KeyboardInterrupt."""
    try:
        return await input_fn(prompt)
    except (EOFError, KeyboardInterrupt):
        return None


async def _default_input(prompt: str) -> str:
    """Default input function using builtin input()."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: input(prompt))


def _default_output(text: str) -> None:
    """Default output function."""
    print(text, flush=True)

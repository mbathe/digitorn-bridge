"""Agent loop - the core execution cycle."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from digitorn.core.runtime.callbacks import AgentTurnCallbacks
from digitorn.core.runtime.compaction import (
    aestimate_tokens,
    emergency_compact,
    estimate_tokens,
    is_context_overflow,
)
from digitorn.core.runtime.extract_tool import _extract_inline_tool_calls
from digitorn.core.runtime.loop_guards import LoopState, check_delegation, check_tool_health
from digitorn.core.runtime.messages import (
    build_assistant_message,
    extract_content,
    extract_tool_calls,
    max_tool_result_chars,
    parse_tool_args,
    serialize_result,
    synthesize_reasoning,
    to_chat_messages,
    truncate_tool_result,
)
from digitorn.core.runtime.notifications import (
    inject_bg_notifications,
    format_bg_task_notification,
    format_watcher_notification,
    _persist_to_memory,
)
from digitorn.core.runtime.streaming import _fire_token, emit_thinking, streaming_chat
from digitorn.core.runtime.system_directive import inject_system_directive
from digitorn.core.runtime.tool_exec import execute_tool, handle_approval, needs_approval
from digitorn.core.runtime.tracking import SessionUsage, format_image_tool_result
from digitorn.core.runtime.types import AgentContext, ToolCallInfo, TurnResult
import digitorn.core.runtime.tool_hooks as _th

logger = logging.getLogger(__name__)

_extract_content = extract_content  # noqa: F811
_extract_tool_calls = extract_tool_calls  # noqa: F811
_serialize_result = serialize_result  # noqa: F811
_is_context_overflow = is_context_overflow  # noqa: F811
_emergency_compact = emergency_compact  # noqa: F811
_format_bg_task_notification = format_bg_task_notification  # noqa: F811
_format_watcher_notification = format_watcher_notification  # noqa: F811
_persist_notification_to_memory = _persist_to_memory  # noqa: F811


_READ_ONLY_ACTIONS = frozenset({
    "read", "grep", "glob", "ls", "find",
    "filesystem__read", "filesystem__grep", "filesystem__glob", "filesystem__ls",
    "filesystem.read", "filesystem.grep", "filesystem.glob", "filesystem.ls",
    "search", "fetch", "extract",
    "web__search", "web__fetch", "web__extract",
    "web.search", "web.fetch", "web.extract",
    "agent", "agent_spawn__agent", "agent_spawn.agent",
    "spawn_agent", "agent_spawn__spawn_agent", "agent_spawn.spawn_agent",
    "agent_status", "agent_spawn__agent_status", "agent_spawn.agent_status",
    "agent_list", "agent_spawn__agent_list", "agent_spawn.agent_list",
    "remember", "memory__remember", "memory.remember",
    "task_create", "memory__task_create", "memory.task_create",
    "task_update", "memory__task_update", "memory.task_update",
    "set_goal", "memory__set_goal", "memory.set_goal",
    "diagnostics", "lsp__diagnostics", "lsp.diagnostics",
    "check", "lsp__check", "lsp.check",
    "schema", "database__schema", "database.schema",
})


def _all_read_only(tool_calls: list[dict]) -> bool:
    """Check if all tool_calls in a batch are read-only (safe for parallel execution)."""
    for call in tool_calls:
        name = call.get("function", {}).get("name", "")
        if name not in _READ_ONLY_ACTIONS:
            return False
    return True



class _ProviderCircuitBreaker:
    """Simple circuit breaker for LLM provider calls."""

    def __init__(
        self, failure_threshold: int = 5, recovery_timeout: float = 30.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._consecutive_failures = 0
        self._opened_at: float = 0.0
        self._state: str = "closed"

    @property
    def state(self) -> str:
        if self._state == "open":
            if time.monotonic() - self._opened_at >= self._recovery_timeout:
                self._state = "half-open"
        return self._state

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._state = "closed"

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._state = "open"
            self._opened_at = time.monotonic()
            logger.warning(
                "llm_circuit_open failures=%d timeout=%.0fs",
                self._consecutive_failures, self._recovery_timeout,
            )

    def check(self) -> None:
        state = self.state
        if state == "open":
            remaining = self._recovery_timeout - (time.monotonic() - self._opened_at)
            raise RuntimeError(
                f"LLM provider circuit breaker is open after "
                f"{self._consecutive_failures} consecutive failures. "
                f"Retry in {remaining:.0f}s."
            )


_circuit_breakers: dict[str, _ProviderCircuitBreaker] = {}
_CB_MAX_SIZE = 64


def _get_circuit_breaker(provider: Any) -> _ProviderCircuitBreaker:
    key = str(getattr(provider, "provider_id", id(provider)))
    if key not in _circuit_breakers:
        if len(_circuit_breakers) >= _CB_MAX_SIZE:
            del _circuit_breakers[next(iter(_circuit_breakers))]
        _circuit_breakers[key] = _ProviderCircuitBreaker()
    return _circuit_breakers[key]


def clear_circuit_breakers(*provider_ids: str) -> None:
    if not provider_ids:
        _circuit_breakers.clear()
        return
    for pid in provider_ids:
        _circuit_breakers.pop(pid, None)


async def _emit_retry_status(
    ctx: Any,
    *,
    attempt: int,
    max_retries: int,
    delay_s: int,
    reason: str,
) -> None:
    bus = getattr(ctx, "event_bus", None) or getattr(ctx, "_event_bus", None)
    if bus is None:
        return
    sid = getattr(ctx, "session_id", "") or ""
    app_id = getattr(ctx, "app_id", "") or "default"
    user_id = getattr(ctx, "user_id", "") or "local"
    if not sid:
        return
    try:
        await bus.publish(f"{app_id}:{user_id}:{sid}", {
            "type": "status",
            "phase": "rate_limited",
            "attempt": attempt,
            "max": max_retries,
            "delay_seconds": delay_s,
            "reason": reason,
            "session_id": sid,
            "app_id": app_id,
        })
    except Exception:  # noqa: BLE001
        logger.debug("retry status emit failed", exc_info=True)


async def _clear_retry_status(ctx: Any) -> None:
    bus = getattr(ctx, "event_bus", None) or getattr(ctx, "_event_bus", None)
    if bus is None:
        return
    sid = getattr(ctx, "session_id", "") or ""
    app_id = getattr(ctx, "app_id", "") or "default"
    user_id = getattr(ctx, "user_id", "") or "local"
    if not sid:
        return
    try:
        await bus.publish(f"{app_id}:{user_id}:{sid}", {
            "type": "status",
            "phase": "",
            "session_id": sid,
            "app_id": app_id,
        })
    except Exception:  # noqa: BLE001
        logger.debug("retry status clear emit failed", exc_info=True)


def _is_connection_error(exc: Exception) -> bool:
    cls_name = type(exc).__name__
    _network_types = (
        "ReadError", "WriteError", "PoolTimeout", "RemoteProtocolError",
        "StreamError", "StreamClosed", "NetworkError", "ProtocolError",
        "ChunkedEncodingError", "IncompleteRead",
    )
    if (
        "Connect" in cls_name
        or "Timeout" in cls_name
        or cls_name in _network_types
    ):
        return True
    msg = str(exc).lower()
    return (
        "connection" in msg
        or "timed out" in msg
        or "peer closed" in msg
        or "stream" in msg and ("closed" in msg or "reset" in msg)
        or "chunked" in msg
    )


_TRANSIENT_OPEN = "<<<DIGITORN_TRANSIENT_BLOCK>>>"
_TRANSIENT_CLOSE = "<<<END_DIGITORN_TRANSIENT_BLOCK>>>"


def _strip_transient_blocks_from_text(text: str) -> str:
    if _TRANSIENT_OPEN not in text:
        return text
    out_parts: list[str] = []
    pos = 0
    while True:
        open_i = text.find(_TRANSIENT_OPEN, pos)
        if open_i < 0:
            out_parts.append(text[pos:])
            break
        out_parts.append(text[pos:open_i])
        close_i = text.find(_TRANSIENT_CLOSE, open_i)
        if close_i < 0:
            break
        pos = close_i + len(_TRANSIENT_CLOSE)
        if pos < len(text) and text[pos] == "\n":
            pos += 1
    return "".join(out_parts)


def _strip_transient_from_past_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break
    out: list[dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "user" or i == last_user_idx:
            out.append(msg)
            continue
        content = msg.get("content", "")
        if isinstance(content, str) and _TRANSIENT_OPEN in content:
            new_content = _strip_transient_blocks_from_text(content)
            patched = dict(msg)
            patched["content"] = new_content
            out.append(patched)
        elif isinstance(content, list):
            new_blocks: list[Any] = []
            mutated = False
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                    and _TRANSIENT_OPEN in block["text"]
                ):
                    new_blocks.append({
                        **block,
                        "text": _strip_transient_blocks_from_text(block["text"]),
                    })
                    mutated = True
                else:
                    new_blocks.append(block)
            if mutated:
                patched = dict(msg)
                patched["content"] = new_blocks
                out.append(patched)
            else:
                out.append(msg)
        else:
            out.append(msg)
    return out


def _chat_messages_for_llm(
    ctx: Any, messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    pruned = _strip_transient_from_past_messages(messages)
    return to_chat_messages(pruned)


async def agent_turn(
    ctx: AgentContext,
    messages: list[dict[str, Any]],
    *,
    max_turns: int = 120,
    timeout: float = 1200.0,
    callbacks: AgentTurnCallbacks | None = None,
    # Legacy kwargs - forwarded to AgentTurnCallbacks if callbacks is None
    on_tool_call: Any | None = None,
    on_tool_start: Any | None = None,
    on_tool_call_streaming: Any | None = None,
    on_thinking: Any | None = None,
    on_thinking_started: Any | None = None,
    on_thinking_delta: Any | None = None,
    hook_runner: Any | None = None,
    on_token: Any | None = None,
    on_stream_done: Any | None = None,
    on_out_token: Any | None = None,
    on_in_token: Any | None = None,
    **kwargs: Any,
) -> TurnResult:
    if callbacks is None:
        callbacks = AgentTurnCallbacks(
            on_token=on_token,
            on_stream_done=on_stream_done,
            on_out_token=on_out_token,
            on_in_token=on_in_token,
            on_tool_start=on_tool_start,
            on_tool_call=on_tool_call,
            on_tool_call_streaming=on_tool_call_streaming,
            on_thinking=on_thinking,
            on_thinking_started=on_thinking_started,
            on_thinking_delta=on_thinking_delta,
            hook_runner=hook_runner,
        )

    from digitorn.core.runtime import run_tracker as _runs
    from digitorn.core.runtime.request_context import (
        RequestContext,
        set_request_context,
        reset_request_context,
        get_inbound_user_jwt,
    )

    parent_run_id = getattr(ctx, "current_run_id", None)
    run_id = _runs.start_run(ctx, max_turns, parent_run_id=parent_run_id)
    ctx.current_run_id = run_id
    _runs.emit_event(run_id, "lifecycle", {"event": "run_started", "max_turns": max_turns})

    _user_jwt = (
        getattr(ctx, "user_jwt", None)
        or get_inbound_user_jwt()
        or None
    )

    _req_ctx_token = set_request_context(RequestContext(
        user_id=getattr(ctx, "user_id", None),
        app_id=getattr(ctx, "app_id", None),
        session_id=getattr(ctx, "session_id", None),
        run_id=run_id,
        agent_id=getattr(ctx, "agent_id", None),
        user_jwt=_user_jwt,
    ))

    final_status = "completed"
    final_reason: str | None = None
    final_result: TurnResult | None = None

    try:
        final_result = await asyncio.wait_for(
            _loop(ctx, messages, max_turns, callbacks),
            timeout=timeout,
        )
        if final_result.error:
            final_status = "failed"
            final_reason = final_result.error
        return final_result
    except asyncio.TimeoutError:
        final_status = "timeout"
        final_reason = "Timeout reached"
        final_result = TurnResult(
            content="[Timeout reached]", truncated=True, error="Timeout reached",
        )
        return final_result
    except asyncio.CancelledError:
        final_status = "cancelled"
        final_reason = "cancelled"
        raise
    except (MemoryError, SystemExit, KeyboardInterrupt):
        final_status = "failed"
        final_reason = "fatal"
        raise
    except Exception as exc:
        final_status = "failed"
        final_reason = f"{type(exc).__name__}: {exc}"
        logger.error("agent_turn_error type=%s error=%s", type(exc).__name__, exc, exc_info=True)
        final_result = TurnResult(content="", error=str(exc))
        return final_result
    finally:
        _runs.emit_event(
            run_id, "lifecycle",
            {"event": f"run_{final_status}", "reason": final_reason},
        )
        _runs.complete_run(
            run_id,
            status=final_status,
            turn_result=final_result,
            status_reason=final_reason,
        )
        ctx.current_run_id = parent_run_id
        reset_request_context(_req_ctx_token)


def _relay_event(ctx: AgentContext, event: dict[str, Any]) -> None:
    relay = getattr(ctx, "progress_relay", None)
    if relay is not None:
        try:
            relay(event)
        except Exception as exc:
            logger.debug("progress_relay %s failed: %s", event.get("type", "?"), exc)


async def _loop(
    ctx: AgentContext,
    messages: list[dict[str, Any]],
    max_turns: int,
    cb: AgentTurnCallbacks,
) -> TurnResult:
    collected_calls: list[ToolCallInfo] = []
    usage = SessionUsage()
    guard = LoopState.from_runtime_config(ctx.runtime_config)

    sm = _get_session_metrics(ctx)
    sm.model = getattr(ctx.provider, "model", "")
    sm.provider = getattr(ctx.provider, "provider_hint", "") or getattr(ctx.provider, "provider_id", "")
    sm.max_turns = max_turns
    sm.user_id = getattr(ctx, "user_id", "") or ""

    _tc = getattr(ctx, "_chat_tool_calls", None)
    if _tc is not None and bool(getattr(_tc, "strict_mode", False)):
        try:
            _last_user_msg = next(
                (m.get("content", "") for m in reversed(messages)
                 if m.get("role") == "user" and isinstance(m.get("content"), str)),
                "",
            )
            if _last_user_msg:
                from digitorn.core.runtime.intent_phrases import generate_and_emit_phrases
                _corr = getattr(ctx, "current_run_id", None) or getattr(ctx, "session_id", "") or ""
                asyncio.create_task(generate_and_emit_phrases(
                    ctx, _last_user_msg, str(_corr),
                ))
        except Exception:  # noqa: BLE001
            logger.debug("strict_mode intent_phrases dispatch failed", exc_info=True)

    _prev_turn_had_streamed_text = False

    for turn in range(max_turns):
        _cancel_evt = getattr(ctx, "cancel_event", None)
        if _cancel_evt is not None and _cancel_evt.is_set():
            _reason = getattr(ctx, "cancel_reason", "") or "cooperative cancel"
            return TurnResult(
                content="",
                turns_used=turn,
                tool_calls=[],
                status="cancelled",
                error=f"cancelled: {_reason}",
            )
        if guard.kill_turn_reason:
            logger.error(
                "agent_loop_hard_killed turn=%d reason=%s",
                turn, guard.kill_turn_reason,
            )
            return TurnResult(
                content="",
                turns_used=turn,
                tool_calls=collected_calls,
                status="loop_killed",
                error=guard.kill_turn_reason,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
            )
        guard.counter["turns"] = turn + 1
        sm.record_turn(turn)

        import os as _os_perf
        from pathlib import Path as _Path_perf
        _perf_on = (turn == 0) and (_os_perf.environ.get("DIGITORN_PERF", "1") != "0")
        _perf_sid = (getattr(ctx, "session_id", "") or "?")[:8]
        _perf_app = (getattr(ctx, "app_id", "") or "?")
        _perf_t0 = time.monotonic() if _perf_on else 0.0
        _perf_prev = _perf_t0
        _perf_path = _Path_perf.home() / ".digitorn" / "logs" / "perf.log"
        _perf_session_turn = (
            int(getattr(getattr(ctx, "session", None), "turn_count", 0) or 0)
        )
        def _perf(_label: str, _t_prev: float = 0.0) -> float:
            if not _perf_on:
                return 0.0
            _now = time.monotonic()
            _line = (
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"PERF app={_perf_app} sid={_perf_sid} "
                f"msg={_perf_session_turn} iter={turn} "
                f"phase={_label} dt={_now - _t_prev:.3f}s total={_now - _perf_t0:.3f}s\n"
            )
            try:
                with open(_perf_path, "a", encoding="utf-8") as _f:
                    _f.write(_line)
            except Exception as exc:
                logger.debug("agent_loop best-effort block failed: %s", exc)
            logger.warning(_line.strip())
            return _now

        await _inject_turn_limit_warning(ctx, messages, turn, max_turns, guard.counter["tools"])
        if turn == 0:
            await _run_hooks(
                cb.hook_runner, "session_start",
                messages, turn, max_turns, guard.counter["tools"], ctx,
            )
            _perf_prev = _perf("hooks.session_start", _perf_prev)
        await _run_hooks(cb.hook_runner, "turn_start", messages, turn, max_turns, guard.counter["tools"], ctx)
        _perf_prev = _perf("hooks.turn_start", _perf_prev)
        inject_bg_notifications(ctx, messages)
        await _call_memory_turn_start(ctx, messages, turn)
        _perf_prev = _perf("memory.turn_start", _perf_prev)

        _beh = getattr(ctx, "behavior_module", None)
        if _beh is not None and hasattr(_beh, "on_turn_start"):
            _beh.on_turn_start(getattr(ctx, "session_id", "") or "")

        _last_msg = messages[-1] if messages else {}
        _is_fresh_user_turn = _last_msg.get("role") == "user"

        _effective = getattr(ctx, "effective_turn", None)
        if _effective is not None and _is_fresh_user_turn:
            from digitorn.core.runtime.mode_merge import (
                build_mode_switch_message,
                compute_tool_partition,
            )
            _all_short: list[str] = []
            if ctx.tools:
                for _t in ctx.tools:
                    _fn_name = _t.get("function", {}).get("name", "")
                    if _fn_name:
                        _all_short.append(_fn_name)
            _allowed_names, _blocked_names = compute_tool_partition(
                _effective.tool_grants, _all_short,
            )
            ctx.allowed_tool_names = (
                _allowed_names if _effective.tool_grants is not None else None
            )
            ctx.active_mode_label = (
                _effective.mode_label or _effective.active_mode_id or ""
            )

            _prev_mode: str | None = None
            _state_ref = None
            try:
                from digitorn.core.runtime.session_store import get_default_bridge
                _sid = getattr(ctx, "session_id", "") or ""
                if _sid:
                    _bridge = get_default_bridge()
                    _state_ref = _bridge.store.state(_sid)
                    if _state_ref is not None:
                        _prev_mode = getattr(_state_ref, "active_mode_id", None)
            except Exception as _exc:
                logger.debug("mode_state_read_failed: %s", _exc)

            if (_effective.active_mode_id or None) != (_prev_mode or None):
                _msg = build_mode_switch_message(
                    _effective, _allowed_names, _blocked_names,
                )
                if _msg:
                    logger.info(
                        "mode_switch sid=%s prev=%s new=%s allowed=%d blocked=%d",
                        getattr(ctx, "session_id", ""),
                        _prev_mode, _effective.active_mode_id,
                        len(_allowed_names), len(_blocked_names),
                    )
                    await inject_system_directive(
                        ctx,
                        content=_msg,
                        source="mode_switch",
                        messages=messages,
                        turn=turn,
                        metadata={
                            "mode_id": _effective.active_mode_id or "",
                            "mode_label": _effective.mode_label or "",
                            "allowed": sorted(_allowed_names),
                            "blocked": sorted(_blocked_names),
                        },
                    )
                if _state_ref is not None:
                    _state_ref.active_mode_id = _effective.active_mode_id

            if _beh is not None and hasattr(_beh, "set_active_profile"):
                try:
                    _beh.set_active_profile(_effective.behavior_profile or "")
                except Exception as _exc:
                    logger.debug("behavior_profile_override_failed: %s", _exc)
        if (
            _beh is not None
            and hasattr(_beh, "classify_turn")
            and getattr(_beh, "classify_enabled", False)
            and _is_fresh_user_turn
        ):
            _content = _last_msg.get("content", "")
            _user_msg = str(_content) if not isinstance(_content, list) else str(_content)
            if _user_msg:
                _tool_inv: list[dict[str, str]] = []
                _caps: list[str] = []
                if ctx.tools:
                    _seen_mods: set[str] = set()
                    for _t in ctx.tools:
                        _fn_info = _t.get("function", {})
                        _fn_name = _fn_info.get("name", "")
                        _fn_desc = _fn_info.get("description", "")[:80]
                        if _fn_name:
                            _tool_inv.append({"name": _fn_name, "description": _fn_desc})
                            _mod = _fn_name.split(".")[0] if "." in _fn_name else _fn_name
                            if _mod not in _seen_mods:
                                _seen_mods.add(_mod)
                                _caps.append(_mod)

                _ws_ctx: dict[str, Any] = {}
                _ws = getattr(ctx, "workspace", None) or getattr(ctx, "workspace_path", None)
                if _ws:
                    _ws_ctx["workspace_path"] = str(_ws)

                _directive = await _beh.classify_turn(
                    session_id=getattr(ctx, "session_id", "") or "",
                    user_message=_user_msg,
                    capabilities=_caps or ["filesystem", "shell", "memory"],
                    turn=turn,
                    recent_messages=messages[-8:] if len(messages) > 1 else None,
                    tool_inventory=_tool_inv or None,
                    workspace_context=_ws_ctx or None,
                    provider_override=getattr(
                        ctx, "_session_classifier_provider", None,
                    ),
                )
                _perf_prev = _perf("behavior.classify_turn", _perf_prev)
                if _directive:
                    logger.info("behavior_directive_injected turn=%d len=%d", turn, len(_directive))
                    await inject_system_directive(
                        ctx,
                        content=_directive,
                        source="behavior_classifier",
                        messages=messages,
                        turn=turn,
                        metadata={"length": len(_directive)},
                    )
                    _bus = getattr(ctx, "event_bus", None) or getattr(ctx, "_event_bus", None)
                    if _bus is not None:
                        try:
                            _sid = getattr(ctx, "session_id", "") or ""
                            _aid = getattr(ctx, "app_id", "") or "default"
                            _uid = getattr(ctx, "user_id", "") or "local"
                            await _bus.publish(f"{_aid}:{_uid}:{_sid}", {
                                "type": "behavior_directive",
                                "data": {
                                    "turn": turn,
                                    "directive": _directive,
                                    "length": len(_directive),
                                },
                            })
                        except Exception as _exc:
                            logger.debug("behavior_directive SSE emit failed: %s", _exc)

        if _prev_turn_had_streamed_text and cb.on_token is not None:
            await _fire_token(cb, "\n\n")

        _llm_t0 = time.monotonic()
        content, tool_calls, response, streamed = await _call_llm(ctx, messages, cb, turn)
        _llm_ms = (time.monotonic() - _llm_t0) * 1000
        _perf_prev = _perf("llm.call_done", _perf_prev)

        _prev_turn_had_streamed_text = bool(streamed and content and content.strip())

        _resp_usage = getattr(response, "usage", None) if response else None
        _pt = getattr(_resp_usage, "prompt_tokens", 0) or 0
        _ct = getattr(_resp_usage, "completion_tokens", 0) or 0
        sm.record_llm_call(_llm_ms, _pt, _ct)
        usage.prompt_tokens += _pt
        usage.completion_tokens += _ct

        if _pt or _ct:
            _relay_event(ctx, {
                "type": "token_usage",
                "turn": turn + 1,
                "input_tokens": _pt,
                "output_tokens": _ct,
                "agent_id": ctx.agent_id,
            })

        if content is None:
            content = response  # type: ignore[assignment]
            tool_calls = []
            streamed = False

        content = await _run_after_middleware(ctx, messages, turn, content, tool_calls)

        if not tool_calls:
            if await _check_unfinished_work(ctx, messages):
                continue
            if await _nudge_empty_response(ctx, messages, content, guard.counter["tools"]):
                continue
            _reasoning_final = (
                getattr(response, "reasoning_content", None)
                if response is not None else None
            )
            messages.append(
                build_assistant_message(content or "", [], reasoning_content=_reasoning_final)
            )
            _persist_turn_bg(ctx, messages, turn, usage, guard.counter, status="completed")
            return _build_final_result(content, guard.counter, collected_calls, usage)

        _beh = getattr(ctx, "behavior_module", None)
        if _beh is not None and hasattr(_beh, "check_agent_text") and content:
            _sid = getattr(ctx, "session_id", "") or ""
            _text_violations = _beh.check_agent_text(_sid, content or "")
            ctx._last_agent_text = content or ""  # type: ignore[attr-defined]
        else:
            ctx._last_agent_text = content or ""  # type: ignore[attr-defined]
            _text_violations = []

        await _emit_thinking_for_turn(cb, content, tool_calls, response, streamed)
        _reasoning = getattr(response, "reasoning_content", None) if response else None
        messages.append(build_assistant_message(content, tool_calls, reasoning_content=_reasoning))
        _persist_turn_bg(ctx, messages, turn, usage, guard.counter)

        deferred_notes: list[str] = list(_text_violations)

        if len(tool_calls) > 1 and _all_read_only(tool_calls):
            _para_t0 = time.monotonic()

            async def _run_one(call: dict) -> tuple[Any, str, dict, str, bool, str, float]:
                t0 = time.monotonic()
                try:
                    r = await _execute_single_tool(ctx, call, guard, cb, messages, turn, max_turns)
                    return (*r, (time.monotonic() - t0) * 1000)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    t_name = call.get("function", {}).get("name", "?")
                    t_args = call.get("function", {}).get("arguments", {})
                    c_id = call.get("id", f"call_{uuid.uuid4().hex[:12]}")
                    err_result = {
                        "success": False,
                        "error": f"Parallel execution exception: {type(exc).__name__}: {exc}",
                    }
                    return (
                        err_result, t_name,
                        t_args if isinstance(t_args, dict) else {},
                        c_id, False, str(exc),
                        (time.monotonic() - t0) * 1000,
                    )

            para_results = await asyncio.gather(
                *[_run_one(c) for c in tool_calls],
                return_exceptions=True,
            )

            for call, res in zip(tool_calls, para_results):
                if isinstance(res, BaseException):
                    t_name = call.get("function", {}).get("name", "?")
                    t_args = call.get("function", {}).get("arguments", {})
                    c_id = call.get("id", f"call_{uuid.uuid4().hex[:12]}")
                    err_result = {"success": False, "error": f"Parallel execution error: {res}"}
                    collected_calls.append(ToolCallInfo(
                        name=t_name,
                        params=t_args if isinstance(t_args, dict) else {},
                        success=False, error=str(res),
                    ))
                    _append_tool_result(
                        ctx, messages, c_id, t_name, err_result, False, cb,
                        tool_args=t_args if isinstance(t_args, dict) else {},
                    )
                    continue
                if not isinstance(res, tuple) or len(res) != 7:
                    logger.error(
                        "parallel_tool_malformed_result type=%s len=%s",
                        type(res).__name__, len(res) if hasattr(res, "__len__") else "?",
                    )
                    t_name = call.get("function", {}).get("name", "?")
                    c_id = call.get("id", f"call_{uuid.uuid4().hex[:12]}")
                    _t_args_raw = call.get("function", {}).get("arguments", {})
                    err_result = {"success": False, "error": "Malformed parallel execution result"}
                    _append_tool_result(
                        ctx, messages, c_id, t_name, err_result, False, cb,
                        tool_args=_t_args_raw if isinstance(_t_args_raw, dict) else {},
                    )
                    continue
                result, tool_name, tool_args, call_id, ok, err, _tool_ms = res
                collected_calls.append(ToolCallInfo(name=tool_name, params=tool_args, success=ok, error=err))
                _append_tool_result(
                    ctx, messages, call_id, tool_name, result, ok, cb,
                    tool_args=tool_args if isinstance(tool_args, dict) else {},
                )
                await _flush_behavior_notes(ctx, messages)
                sm.record_tool_call(tool_name, _tool_ms, ok, err or "")
                _relay_event(ctx, {
                    "type": "tool_call",
                    "turn": turn + 1,
                    "tool": tool_name,
                    "success": ok,
                    "duration_ms": round(_tool_ms, 1),
                    "agent_id": ctx.agent_id,
                    "parallel": True,
                    "op_id": call_id,
                    "op_type": "tool",
                    "op_state": "completed" if ok else "failed",
                })
                serialized_len = len(serialize_result(result)) if not isinstance(result, str) else len(result)
                deferred_notes.extend(check_tool_health(guard, tool_name, tool_args, ok, serialized_len))

            terminal = _intra_turn_terminal_reason(ctx, guard)
            if terminal is not None:
                status, err = terminal
                logger.error(
                    "agent_loop_intra_turn_abort_parallel turn=%d status=%s",
                    turn, status,
                )
                return TurnResult(
                    content="",
                    turns_used=turn + 1,
                    tool_calls=collected_calls,
                    status=status,
                    error=err,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                )

            _para_ms = (time.monotonic() - _para_t0) * 1000
            logger.info("parallel_tools count=%d duration_ms=%.0f", len(tool_calls), _para_ms)
            _persist_turn_bg(ctx, messages, turn, usage, guard.counter)

        else:
            for call in tool_calls:
                _tool_t0 = time.monotonic()
                result, tool_name, tool_args, call_id, ok, err = await _execute_single_tool(
                    ctx, call, guard, cb, messages, turn, max_turns,
                )
                _tool_ms = (time.monotonic() - _tool_t0) * 1000
                collected_calls.append(ToolCallInfo(name=tool_name, params=tool_args, success=ok, error=err))
                _append_tool_result(
                    ctx, messages, call_id, tool_name, result, ok, cb,
                    tool_args=tool_args if isinstance(tool_args, dict) else {},
                )
                await _flush_behavior_notes(ctx, messages)

                sm.record_tool_call(tool_name, _tool_ms, ok, err or "")

                _relay_event(ctx, {
                    "type": "tool_call",
                    "turn": turn + 1,
                    "tool": tool_name,
                    "success": ok,
                    "duration_ms": round(_tool_ms, 1),
                    "agent_id": ctx.agent_id,
                    "op_id": call_id,
                    "op_type": "tool",
                    "op_state": "completed" if ok else "failed",
                })

                _persist_turn_bg(ctx, messages, turn, usage, guard.counter)

                if ok:
                    from digitorn.core.runtime.tool_names import to_fqn as _to_fqn_diag
                    if _to_fqn_diag(tool_name) in ("filesystem.write", "filesystem.edit"):
                        diag_note = await _get_diagnostics_note(ctx, tool_args)
                        if diag_note:
                            deferred_notes.append(diag_note)

                serialized_len = len(serialize_result(result)) if not isinstance(result, str) else len(result)
                deferred_notes.extend(check_tool_health(guard, tool_name, tool_args, ok, serialized_len))

                terminal = _intra_turn_terminal_reason(ctx, guard)
                if terminal is not None:
                    status, err = terminal
                    logger.error(
                        "agent_loop_intra_turn_abort turn=%d tool_idx=%d status=%s",
                        turn, len(collected_calls), status,
                    )
                    return TurnResult(
                        content="",
                        turns_used=turn + 1,
                        tool_calls=collected_calls,
                        status=status,
                        error=err,
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                    )

        deferred_notes.extend(check_delegation(tool_calls, guard.counter["tools"], ctx.tools))
        for note in deferred_notes:
            await inject_system_directive(
                ctx,
                content=note,
                source="delegation_check",
                messages=messages,
                turn=turn,
            )

        _call_memory_turn_end(ctx, messages, turn, collected_calls, tool_calls)
        await _run_hooks(cb.hook_runner, "turn_end", messages, turn, max_turns, guard.counter["tools"], ctx)

        import asyncio as _asyncio_m
        _native = bool(getattr(ctx, "native_tool_use", True))
        _mem_mod = getattr(ctx, "memory_module", None)
        def _refresh_metrics() -> None:
            sm.update_context(
                messages, ctx.system_prompt or "", ctx.tools, ctx.context_config,
                native_tool_use=_native,
            )
            sm.update_memory(_mem_mod)
            sm.emit_to_collector()
        await _asyncio_m.to_thread(_refresh_metrics)

        _relay_event(ctx, {
            "type": "turn_complete",
            "turn": turn + 1,
            "tool_calls_total": guard.counter["tools"],
            "agent_id": ctx.agent_id,
        })
        try:
            from digitorn.core.runtime import run_tracker as _runs
            _run_id = getattr(ctx, "current_run_id", None)
            _runs.emit_event(_run_id, "turn", {
                "turn": turn + 1,
                "tool_calls": guard.counter["tools"],
                "agent_id": ctx.agent_id,
            })
            _runs.increment_turns(_run_id)
        except Exception as exc:
            logger.debug("run_tracker turn emit failed: %s", exc)

    return TurnResult(
        content="[Max turns reached]",
        tool_calls_count=guard.counter["tools"],
        turns_used=max_turns,
        truncated=True,
        error="Max turns reached",
        tool_calls=collected_calls,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
    )


async def _inject_turn_limit_warning(
    ctx: AgentContext,
    messages: list[dict], turn: int, max_turns: int, tool_count: int,
) -> None:
    if turn == max_turns - 2 and tool_count > 0:
        from digitorn.core.runtime.system_directives import SYS_TURN_LIMIT_NEAR
        await inject_system_directive(
            ctx,
            content=SYS_TURN_LIMIT_NEAR,
            source="turn_limit_near",
            messages=messages,
            turn=turn,
            metadata={"max_turns": max_turns, "tool_count": tool_count},
        )


def _intra_turn_terminal_reason(ctx: Any, guard: Any) -> tuple[str, str] | None:
    evt = getattr(ctx, "cancel_event", None)
    if evt is not None and evt.is_set():
        reason = getattr(ctx, "cancel_reason", "") or "cooperative cancel"
        return "cancelled", f"cancelled: {reason}"
    if getattr(guard, "kill_turn_reason", ""):
        return "loop_killed", guard.kill_turn_reason
    return None


async def _call_llm(
    ctx: AgentContext,
    messages: list[dict[str, Any]],
    cb: AgentTurnCallbacks,
    turn: int,
) -> tuple[str | None, list[dict], Any, bool]:
    short_circuit = await _run_before_middleware(ctx, messages, turn)
    if short_circuit is not None:
        logger.info("app_middleware_short_circuit agent=%s turn=%d", ctx.agent_id, turn)
        return None, [], short_circuit, False

    _orig = getattr(ctx, "_billing_original_provider", None)
    _resume_at = getattr(ctx, "_billing_fallback_until", 0.0)
    if _orig is not None and _resume_at and time.monotonic() >= _resume_at:
        logger.info(
            "llm_billing_fallback_restore: trying primary again "
            "(%s/%s) after cooldown",
            getattr(_orig, "provider_hint", "?"),
            getattr(_orig, "model", "?"),
        )
        ctx.provider = _orig
        try:
            del ctx._billing_original_provider  # type: ignore[attr-defined]
        except AttributeError:
            pass
        try:
            del ctx._billing_fallback_until  # type: ignore[attr-defined]
        except AttributeError:
            pass

    api_tools = ctx.tools if (ctx.native_tool_use and ctx.tools) else None
    if api_tools is not None:
        _allowed = getattr(ctx, "allowed_tool_names", None)
        if _allowed is not None:
            api_tools = [
                _t for _t in api_tools
                if (_t.get("function", {}).get("name") or "") in _allowed
            ]
    chat_messages = _chat_messages_for_llm(ctx, messages)

    _msg_chars = sum(len(str(m.get("content", ""))) for m in messages)
    logger.info(
        "llm_call turn=%d messages=%d approx_chars=%d agent=%s",
        turn, len(messages), _msg_chars, ctx.agent_id,
    )

    breaker = _get_circuit_breaker(ctx.provider)
    breaker.check()

    try:
        ctx._streaming_assistant_seq = len(messages)
    except AttributeError:
        pass

    try:
        if cb.on_token is not None and hasattr(ctx.provider, "chat_stream"):
            content, tool_calls, response = await streaming_chat(
                ctx.provider, chat_messages, api_tools, ctx.generation_params, cb,
                ctx=ctx,
            )
            streamed = True
            logger.info("streaming_result content_len=%d tool_calls=%d", len(content), len(tool_calls))
        else:
            streamed = False
            response = await ctx.provider.chat(
                chat_messages, tools=api_tools, **ctx.generation_params,
            )
    except Exception as exc:
        return await _handle_llm_error(ctx, messages, exc, breaker, api_tools)

    breaker.record_success()

    usage_obj = getattr(response, "usage", None)
    if usage_obj:
        pt = getattr(usage_obj, "prompt_tokens", 0) or 0
        ct = getattr(usage_obj, "completion_tokens", 0) or 0
        logger.info(
            "llm_usage turn=%d prompt_tokens=%d completion_tokens=%d agent=%s",
            turn, pt, ct, ctx.agent_id,
        )
        _fire_token_counts(cb, usage_obj, skip_out=streamed)

    if not streamed:
        content = extract_content(response)
        tool_calls = extract_tool_calls(response)

    if not tool_calls and content:
        inline = _extract_inline_tool_calls(content)
        if inline is not None:
            text_before, tool_calls = inline
            if text_before:
                await emit_thinking(cb.on_thinking, text_before)
            content = text_before
            logger.info("Inline tool call detected - %d synthetic call(s)", len(tool_calls))

    return content, tool_calls, response, streamed


async def _handle_llm_error(
    ctx: AgentContext,
    messages: list[dict[str, Any]],
    exc: Exception,
    breaker: _ProviderCircuitBreaker,
    api_tools: list[dict] | None,
) -> tuple[str, list[dict], Any, bool]:
    try:
        cb_hooks = getattr(
            getattr(ctx, "context_builder", None), "hook_runner", None,
        )
        if cb_hooks is not None:
            await _run_hooks(
                cb_hooks, "error", messages,
                0, 0, 0, ctx, error=exc,
            )
    except Exception as hook_exc:
        logger.debug("error hook failed during llm_error handling: %s", hook_exc)

    if is_context_overflow(exc):
        _tokens_before = await aestimate_tokens(
            messages, provider=getattr(ctx, "provider", None),
        )
        logger.warning(
            "Context overflow (%d tokens). Emergency compaction.",
            _tokens_before,
        )

        _bus = getattr(ctx, "_event_bus", None)
        _bus_key = getattr(ctx, "_bus_key", None)
        _compact_op_id = None
        if _bus is not None and _bus_key is not None:
            try:
                from digitorn.core.events.envelope import (
                    SessionEvent, OpType, OpState, gen_op_id,
                )
                _compact_op_id = gen_op_id("compact")
                app_id = getattr(ctx, "app_id", "") or ""
                session_id = getattr(ctx, "session_id", "") or ""
                user_id = getattr(ctx, "user_id", "") or "local"
                if app_id and session_id:
                    await _bus.emit(SessionEvent.build(
                        type="compact_started",
                        app_id=app_id,
                        session_id=session_id,
                        user_id=user_id,
                        op_id=_compact_op_id,
                        op_type=OpType.COMPACT,
                        op_state=OpState.RUNNING,
                        payload={
                            "reason": "context_overflow",
                            "tokens_before": _tokens_before,
                            "strategy": "truncate",
                        },
                    ))
            except Exception:
                logger.debug("compact_started emit failed", exc_info=True)

        _compact_result = await emergency_compact(
            ctx, messages, reason="context_overflow",
        )
        if isinstance(_compact_result, dict) and _compact_result.get("compacted"):
            try:
                from digitorn.core.runtime.session_store.bridge import (
                    get_default_bridge,
                )
                _bridge = get_default_bridge()
                _sid_for_compact = getattr(ctx, "session_id", None)
                if _bridge is not None and _sid_for_compact:
                    _store_state = _bridge.store.state(_sid_for_compact)
                    if _store_state is not None and _store_state.messages:
                        _keep = int(_compact_result.get("to_keep_count", 0))
                        _state_msgs = _store_state.messages
                        _cutoff_idx = max(0, len(_state_msgs) - _keep)
                        if _cutoff_idx > 0:
                            _cutoff_seq = int(_state_msgs[_cutoff_idx - 1].seq)
                            await _bridge.store.compact_session(
                                _sid_for_compact,
                                cutoff_seq=_cutoff_seq,
                                summary=(
                                    f"[Context overflow compacted: "
                                    f"{_compact_result.get('to_compact_count', 0)} "
                                    f"older messages removed]"
                                ),
                                strategy="truncate",
                                tokens_estimate=int(
                                    _compact_result.get("tokens_after", 0)
                                ),
                                model=str(getattr(ctx, "model", "") or ""),
                            )
            except Exception as _exc:
                logger.warning(
                    "context_overflow durable compaction failed: %s", _exc,
                )
        _tokens_after = await aestimate_tokens(
            messages, provider=getattr(ctx, "provider", None),
        )

        _sm = _get_session_metrics(ctx)
        _sm.context.compactions += 1
        import asyncio as _asyncio_m
        _native_c = bool(getattr(ctx, "native_tool_use", True))
        await _asyncio_m.to_thread(
            _sm.update_context,
            messages, ctx.system_prompt or "", ctx.tools, ctx.context_config,
            native_tool_use=_native_c,
        )

        try:
            if _bus is not None and _bus_key is not None and _compact_op_id:
                from digitorn.core.events.envelope import (
                    SessionEvent, OpType, OpState,
                )
                app_id = getattr(ctx, "app_id", "") or ""
                session_id = getattr(ctx, "session_id", "") or ""
                user_id = getattr(ctx, "user_id", "") or "local"
                if app_id and session_id:
                    await _bus.emit(SessionEvent.build(
                        type="compact_done",
                        app_id=app_id,
                        session_id=session_id,
                        user_id=user_id,
                        op_id=_compact_op_id,
                        op_type=OpType.COMPACT,
                        op_state=OpState.COMPLETED,
                        payload={
                            "strategy": "truncate",
                            "tokens_before": _tokens_before,
                            "tokens_after": _tokens_after,
                            "tokens_reduced": _tokens_before - _tokens_after,
                            "messages_after": len(messages),
                            "pressure": _sm.context.pressure,
                        },
                    ))
        except Exception:
            logger.debug("compact_done emit failed", exc_info=True)
        try:
            chat_messages = _chat_messages_for_llm(ctx, messages)
            response = await ctx.provider.chat(
                chat_messages, tools=api_tools, **ctx.generation_params,
            )
        except Exception as retry_exc:
            breaker.record_failure()
            raise retry_exc from exc
        breaker.record_success()
        content = extract_content(response)
        tool_calls = extract_tool_calls(response)
        return content, tool_calls, response, False

    if _is_connection_error(exc):
        breaker.record_failure()
        provider_id = getattr(ctx.provider, "provider_id", "unknown")
        base_url = getattr(ctx.provider, "base_url", "")

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            delay = min(2 ** attempt, 16)
            logger.warning(
                "llm_connection_error provider=%s attempt=%d/%d retrying_in=%ds error=%s",
                provider_id, attempt, max_retries, delay, exc,
            )
            await _emit_retry_status(
                ctx,
                attempt=attempt, max_retries=max_retries, delay_s=delay,
                reason="connection_error",
            )
            await asyncio.sleep(delay)
            try:
                chat_messages = _chat_messages_for_llm(ctx, messages)
                response = await ctx.provider.chat(
                    chat_messages, tools=api_tools, **ctx.generation_params,
                )
                breaker.record_success()
                content = extract_content(response)
                tool_calls = extract_tool_calls(response)
                logger.info("llm_reconnected provider=%s after=%d attempts", provider_id, attempt)
                await _clear_retry_status(ctx)
                return content, tool_calls, response, False
            except Exception as retry_exc:
                if not _is_connection_error(retry_exc):
                    raise retry_exc from exc

        await _clear_retry_status(ctx)
        raise RuntimeError(
            f"Connection to LLM provider '{provider_id}' failed after {max_retries} retries ({base_url}). "
            f"{type(exc).__name__}: {exc}. "
            f"Check that the provider is running and reachable."
        ) from exc

    # Gateway 429 quota_exceeded is terminal; bail before the retry loop burns minutes.
    try:
        from digitorn.modules.llm_provider.errors import QuotaExceededError
        if isinstance(exc, QuotaExceededError):
            raise exc
    except ImportError:
        pass

    exc_str = str(exc).lower()
    exc_type = type(exc).__name__
    # Worker-to-daemon serialisation can collapse QuotaExceededError to RuntimeError; match the message too.
    _is_quota_exhausted = (
        "quota_exceeded" in exc_str
        or "quotaexceeded" in exc_type.lower()
        or "cost_usd_quota_exceeded" in exc_str
    )
    if _is_quota_exhausted:
        raise exc
    _is_retriable_llm = (
        "429" in exc_str or "rate" in exc_str or "RateLimit" in exc_type
        or "overload" in exc_str or "529" in exc_str or "capacity" in exc_str
        or "server_error" in exc_str or "500" in exc_str
    )
    if _is_retriable_llm:
        provider_id = getattr(ctx.provider, "provider_id", "unknown")
        max_retries = 5
        for attempt in range(1, max_retries + 1):
            delay = min(5 * (2 ** (attempt - 1)), 120)
            logger.warning(
                "llm_retriable_error provider=%s attempt=%d/%d retrying_in=%ds error=%s",
                provider_id, attempt, max_retries, delay, exc_type,
            )
            await _emit_retry_status(
                ctx,
                attempt=attempt, max_retries=max_retries, delay_s=delay,
                reason="rate_limited",
            )
            await asyncio.sleep(delay)
            try:
                chat_messages = _chat_messages_for_llm(ctx, messages)
                response = await ctx.provider.chat(
                    chat_messages, tools=api_tools, **ctx.generation_params,
                )
                content = extract_content(response)
                tool_calls = extract_tool_calls(response)
                await _clear_retry_status(ctx)
                return content, tool_calls, response, False
            except Exception as retry_exc:
                retry_str = str(retry_exc).lower()
                retry_type = type(retry_exc).__name__
                _still_retriable = (
                    "429" in retry_str or "rate" in retry_str or "RateLimit" in retry_type
                    or "overload" in retry_str or "529" in retry_str or "500" in retry_str
                )
                if not _still_retriable:
                    await _clear_retry_status(ctx)
                    raise retry_exc from exc
        await _clear_retry_status(ctx)

    # Gateway returns `model_not_provided_by_digitorn` as a config error, not billing; short-circuit.
    if "model_not_provided_by_digitorn" in exc_str:
        raise exc

    # Keep the billing match tight; bare "insufficient" also matches OpenAI tool-call validation errors.
    _is_billing = (
        "402" in exc_str
        or "insufficient balance" in exc_str
        or "insufficient credit" in exc_str
        or "insufficient_quota" in exc_str
        or "insufficient funds" in exc_str
        or "exceeded your current quota" in exc_str
        or "credit_balance" in exc_str
        or ("credit" in exc_str
            and ("exhaust" in exc_str or "depleted" in exc_str))
    )
    if _is_billing:
        fallback_brain = getattr(ctx, "_fallback_brain", None)
        if fallback_brain is not None:
            logger.warning(
                "llm_billing_exhausted: %s/%s - switching to fallback: %s/%s",
                getattr(ctx.provider, "provider_hint", "?"),
                getattr(ctx.provider, "model", "?"),
                getattr(fallback_brain, "provider_hint", "?"),
                getattr(fallback_brain, "model", "?"),
            )
            try:
                chat_messages = _chat_messages_for_llm(ctx, messages)
                response = await fallback_brain.chat(
                    chat_messages, tools=api_tools, **ctx.generation_params,
                )
                # Stash primary so the next _call_llm tries it again after the cooldown.
                _BILLING_COOLDOWN_S = 300.0
                if not hasattr(ctx, "_billing_original_provider"):
                    ctx._billing_original_provider = ctx.provider  # type: ignore[attr-defined]
                ctx._billing_fallback_until = time.monotonic() + _BILLING_COOLDOWN_S  # type: ignore[attr-defined]
                ctx.provider = fallback_brain
                content = extract_content(response)
                tool_calls = extract_tool_calls(response)
                logger.info("llm_billing_fallback_ok: %s", getattr(fallback_brain, "model", "?"))
                return content, tool_calls, response, False
            except Exception as fallback_exc:
                logger.warning("llm_billing_fallback_failed: %s", fallback_exc)
        else:
            logger.warning(
                "llm_billing_exhausted: no fallback brain configured. "
                "Add 'fallback:' to the agent's brain config in app.yaml."
            )
            raise RuntimeError(
                f"LLM billing error ({getattr(ctx.provider, 'provider_hint', '?')}"
                f"/{getattr(ctx.provider, 'model', '?')}) and no "
                f"`brain.fallback` is configured in the app YAML. "
                f"Either refill the provider credit or add a "
                f"`fallback:` block under `brain:` pointing at a "
                f"second provider (e.g. anthropic claude-haiku-4-5). "
                f"Original error: {exc}"
            ) from exc

    raise exc


async def _execute_single_tool(
    ctx: AgentContext,
    call: dict[str, Any],
    guard: LoopState,
    cb: AgentTurnCallbacks,
    messages: list[dict[str, Any]],
    turn: int,
    max_turns: int,
) -> tuple[Any, str, dict, str, bool, str]:
    """Execute a single tool call. Returns (result, name, args, call_id, ok, error)."""
    guard.counter["tools"] += 1
    tool_name = call.get("function", {}).get("name", "")
    tool_args = call.get("function", {}).get("arguments") or {}
    call_id = call.get("id") or f"call_{uuid.uuid4().hex[:12]}"

    if isinstance(tool_args, str):
        tool_args = parse_tool_args(tool_args)
    if not isinstance(tool_args, dict):
        tool_args = {}

    if not tool_name:
        logger.warning(
            "tool_call_skipped_empty_name call_id=%s args_keys=%s",
            call_id, list((tool_args or {}).keys())[:5],
        )
        err_msg = (
            "Tool call ignored: function.name was empty. Re-emit the "
            "call with a valid tool name from the available tools."
        )
        return (
            {"success": False, "error": err_msg},
            "unknown", tool_args, call_id, False, err_msg,
        )

    if cb.on_tool_start is not None:
        try:
            try:
                await cb.on_tool_start(tool_name, tool_args, call_id)
            except TypeError:
                await cb.on_tool_start(tool_name, tool_args)  # type: ignore[call-arg]
        except asyncio.CancelledError:
            raise
        except Exception as cb_exc:
            logger.warning(
                "callback_error on_tool_start tool=%s: %s",
                tool_name, cb_exc, exc_info=True,
            )

    if cb.hook_runner is not None:
        _gated, _gate_reason = await _run_tool_hooks(
            cb.hook_runner, "tool_start", messages, turn, max_turns,
            guard.counter["tools"], ctx, tool_name, tool_args,
        )
        if _gated:
            logger.info(
                "tool_gated_by_hook tool=%s reason=%s",
                tool_name, _gate_reason,
            )
            result = {"success": False, "error": _gate_reason}
            _append_tool_result(
                ctx, messages, call_id, tool_name, result, False, cb,
                tool_args=tool_args if isinstance(tool_args, dict) else {},
            )
            if cb.on_tool_call is not None:
                try:
                    await cb.on_tool_call(tool_name, tool_args, result, call_id)
                except Exception as cb_exc:
                    logger.debug("on_tool_call callback failed (gate path): %s", cb_exc)
            return result, tool_name, tool_args, call_id, False, _gate_reason

    rt = ctx.runtime_config
    tool_timeout = getattr(ctx, "tool_timeout", None) or getattr(rt, "tool_timeout", 600.0)
    if "wait" in tool_name.lower() and isinstance(tool_args, dict):
        wait_t = tool_args.get("timeout")
        if isinstance(wait_t, (int, float)) and wait_t > tool_timeout:
            # Cap per-tool wait to agent_turn timeout so the turn budget is never exceeded.
            turn_timeout = getattr(rt, "timeout", 3600.0)
            tool_timeout = min(wait_t + 30.0, turn_timeout)

    _behavior = getattr(ctx, "behavior_module", None)
    _session_id = getattr(ctx, "session_id", "") or ""
    logger.info("behavior_pre_check tool=%s behavior=%s session=%s", tool_name, _behavior is not None, _session_id[:12])
    _behavior_notes: list[str] = []
    if _behavior is not None and hasattr(_behavior, "pre_tool_check") and _session_id:
        _allowed, _violations = _behavior.pre_tool_check(
            _session_id, tool_name, tool_args,
            agent_text=getattr(ctx, "_last_agent_text", ""),
        )
        if _violations:
            # Skip system messages here; they would break the assistant->tool sequence required by LLM APIs.
            _behavior_notes.extend(_violations)
        if not _allowed:
            block_error = _violations[0] if _violations else "Blocked by behavior rule."
            result = {"success": False, "error": block_error}
            ok, err = False, block_error
            if not hasattr(ctx, "_pending_behavior_notes"):
                ctx._pending_behavior_notes = []
            ctx._pending_behavior_notes.extend(_behavior_notes)
            if cb.on_tool_call is not None:
                try:
                    await cb.on_tool_call(tool_name, tool_args, result, call_id)
                except Exception as cb_exc:
                    logger.debug("on_tool_call callback failed (behavior-block path): %s", cb_exc)
            return result, tool_name, tool_args, call_id, False, err

    try:
        result = await asyncio.wait_for(
            execute_tool(ctx, tool_name, tool_args), timeout=tool_timeout,
        )
        attempts = 0
        while needs_approval(result) and ctx.approval_queue is not None and attempts < 3:
            attempts += 1
            try:
                result = await handle_approval(ctx, tool_name, tool_args, result)
            except asyncio.CancelledError:
                raise
            except Exception as approval_exc:
                logger.warning(
                    "handle_approval_error tool=%s attempt=%d: %s",
                    tool_name, attempts, approval_exc, exc_info=True,
                )
                result = {
                    "success": False,
                    "error": f"Approval handler failed: {type(approval_exc).__name__}: {approval_exc}",
                }
                break
    except asyncio.TimeoutError:
        logger.warning("tool_timeout tool=%s timeout=%.0fs", tool_name, tool_timeout)
        result = {"success": False, "error": f"Tool '{tool_name}' timed out after {tool_timeout:.0f}s."}
    except PermissionError as exc:
        logger.warning("sandbox_blocked tool=%s error=%s", tool_name, exc)
        result = {
            "success": False,
            "error": (
                f"OS sandbox blocked '{tool_name}': {exc}. "
                "The app YAML does not grant sufficient permissions for this operation."
            ),
        }
    except OSError as exc:
        import errno as _errno
        if exc.errno in (_errno.EACCES, _errno.EPERM):
            logger.warning("sandbox_blocked tool=%s errno=%d error=%s", tool_name, exc.errno, exc)
            result = {
                "success": False,
                "error": f"OS sandbox denied access for '{tool_name}': {exc.strerror}",
            }
        else:
            logger.warning("tool_os_error tool=%s error=%s", tool_name, exc, exc_info=True)
            result = {"success": False, "error": f"Tool '{tool_name}' raised OSError: {exc}"}
    except Exception as exc:
        logger.warning("tool_execution_error tool=%s error=%s", tool_name, exc, exc_info=True)
        result = {"success": False, "error": f"Tool '{tool_name}' raised {type(exc).__name__}: {exc}"}

    ok, err = _extract_result_status(result)

    if _behavior is not None and hasattr(_behavior, "post_tool_check") and _session_id:
        _reminders = _behavior.post_tool_check(_session_id, tool_name, tool_args, result)
        if _reminders:
            _behavior_notes.extend(_reminders)

    if ctx.memory_module is not None:
        from digitorn.modules.memory.hooks import on_tool_result
        on_tool_result(ctx.memory_module, tool_name, tool_args, result)

    if cb.on_tool_call is not None:
        try:
            try:
                await cb.on_tool_call(tool_name, tool_args, result, call_id)
            except TypeError:
                await cb.on_tool_call(tool_name, tool_args, result)  # type: ignore[call-arg]
        except asyncio.CancelledError:
            raise
        except Exception as cb_exc:
            logger.warning(
                "callback_error on_tool_call tool=%s: %s",
                tool_name, cb_exc, exc_info=True,
            )

    if cb.hook_runner is not None:
        await _run_tool_hooks(
            cb.hook_runner, "tool_end", messages, turn, max_turns,
            guard.counter["tools"], ctx, tool_name, tool_args,
            result=result, ok=ok,
        )

    if _behavior_notes:
        if not hasattr(ctx, "_pending_behavior_notes"):
            ctx._pending_behavior_notes = []
        ctx._pending_behavior_notes.extend(_behavior_notes)

    return result, tool_name, tool_args, call_id, ok, err


async def _flush_behavior_notes(ctx: AgentContext, messages: list[dict[str, Any]]) -> None:
    """Inject pending behavior notes into messages (after tool results)."""
    notes = getattr(ctx, "_pending_behavior_notes", None)
    if notes:
        for note in notes:
            await inject_system_directive(
                ctx,
                content=note,
                source="behavior_pending",
                messages=messages,
            )
        ctx._pending_behavior_notes = []


def _append_tool_result(
    ctx: AgentContext,
    messages: list[dict[str, Any]],
    call_id: str,
    tool_name: str,
    result: Any,
    ok: bool,
    cb: AgentTurnCallbacks,
    *,
    tool_args: dict[str, Any] | None = None,
) -> None:
    image_blocks = None
    if ok and isinstance(result, dict):
        rd = result.get("data") if "data" in result else result
        if isinstance(rd, dict):
            image_blocks = format_image_tool_result(rd)

    if image_blocks is not None:
        messages.append({"role": "tool", "tool_call_id": call_id, "content": image_blocks})
    else:
        serialized = serialize_result(result)
        max_chars = max_tool_result_chars(ctx)
        if len(serialized) > max_chars:
            serialized = truncate_tool_result(serialized, max_chars, tool_name)
        messages.append({"role": "tool", "tool_call_id": call_id, "content": serialized})

    _meta = getattr(result, "metadata", None)
    if not _meta and isinstance(result, dict):
        _meta = result.get("metadata")
    if isinstance(_meta, dict) and "image_data" in _meta:
        from digitorn.core.runtime.multimodal import inject_tool_image
        inject_tool_image(
            messages,
            image_data=_meta["image_data"],
            media_type=_meta.get("media_type", "image/png"),
            tool_name=tool_name,
        )

    _serialized_for_event = serialize_result(result)
    _max = max_tool_result_chars(ctx)
    if len(_serialized_for_event) > _max:
        _serialized_for_event = truncate_tool_result(
            _serialized_for_event, _max, tool_name,
        )
    _err: str | None = None
    if not ok and isinstance(result, dict):
        _err_val = result.get("error")
        _err = _err_val if isinstance(_err_val, str) else None
    _emit_tool_events_bg(
        ctx, call_id, tool_name, tool_args or {},
        _serialized_for_event, ok, error=_err,
    )


def _emit_tool_events_bg(
    ctx: Any,
    call_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    serialized_output: str,
    ok: bool,
    *,
    error: str | None = None,
) -> None:
    try:
        from digitorn.core.runtime.persist_worker import get_default_worker
        worker = get_default_worker()
        worker.submit(
            _emit_tool_events_async,
            ctx, call_id, tool_name, tool_args,
            serialized_output, ok, error,
        )
    except Exception as exc:
        logger.debug("emit_tool_events_submit_failed: %s", exc)


async def _emit_tool_events_async(
    ctx: Any,
    call_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    serialized_output: str,
    ok: bool,
    error: str | None,
) -> None:
    try:
        from digitorn.core.runtime.session_store.bridge import (
            get_default_bridge,
        )
        _bridge = get_default_bridge()
        if _bridge is None:
            return
        session_id = getattr(ctx, "session_id", "") or ""
        if not session_id:
            return
        app_id = getattr(ctx, "app_id", "") or "default"
        user_id = getattr(ctx, "user_id", "") or ""
        await _bridge.record(
            kind="event",
            type="tool_call",
            app_id=app_id, session_id=session_id, user_id=user_id,
            seq=0,
            tool_call_id=call_id,
            name=tool_name,
            payload={
                "id": call_id, "name": tool_name,
                "arguments": tool_args,
            },
        )
        await _bridge.record(
            kind="event",
            type="tool_result",
            app_id=app_id, session_id=session_id, user_id=user_id,
            seq=0,
            tool_call_id=call_id,
            payload={
                "tool_call_id": call_id,
                "output": serialized_output,
                "error": error,
            },
            success=ok,
        )
    except Exception as exc:
        logger.debug(
            "emit_tool_events_failed call=%s tool=%s: %s",
            call_id, tool_name, exc,
        )



async def _run_before_middleware(
    ctx: AgentContext, messages: list[dict[str, Any]], turn: int,
) -> str | None:
    """Run app middleware before LLM call. Returns short_circuit text or None."""
    if ctx.app_middleware is None:
        return None

    from digitorn.core.middleware import AppMiddlewareContext
    mw_ctx = AppMiddlewareContext(
        agent_id=ctx.agent_id,
        system_prompt=ctx.system_prompt,
        messages=messages,
        turn=turn,
    )
    short_circuit = await ctx.app_middleware.run_before(mw_ctx)

    if mw_ctx.system_prompt != ctx.system_prompt:
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = mw_ctx.system_prompt

    return short_circuit


async def _run_after_middleware(
    ctx: AgentContext,
    messages: list[dict[str, Any]],
    turn: int,
    content: str | None,
    tool_calls: list[dict],
) -> str:
    """Run app middleware after LLM call."""
    if content is None:
        return ""
    if ctx.app_middleware is None:
        return content

    from digitorn.core.middleware import AppMiddlewareContext
    mw_ctx = AppMiddlewareContext(
        agent_id=ctx.agent_id,
        system_prompt=ctx.system_prompt,
        messages=messages,
        turn=turn,
    )
    return await ctx.app_middleware.run_after(mw_ctx, content, tool_calls)



async def _check_unfinished_work(ctx: AgentContext, messages: list[dict[str, Any]]) -> bool:
    """Return True if we should continue the loop (unfinished work)."""
    if ctx.memory_module is None or ctx.completion_reminded:
        return False
    has_unfinished, details = ctx.memory_module.store.working.has_unfinished_work()
    if not has_unfinished:
        return False
    ctx.completion_reminded = True
    from digitorn.core.runtime.system_directives import SYS_NUDGE_UNFINISHED_WORK
    await inject_system_directive(
        ctx,
        content=SYS_NUDGE_UNFINISHED_WORK.format(details=details),
        source="nudge_unfinished_work",
        messages=messages,
        metadata={"details": details},
    )
    return True


async def _nudge_empty_response(
    ctx: AgentContext, messages: list[dict[str, Any]], content: str | None, tool_count: int,
) -> bool:
    """Return True if we should continue (empty response nudge)."""
    if not content or content.strip() or tool_count == 0 or ctx.nudged_response:
        return False
    ctx.nudged_response = True
    from digitorn.core.runtime.system_directives import SYS_NUDGE_EMPTY_RESPONSE
    await inject_system_directive(
        ctx,
        content=SYS_NUDGE_EMPTY_RESPONSE,
        source="nudge_empty_response",
        messages=messages,
        metadata={"tool_count": tool_count},
    )
    return True


def _build_final_result(
    content: str | None,
    counter: dict[str, int],
    collected: list[ToolCallInfo],
    usage: SessionUsage,
) -> TurnResult:
    return TurnResult(
        content=content or "",
        tool_calls_count=counter["tools"],
        turns_used=counter["turns"],
        tool_calls=collected,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
    )



async def _emit_thinking_for_turn(
    cb: AgentTurnCallbacks,
    content: str | None,
    tool_calls: list[dict],
    response: Any,
    streamed: bool,
) -> None:
    """Emit thinking content after LLM call, before tool execution."""
    if not streamed and content and content.strip():
        await emit_thinking(cb.on_thinking, content.strip())
        return

    raw = getattr(response, "raw", {}) or {}
    choices = raw.get("choices", [{}])
    if choices:
        msg = choices[0].get("message", {})
        reasoning = msg.get("reasoning_content") or msg.get("thinking") or ""
        if reasoning:
            await emit_thinking(cb.on_thinking, reasoning)
            return

    # No synthetic thinking from tool calls - the UI already shows tool summaries.
    # Only emit real reasoning from the model (content or reasoning_content).



async def _call_memory_turn_start(
    ctx: AgentContext, messages: list[dict[str, Any]], turn: int,
) -> None:
    if ctx.memory_module is not None:
        from digitorn.modules.memory.hooks import on_turn_start
        on_turn_start(ctx.memory_module, messages, turn, session_id=ctx.session_id)
    # Hydrate the preview canvas from the session's persisted workspace
    # snapshot before any tool call runs.
    preview = getattr(ctx, "preview_module", None)
    if preview is not None:
        try:
            if hasattr(preview, "activate_session"):
                await preview.activate_session(
                    ctx.session_id,
                    user_id=getattr(ctx, "user_id", None),
                    workspace=getattr(ctx, "workspace", "") or None,
                )
            elif hasattr(preview, "set_active_session"):
                try:
                    preview.set_active_session(ctx.session_id, user_id=getattr(ctx, "user_id", None))
                except TypeError:
                    preview.set_active_session(ctx.session_id)
        except Exception as exc:
            logger.warning(
                "preview_activate_session_failed sid=%s: %s",
                ctx.session_id, exc,
            )
    # Same wiring for the widget module - each session's mounted widgets
    # are isolated and events are routed via Socket.IO session room.
    widget = getattr(ctx, "widget_module", None)
    if widget is not None and hasattr(widget, "set_active_session"):
        widget.set_active_session(ctx.session_id)
    cron = getattr(ctx, "cron_native_module", None)
    if cron is not None and hasattr(cron, "set_active_session"):
        cron.set_active_session(ctx.session_id)


def _call_memory_turn_end(
    ctx: AgentContext,
    messages: list[dict[str, Any]],
    turn: int,
    collected: list[ToolCallInfo],
    tool_calls: list[dict],
) -> None:
    if ctx.memory_module is not None:
        from digitorn.modules.memory.hooks import on_turn_end
        # Slicing with -0 returns the full list (list[-0:] == list[0:]); handle n==0 explicitly.
        n = len(tool_calls)
        if n == 0:
            turn_calls: list[dict[str, Any]] = []
        else:
            turn_calls = [
                {"name": c.name, "params": c.params, "success": c.success}
                for c in collected[-n:]
            ]
        on_turn_end(ctx.memory_module, messages, turn, turn_calls)



async def _run_hooks(
    hook_runner: Any,
    event: str,
    messages: list[dict[str, Any]],
    turn: int,
    max_turns: int,
    tool_calls_count: int,
    ctx: AgentContext,
    *,
    error: Exception | None = None,
) -> None:
    if hook_runner is None:
        return
    from digitorn.core.runtime.hooks import TurnState

    cc = ctx.context_config
    state = TurnState(
        messages=messages,
        turn=turn,
        max_turns=max_turns,
        tool_calls_count=tool_calls_count,
        agent_id=ctx.agent_id,
        tool_injection=ctx.tool_injection,
        max_context_tokens=cc.max_tokens,
        output_reserved=cc.output_reserved,
        _last_compact_turn=ctx.last_compact_turn,
        _agent_context=ctx,
    )
    if error is not None:
        state._error = error  # type: ignore[attr-defined]
        state._error_code = _classify_error_code(error)  # type: ignore[attr-defined]
    try:
        await hook_runner.run(event, state)
    except Exception as exc:
        logger.warning("Hooks error (%s): %s", event, exc, exc_info=True)


def _classify_error_code(exc: Exception) -> str:
    msg = str(exc).lower()
    if "rate" in msg and "limit" in msg:
        return "rate_limit"
    if "context" in msg and ("overflow" in msg or "too long" in msg):
        return "context_overflow"
    if "402" in msg or "insufficient balance" in msg or "credit" in msg:
        return "billing"
    if "timeout" in msg:
        return "timeout"
    if "auth" in msg or "401" in msg or "403" in msg:
        return "auth"
    if "connection" in msg or "network" in msg:
        return "network"
    return "internal"


async def _run_tool_hooks(
    hook_runner: Any,
    event: str,
    messages: list[dict[str, Any]],
    turn: int,
    max_turns: int,
    tool_calls_count: int,
    ctx: AgentContext,
    tool_name: str,
    tool_params: dict[str, Any],
    *,
    result: Any = None,
    ok: bool = True,
) -> tuple[bool, str]:
    from digitorn.core.runtime.hooks import TurnState

    cc = ctx.context_config
    state = TurnState(
        messages=messages,
        turn=turn,
        max_turns=max_turns,
        tool_calls_count=tool_calls_count,
        agent_id=ctx.agent_id,
        tool_injection=ctx.tool_injection,
        max_context_tokens=cc.max_tokens,
        output_reserved=cc.output_reserved,
        _last_compact_turn=ctx.last_compact_turn,
        _agent_context=ctx,
    )
    state = _th.make_tool_state(state, tool_name, tool_params, result=result, ok=ok)
    try:
        await hook_runner.run(event, state)
    except Exception as exc:
        logger.warning("Tool hooks error (%s, %s): %s", event, tool_name, exc, exc_info=True)
    blocked = bool(getattr(state, "_gate_blocked", False))
    reason = str(getattr(state, "_gate_reason", "") or "Blocked by hook policy.")
    return blocked, reason


async def _get_diagnostics_note(ctx: AgentContext, tool_args: dict[str, Any]) -> str | None:
    path = tool_args.get("path", "")
    if not path or ctx.lsp_module is None:
        return None
    try:
        from digitorn.modules.lsp.params import NotifyChangeParams
        dr = await ctx.lsp_module.execute(
            "notify_change", NotifyChangeParams(path=path).model_dump(),
        )
        data = getattr(dr, "data", None) or {}
        diags = data.get("diagnostics", [])
        if not diags:
            return None
        lines = [
            f"  {d.get('severity', 'info')} L{d.get('line', 0)}: {d.get('message', '')}"
            for d in diags[:10]
        ]
        count = len(diags)
        return f"Diagnostics for {Path(path).name} ({count} issue{'s' if count > 1 else ''}):\n" + "\n".join(lines)
    except Exception as exc:
        logger.debug("lsp diagnostics format failed for %s: %s", path, exc)
        return None


def _extract_result_status(result: Any) -> tuple[bool, str]:
    if isinstance(result, dict):
        return result.get("success", True), result.get("error", "")
    if hasattr(result, "success"):
        return result.success, getattr(result, "error", "") or ""
    return True, ""


def _fire_token_counts(cb: AgentTurnCallbacks, usage: Any, *, skip_out: bool = False) -> None:
    pt = getattr(usage, "prompt_tokens", 0) or 0
    ct = getattr(usage, "completion_tokens", 0) or 0
    if cb.on_in_token is not None and pt > 0:
        try:
            cb.on_in_token(pt)
        except Exception as exc:
            logger.debug("agent_loop best-effort block failed: %s", exc)
    if not skip_out and cb.on_out_token is not None and ct > 0:
        try:
            cb.on_out_token(ct)
        except Exception as exc:
            logger.debug("agent_loop best-effort block failed: %s", exc)


from digitorn.core.runtime.notifications import (  # noqa: E402, F401
    format_bg_task_notification as _format_bg_task_notification,  # noqa: F811
    format_watcher_notification as _format_watcher_notification,  # noqa: F811
)

def _get_session_metrics(ctx: Any) -> Any:
    try:
        from digitorn.core.runtime.session_metrics import get_session_metrics
        app_id = getattr(ctx, "app_id", "") or "default"
        session_id = getattr(ctx, "session_id", "") or "default"
        agent_id = getattr(ctx, "agent_id", "") or "main"
        return get_session_metrics(app_id, session_id, agent_id)
    except Exception as exc:
        logger.warning(
            "session_metrics_fallback agent=%s: %s - metrics will be dropped",
            getattr(ctx, "agent_id", "?"), exc,
        )
        class _Noop:
            def __getattr__(self, _: str) -> Any:
                return lambda *a, **kw: None
        return _Noop()


_BG_PERSIST_TASKS: set[asyncio.Task] = set()
_BG_TITLE_TASKS: set[asyncio.Task] = set()


def _persist_turn_bg(
    ctx: Any,
    messages: list[dict[str, Any]],
    turn: int,
    usage: Any,
    counter: dict[str, int],
    status: str = "active",
) -> None:
    try:
        snapshot = list(messages)
        from digitorn.core.runtime.persist_worker import get_default_worker
        worker = get_default_worker()
        worker.submit(
            _persist_turn, ctx, snapshot, turn, usage, counter,
            status=status,
        )
    except Exception as exc:
        logger.debug("persist_turn_submit_failed: %s", exc)


async def _persist_turn(
    ctx: Any,
    messages: list[dict[str, Any]],
    turn: int,
    usage: Any,
    counter: dict[str, int],
    status: str = "active",
) -> None:
    try:
        from digitorn.core.database import _engine
        if _engine is None:
            return

        from digitorn.core.runtime.persistence import SessionPersister

        app_id = getattr(ctx, "app_id", "") or "default"
        session_id = getattr(ctx, "session_id", "") or "default"
        agent_id = getattr(ctx, "agent_id", "") or "main"
        user_id = getattr(ctx, "user_id", "") or ""
        if not user_id:
            sess = getattr(ctx, "session", None)
            user_id = getattr(sess, "user_id", "") if sess is not None else ""

        _blacklist = getattr(_persist_turn, "_fk_blacklist", None)
        if _blacklist is None:
            _blacklist = set()
            _persist_turn._fk_blacklist = _blacklist  # type: ignore[attr-defined]
        if user_id and user_id in _blacklist:
            return

        sess_for_dirs = getattr(ctx, "session", None)
        persister = SessionPersister(
            app_id, session_id, agent_id, user_id=user_id or None,
            workspace=getattr(sess_for_dirs, "workspace", "") or "",
            workdir=getattr(sess_for_dirs, "workdir", "") or "",
        )

        _commit_now = (status == "completed")

        await persister.save_messages(messages, create_if_missing=_commit_now)

        memory_snap = None
        mem_module = getattr(ctx, "memory_module", None)
        if mem_module and hasattr(mem_module, "store") and mem_module.store:
            try:
                memory_snap = mem_module.store.to_dict()
            except Exception as exc:
                logger.debug("memory snapshot for persist failed: %s", exc)

        metrics_snap = None
        try:
            sm = _get_session_metrics(ctx)
            if hasattr(sm, "snapshot"):
                metrics_snap = sm.snapshot()
        except Exception as exc:
            logger.debug("metrics snapshot for persist failed: %s", exc)

        await persister.save_checkpoint(
            turn=turn,
            status=status,
            prompt_tokens=getattr(usage, "prompt_tokens", 0),
            completion_tokens=getattr(usage, "completion_tokens", 0),
            tool_calls_count=counter.get("tools", 0),
            memory_snapshot=memory_snap,
            metadata={"metrics": metrics_snap} if metrics_snap else None,
            create_if_missing=_commit_now,
        )

        if _commit_now and turn == 0:
            try:
                from digitorn.core.runtime.title_generator import maybe_update_session_title
                sess = getattr(ctx, "session", None)
                store = getattr(ctx, "session_store", None)
                if sess is not None and not getattr(sess, "_title_semantic_generated", False):
                    sess._title_semantic_generated = True  # type: ignore[attr-defined]
                    _title_task = asyncio.create_task(
                        maybe_update_session_title(ctx, sess, session_store=store),
                        name=f"title-gen:{getattr(ctx, 'session_id', 'unknown')}",
                    )
                    _BG_TITLE_TASKS.add(_title_task)
                    _title_task.add_done_callback(_BG_TITLE_TASKS.discard)
            except Exception as exc:
                logger.debug("title_gen_dispatch_failed: %s", exc)
    except Exception as exc:
        _msg = str(exc)
        _is_shutdown_noise = (
            "cannot schedule new futures after shutdown" in _msg
            or "Event loop is closed" in _msg
            or isinstance(exc, asyncio.CancelledError)
        )
        if _is_shutdown_noise:
            logger.debug("persist_turn_skipped_during_shutdown: %s", exc)
            return
        if "user_sessions_user_id_fkey" in _msg or (
            "ForeignKeyViolationError" in _msg and "user_id" in _msg
        ):
            _bl = getattr(_persist_turn, "_fk_blacklist", None)
            if _bl is None:
                _bl = set()
                _persist_turn._fk_blacklist = _bl  # type: ignore[attr-defined]
            try:
                _uid = (
                    getattr(ctx, "user_id", "")
                    or getattr(getattr(ctx, "session", None), "user_id", "")
                    or ""
                )
                if _uid:
                    _bl.add(_uid)
                    logger.warning(
                        "persist_turn_blacklisted user_id=%s reason=%s "
                        "(future persists for this user_id will be skipped "
                        "until daemon restart)",
                        _uid, "fk_violation_users",
                    )
                    return
            except Exception as bl_exc:
                logger.debug("fk_blacklist update failed: %s", bl_exc)
        logger.warning("persist_turn_failed: %s", exc, exc_info=True)


__all__ = [
    "agent_turn",
    "_format_bg_task_notification",
    "_format_watcher_notification",
]

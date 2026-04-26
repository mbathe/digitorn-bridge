"""Agent loop — the core execution cycle.

    messages = [system, user]
    loop:
        response = provider.chat(messages, tools)
        if no tool_calls → return response (done)
        for each tool_call:
            result = execute_tool(name, params)
            messages.append(tool_result)
        continue

This module is mode-agnostic. The modes (one_shot, conversation,
background) call agent_turn() with the appropriate messages.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from digitorn.core.runtime.callbacks import AgentTurnCallbacks
from digitorn.core.runtime.compaction import (
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
from digitorn.core.runtime.tool_exec import execute_tool, handle_approval, needs_approval
from digitorn.core.runtime.tracking import SessionUsage, format_image_tool_result
from digitorn.core.runtime.types import AgentContext, ToolCallInfo, TurnResult
import digitorn.core.runtime.tool_hooks as _th

logger = logging.getLogger(__name__)

# Backward-compatible aliases (old private names used by tests/consumers)
_extract_content = extract_content  # noqa: F811
_extract_tool_calls = extract_tool_calls  # noqa: F811
_serialize_result = serialize_result  # noqa: F811
_is_context_overflow = is_context_overflow  # noqa: F811
_emergency_compact = emergency_compact  # noqa: F811
_format_bg_task_notification = format_bg_task_notification  # noqa: F811
_format_watcher_notification = format_watcher_notification  # noqa: F811
_persist_notification_to_memory = _persist_to_memory  # noqa: F811


# ── Parallel tool execution helpers ──────────────────────────────────

# Tools that only read state and never modify files, databases, or system state.
# When ALL tool_calls in a single LLM response are in this set, they run concurrently.
_READ_ONLY_ACTIONS = frozenset({
    # filesystem
    "read", "grep", "glob", "ls", "find",
    "filesystem__read", "filesystem__grep", "filesystem__glob", "filesystem__ls",
    "filesystem.read", "filesystem.grep", "filesystem.glob", "filesystem.ls",
    # web
    "search", "fetch", "extract",
    "web__search", "web__fetch", "web__extract",
    "web.search", "web.fetch", "web.extract",
    # agent (each agent is isolated — safe to spawn in parallel)
    "agent", "agent_spawn__agent", "agent_spawn.agent",
    "spawn_agent", "agent_spawn__spawn_agent", "agent_spawn.spawn_agent",
    "agent_status", "agent_spawn__agent_status", "agent_spawn.agent_status",
    "agent_list", "agent_spawn__agent_list", "agent_spawn.agent_list",
    # memory (no side effects across calls)
    "remember", "memory__remember", "memory.remember",
    "task_create", "memory__task_create", "memory.task_create",
    "task_update", "memory__task_update", "memory.task_update",
    "set_goal", "memory__set_goal", "memory.set_goal",
    # lsp (read-only)
    "diagnostics", "lsp__diagnostics", "lsp.diagnostics",
    "check", "lsp__check", "lsp.check",
    # schema (read-only)
    "schema", "database__schema", "database.schema",
})


def _all_read_only(tool_calls: list[dict]) -> bool:
    """Check if all tool_calls in a batch are read-only (safe for parallel execution)."""
    for call in tool_calls:
        name = call.get("function", {}).get("name", "")
        if name not in _READ_ONLY_ACTIONS:
            return False
    return True


# ── Circuit breaker ──────────────────────────────────────────────────


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
    """Remove circuit breaker state for given provider IDs.

    Called during app undeploy to prevent stale state from
    affecting redeployed apps.  Pass no args to clear all.
    """
    if not provider_ids:
        _circuit_breakers.clear()
        return
    for pid in provider_ids:
        _circuit_breakers.pop(pid, None)


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


# ── Public API ───────────────────────────────────────────────────────


async def agent_turn(
    ctx: AgentContext,
    messages: list[dict[str, Any]],
    *,
    max_turns: int = 120,
    timeout: float = 1200.0,
    callbacks: AgentTurnCallbacks | None = None,
    # Legacy kwargs — forwarded to AgentTurnCallbacks if callbacks is None
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
    """Execute one full agent turn: chat → tools → chat → ... until done."""
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

    try:
        return await asyncio.wait_for(
            _loop(ctx, messages, max_turns, callbacks),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return TurnResult(
            content="[Timeout reached]", truncated=True, error="Timeout reached",
        )
    except (MemoryError, SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:
        logger.error("agent_turn_error type=%s error=%s", type(exc).__name__, exc, exc_info=True)
        return TurnResult(content="", error=str(exc))



def _relay_event(ctx: AgentContext, event: dict[str, Any]) -> None:
    """Send an event to the parent coordinator's progress relay (if any)."""
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
    """Inner loop without timeout wrapper."""
    collected_calls: list[ToolCallInfo] = []
    usage = SessionUsage()
    guard = LoopState.from_runtime_config(ctx.runtime_config)

    # Per-session metrics (isolated, real-time)
    sm = _get_session_metrics(ctx)
    sm.model = getattr(ctx.provider, "model", "")
    sm.provider = getattr(ctx.provider, "provider_hint", "") or getattr(ctx.provider, "provider_id", "")
    sm.max_turns = max_turns
    sm.user_id = getattr(ctx, "user_id", "") or ""

    _prev_turn_had_streamed_text = False

    for turn in range(max_turns):
        guard.counter["turns"] = turn + 1
        sm.record_turn(turn)

        _inject_turn_limit_warning(messages, turn, max_turns, guard.counter["tools"])
        # `session_start` fires once per session — on turn 0 only —
        # before the regular `turn_start`. Lets apps run per-session
        # setup hooks (hydrate context, send welcome notifications).
        if turn == 0:
            await _run_hooks(
                cb.hook_runner, "session_start",
                messages, turn, max_turns, guard.counter["tools"], ctx,
            )
        await _run_hooks(cb.hook_runner, "turn_start", messages, turn, max_turns, guard.counter["tools"], ctx)
        inject_bg_notifications(ctx, messages)
        await _call_memory_turn_start(ctx, messages, turn)

        # Behavior engine: reset per-turn state + semantic classification
        _beh = getattr(ctx, "behavior_module", None)
        if _beh is not None and hasattr(_beh, "on_turn_start"):
            _beh.on_turn_start(getattr(ctx, "session_id", "") or "")

        _last_msg = messages[-1] if messages else {}
        _is_fresh_user_turn = _last_msg.get("role") == "user"
        if (
            _beh is not None
            and hasattr(_beh, "classify_turn")
            and getattr(_beh, "classify_enabled", False)
            and _is_fresh_user_turn
        ):
            _content = _last_msg.get("content", "")
            _user_msg = str(_content) if not isinstance(_content, list) else str(_content)
            if _user_msg:
                # Build tool inventory: actual tool names + descriptions
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

                # Build workspace context from session metadata
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
                )
                if _directive:
                    logger.info("behavior_directive_injected turn=%d len=%d", turn, len(_directive))
                    messages.append({"role": "system", "content": _directive})
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

        # ── Quota pre-turn check ────────────────────────────────────
        # Before hitting the LLM: enforce `messages` / `tokens_*` /
        # `cost_usd` / `requests` limits at BOTH app and user scopes.
        # A positive check here reserves the slot — we'll ``charge`` the
        # actual counters right after the response comes back so tokens/
        # cost reflect real consumption. If any rule would overflow, we
        # raise and return a truncated TurnResult with a quota_exceeded
        # SSE event the client can render.
        _qstore = _get_quota_store_from_ctx(ctx)
        if _qstore is not None:
            _app_id = getattr(ctx, "app_id", "") or ""
            _user_id = getattr(ctx, "user_id", "") or None
            try:
                # Pre-charge: 1 message + 1 request. Tokens/cost are
                # charged post-turn once we know the actual counts.
                _qstore.check_and_charge(
                    app_id=_app_id, user_id=_user_id,
                    charges={"requests": 1, "messages": 1},
                    model=getattr(ctx.provider, "model", None) if getattr(ctx, "provider", None) else None,
                )
            except Exception as _quota_exc:
                from digitorn.core.quota import QuotaExceededError
                if isinstance(_quota_exc, QuotaExceededError):
                    _relay_event(ctx, {
                        "type": "quota_exceeded",
                        "turn": turn + 1,
                        "agent_id": ctx.agent_id,
                        **_quota_exc.to_dict(),
                    })
                    _persist_turn_bg(
                        ctx, messages, turn, usage, guard.counter,
                        status="quota_exceeded",
                    )
                    return TurnResult(
                        content="",
                        error=str(_quota_exc),
                        truncated=True,
                    )
                logger.warning("quota pre-check raised: %s", _quota_exc, exc_info=True)

        _llm_t0 = time.monotonic()
        content, tool_calls, response, streamed = await _call_llm(ctx, messages, cb, turn)
        _llm_ms = (time.monotonic() - _llm_t0) * 1000

        # Track whether this turn produced streamed text (for next iteration's separator)
        _prev_turn_had_streamed_text = bool(streamed and content and content.strip())

        # Record LLM metrics
        _resp_usage = getattr(response, "usage", None) if response else None
        _pt = getattr(_resp_usage, "prompt_tokens", 0) or 0
        _ct = getattr(_resp_usage, "completion_tokens", 0) or 0
        sm.record_llm_call(_llm_ms, _pt, _ct)
        usage.prompt_tokens += _pt
        usage.completion_tokens += _ct

        # ── Quota post-turn charge ──────────────────────────────────
        # Now that we know real input/output tokens and cost, incr the
        # token/cost counters. Failure here can't undo the LLM call, so
        # we charge past the limit — the next pre-check bounces the
        # caller. That's the honest bucket semantics.
        if _qstore is not None and (_pt or _ct):
            _cost = 0.0
            try:
                _cost = float(getattr(_resp_usage, "cost_usd", 0) or 0)
            except Exception:
                _cost = 0.0
            _charges: dict[str, float] = {}
            if _pt:
                _charges["tokens_input"] = float(_pt)
            if _ct:
                _charges["tokens_output"] = float(_ct)
            if _pt or _ct:
                _charges["tokens_total"] = float(_pt + _ct)
            if _cost > 0:
                _charges["cost_usd"] = _cost
            try:
                _qstore.check_and_charge(
                    app_id=_app_id, user_id=_user_id,
                    charges=_charges,
                    model=getattr(ctx.provider, "model", None) if getattr(ctx, "provider", None) else None,
                )
            except Exception as _post_exc:
                # Post-charge overflow: the turn happened, but now we're
                # over budget. Log + emit event so the UI shows it; the
                # next call will be refused at pre-check.
                from digitorn.core.quota import QuotaExceededError
                if isinstance(_post_exc, QuotaExceededError):
                    _relay_event(ctx, {
                        "type": "quota_exceeded",
                        "turn": turn + 1,
                        "agent_id": ctx.agent_id,
                        "post_turn": True,
                        **_post_exc.to_dict(),
                    })

        if _pt or _ct:
            _relay_event(ctx, {
                "type": "token_usage",
                "turn": turn + 1,
                "input_tokens": _pt,
                "output_tokens": _ct,
                "agent_id": ctx.agent_id,
            })

        if content is None:
            # Middleware short-circuit
            content = response  # type: ignore[assignment]
            tool_calls = []
            streamed = False

        content = await _run_after_middleware(ctx, messages, turn, content, tool_calls)

        if not tool_calls:
            if _check_unfinished_work(ctx, messages):
                continue
            if _nudge_empty_response(ctx, messages, content, guard.counter["tools"]):
                continue
            _persist_turn_bg(ctx, messages, turn, usage, guard.counter, status="completed")
            return _build_final_result(content, guard.counter, collected_calls, usage)

        # Behavior engine: check agent text for violations (uncertainty, missing plan)
        _beh = getattr(ctx, "behavior_module", None)
        if _beh is not None and hasattr(_beh, "check_agent_text") and content:
            _sid = getattr(ctx, "session_id", "") or ""
            _text_violations = _beh.check_agent_text(_sid, content or "")
            # Store text for pre_tool_check (plan_before_execute)
            ctx._last_agent_text = content or ""  # type: ignore[attr-defined]
        else:
            ctx._last_agent_text = content or ""  # type: ignore[attr-defined]
            _text_violations = []

        await _emit_thinking_for_turn(cb, content, tool_calls, response, streamed)
        _reasoning = getattr(response, "reasoning_content", None) if response else None
        messages.append(build_assistant_message(content, tool_calls, reasoning_content=_reasoning))
        # BANK-GRADE: the assistant message is an **audit event** — a
        # user asked something and the system responded. Persist it
        # synchronously BEFORE dispatching tools so that a crash
        # mid-tool-call still leaves a durable trace of the response.
        # append-only save_messages means we just INSERT, no race.
        await _persist_turn(ctx, messages, turn, usage, guard.counter)

        deferred_notes: list[str] = list(_text_violations)

        # Parallel execution: when multiple tool_calls arrive in a single
        # LLM response and ALL are read-only, execute them concurrently.
        # If any is a write tool, fall back to sequential execution.
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
                    # Wrap any exception into a proper failure tuple so the
                    # caller never has to deal with bare exception objects.
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
                # RT1: defensive — accept both BaseException and tuples that
                # might not have exactly 7 elements (corruption guard).
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
                    _append_tool_result(ctx, messages, c_id, t_name, err_result, False, cb)
                    continue
                if not isinstance(res, tuple) or len(res) != 7:
                    logger.error(
                        "parallel_tool_malformed_result type=%s len=%s",
                        type(res).__name__, len(res) if hasattr(res, "__len__") else "?",
                    )
                    t_name = call.get("function", {}).get("name", "?")
                    c_id = call.get("id", f"call_{uuid.uuid4().hex[:12]}")
                    err_result = {"success": False, "error": "Malformed parallel execution result"}
                    _append_tool_result(ctx, messages, c_id, t_name, err_result, False, cb)
                    continue
                result, tool_name, tool_args, call_id, ok, err, _tool_ms = res
                collected_calls.append(ToolCallInfo(name=tool_name, params=tool_args, success=ok, error=err))
                _append_tool_result(ctx, messages, call_id, tool_name, result, ok, cb)
                _flush_behavior_notes(ctx, messages)
                sm.record_tool_call(tool_name, _tool_ms, ok, err or "")
                # AS16: also relay parallel-path tool calls. The
                # parent coordinator keys on op_id + op_state to drive
                # its own UI correlation (same contract as the
                # Socket.IO session bus).
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

            _para_ms = (time.monotonic() - _para_t0) * 1000
            logger.info("parallel_tools count=%d duration_ms=%.0f", len(tool_calls), _para_ms)
            # Synchronous persist after parallel tool batch — same
            # audit guarantee as sequential.
            await _persist_turn(ctx, messages, turn, usage, guard.counter)

        else:
            # Sequential execution (default for write tools or single calls)
            for call in tool_calls:
                _tool_t0 = time.monotonic()
                result, tool_name, tool_args, call_id, ok, err = await _execute_single_tool(
                    ctx, call, guard, cb, messages, turn, max_turns,
                )
                _tool_ms = (time.monotonic() - _tool_t0) * 1000
                collected_calls.append(ToolCallInfo(name=tool_name, params=tool_args, success=ok, error=err))
                _append_tool_result(ctx, messages, call_id, tool_name, result, ok, cb)
                _flush_behavior_notes(ctx, messages)

                sm.record_tool_call(tool_name, _tool_ms, ok, err or "")

                # AS16: relay live tool_call event to a parent coordinator.
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

                # BANK-GRADE: each tool result is an audit-critical
                # action (a tool WAS executed, the output WAS these
                # bytes). Persist synchronously so that a crash or
                # timeout between tools doesn't leave a gap in the
                # audit trail. append_messages = 1 INSERT, fast.
                await _persist_turn(ctx, messages, turn, usage, guard.counter)

                if ok and tool_name in ("filesystem__edit", "filesystem__write", "filesystem.edit", "filesystem.write"):
                    diag_note = await _get_diagnostics_note(ctx, tool_args)
                    if diag_note:
                        deferred_notes.append(diag_note)

                serialized_len = len(serialize_result(result)) if not isinstance(result, str) else len(result)
                deferred_notes.extend(check_tool_health(guard, tool_name, tool_args, ok, serialized_len))

        deferred_notes.extend(check_delegation(tool_calls, guard.counter["tools"], ctx.tools))
        for note in deferred_notes:
            messages.append({"role": "system", "content": note})

        _call_memory_turn_end(ctx, messages, turn, collected_calls, tool_calls)
        await _run_hooks(cb.hook_runner, "turn_end", messages, turn, max_turns, guard.counter["tools"], ctx)

        # Update real-time metrics: context breakdown, memory
        sm.update_context(
            messages, ctx.system_prompt or "", ctx.tools, ctx.context_config,
            native_tool_use=bool(getattr(ctx, "native_tool_use", True)),
        )
        sm.update_memory(getattr(ctx, "memory_module", None))
        sm.emit_to_collector()

        # AS16: notify parent that one full turn has completed.
        _relay_event(ctx, {
            "type": "turn_complete",
            "turn": turn + 1,
            "tool_calls_total": guard.counter["tools"],
            "agent_id": ctx.agent_id,
        })

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


# ── Turn phases ──────────────────────────────────────────────────────


def _get_quota_store_from_ctx(ctx: Any) -> Any:
    """Resolve the shared ``QuotaStore`` from the agent context.

    The store lives on ``app.state.quota_store`` and is injected at
    bootstrap. The agent context holds a back-ref via ``ctx._app_state``
    or ``ctx.daemon_app_state``. When missing (unit tests, fallback
    contexts), we just skip enforcement.
    """
    for attr in ("quota_store", "_quota_store"):
        val = getattr(ctx, attr, None)
        if val is not None:
            return val
    app_state = (
        getattr(ctx, "_app_state", None)
        or getattr(ctx, "daemon_app_state", None)
        or getattr(ctx, "app_state", None)
    )
    if app_state is not None:
        store = getattr(app_state, "quota_store", None)
        if store is not None:
            return store
        # Build it lazily if the backend limiter exists.
        limiter = getattr(app_state, "rate_limiter", None)
        if limiter is not None:
            try:
                from digitorn.core.quota import QuotaStore
                backend = getattr(limiter, "_backend", None)
                if backend is not None:
                    store = QuotaStore(backend)
                    app_state.quota_store = store
                    return store
            except Exception as exc:
                logger.debug("quota_store lazy init failed: %s", exc)
    return None


def _inject_turn_limit_warning(
    messages: list[dict], turn: int, max_turns: int, tool_count: int,
) -> None:
    if turn == max_turns - 2 and tool_count > 0:
        messages.append({
            "role": "system",
            "content": (
                "You are approaching the turn limit. "
                "Stop calling tools and respond to the user now. "
                "Summarize what you found from the tool results above."
            ),
        })


async def _call_llm(
    ctx: AgentContext,
    messages: list[dict[str, Any]],
    cb: AgentTurnCallbacks,
    turn: int,
) -> tuple[str | None, list[dict], Any, bool]:
    """Call the LLM (streaming or sync). Returns (content, tool_calls, response, streamed).

    If middleware short-circuits, returns (None, [], short_circuit_text, False).
    """
    short_circuit = await _run_before_middleware(ctx, messages, turn)
    if short_circuit is not None:
        logger.info("app_middleware_short_circuit agent=%s turn=%d", ctx.agent_id, turn)
        return None, [], short_circuit, False

    api_tools = ctx.tools if (ctx.native_tool_use and ctx.tools) else None
    chat_messages = to_chat_messages(messages)

    # Debug: trace message count and approximate size per LLM call
    _msg_chars = sum(len(str(m.get("content", ""))) for m in messages)
    logger.info(
        "llm_call turn=%d messages=%d approx_chars=%d agent=%s",
        turn, len(messages), _msg_chars, ctx.agent_id,
    )

    breaker = _get_circuit_breaker(ctx.provider)
    breaker.check()

    # Seed the seq at which the forthcoming assistant message will
    # land. Streaming snapshots use this to UPSERT into ``history_log``
    # at the correct position — same seq ``save_messages`` will claim
    # at turn-end, so the final flush is a no-op (skipped by the
    # existing max-seq pre-check) and the row keeps the content we
    # streamed progressively.
    try:
        ctx._streaming_assistant_seq = len(messages)
    except Exception:
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
        # In streaming mode, out_tokens are already emitted per-chunk by
        # streaming.py — only emit in_tokens here to avoid double counting.
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
            logger.info("Inline tool call detected — %d synthetic call(s)", len(tool_calls))

    return content, tool_calls, response, streamed


async def _handle_llm_error(
    ctx: AgentContext,
    messages: list[dict[str, Any]],
    exc: Exception,
    breaker: _ProviderCircuitBreaker,
    api_tools: list[dict] | None,
) -> tuple[str, list[dict], Any, bool]:
    """Handle LLM call errors — overflow retry or connection failure."""
    # Fire the `error` hook — lets apps log, notify, swap brain, etc.
    # Passes the exception on state via a lightweight attribute so
    # `{{tool.error}}` templates + `error_type` condition can inspect it.
    try:
        cb_hooks = getattr(
            getattr(ctx, "context_builder", None), "hook_runner", None,
        )
        if cb_hooks is not None:
            await _run_hooks(
                cb_hooks, "error", messages,
                0, 0, 0, ctx, error=exc,
            )
    except Exception:
        pass  # hook failure must not mask the original error

    if is_context_overflow(exc):
        _tokens_before = estimate_tokens(messages)
        logger.warning(
            "Context overflow (%d estimated tokens). Emergency compaction.",
            _tokens_before,
        )

        # Emit ``compact_started`` BEFORE running the work so clients
        # that reconnect mid-compaction see a running op_id for
        # which they can show a "compacting…" banner. The same op_id
        # is reused by the terminal ``compact_done`` (or a failure
        # path below) so reconnect logic stays deterministic.
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

        await emergency_compact(ctx, messages, reason="context_overflow")
        _tokens_after = estimate_tokens(messages)

        _sm = _get_session_metrics(ctx)
        _sm.context.compactions += 1
        _sm.update_context(
            messages, ctx.system_prompt or "", ctx.tools, ctx.context_config,
            native_tool_use=bool(getattr(ctx, "native_tool_use", True)),
        )

        # Terminal ``compact_done`` with the SAME op_id.
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
            chat_messages = to_chat_messages(messages)
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
            await asyncio.sleep(delay)
            try:
                chat_messages = to_chat_messages(messages)
                response = await ctx.provider.chat(
                    chat_messages, tools=api_tools, **ctx.generation_params,
                )
                breaker.record_success()
                content = extract_content(response)
                tool_calls = extract_tool_calls(response)
                logger.info("llm_reconnected provider=%s after=%d attempts", provider_id, attempt)
                return content, tool_calls, response, False
            except Exception as retry_exc:
                if not _is_connection_error(retry_exc):
                    raise retry_exc from exc

        raise RuntimeError(
            f"Connection to LLM provider '{provider_id}' failed after {max_retries} retries ({base_url}). "
            f"{type(exc).__name__}: {exc}. "
            f"Check that the provider is running and reachable."
        ) from exc

    # Rate limit (429) / Overloaded (529) — wait and retry
    exc_str = str(exc).lower()
    exc_type = type(exc).__name__
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
            await asyncio.sleep(delay)
            try:
                chat_messages = to_chat_messages(messages)
                response = await ctx.provider.chat(
                    chat_messages, tools=api_tools, **ctx.generation_params,
                )
                content = extract_content(response)
                tool_calls = extract_tool_calls(response)
                return content, tool_calls, response, False
            except Exception as retry_exc:
                retry_str = str(retry_exc).lower()
                retry_type = type(retry_exc).__name__
                _still_retriable = (
                    "429" in retry_str or "rate" in retry_str or "RateLimit" in retry_type
                    or "overload" in retry_str or "529" in retry_str or "500" in retry_str
                )
                if not _still_retriable:
                    raise retry_exc from exc

    # Billing / credit exhausted — try fallback brain from compiled config.
    # Keep the match tight: a bare "insufficient" keyword was too broad and
    # mis-classified messages like OpenAI's 400 "insufficient tool messages
    # following tool_calls" (an orphan-tool-call validation error) as a
    # billing exhaustion — triggering a confusing "LLM billing error" UI
    # toast even though the provider still had balance.
    _is_billing = (
        "402" in exc_str
        or "insufficient balance" in exc_str
        or "insufficient credit" in exc_str
        or "insufficient_quota" in exc_str
        or "insufficient funds" in exc_str
        or "exceeded your current quota" in exc_str
        or "billing" in exc_str
        or "credit_balance" in exc_str
        or ("credit" in exc_str
            and ("exhaust" in exc_str or "depleted" in exc_str))
    )
    if _is_billing:
        fallback_brain = getattr(ctx, "_fallback_brain", None)
        if fallback_brain is not None:
            logger.warning(
                "llm_billing_exhausted: %s/%s — switching to fallback: %s/%s",
                getattr(ctx.provider, "provider_hint", "?"),
                getattr(ctx.provider, "model", "?"),
                getattr(fallback_brain, "provider_hint", "?"),
                getattr(fallback_brain, "model", "?"),
            )
            try:
                chat_messages = to_chat_messages(messages)
                response = await fallback_brain.chat(
                    chat_messages, tools=api_tools, **ctx.generation_params,
                )
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
            # BUG-104: the bare ``exc`` bubbles up as a cryptic stack
            # trace on the client (``APIStatusError: 402 Insufficient
            # Balance``) and the user has no clue they need either to
            # top up the provider OR add a ``fallback:`` brain. Wrap
            # it in a RuntimeError whose message is actionable — the
            # outer error classifier turns that into a clean billing
            # error for the SSE payload.
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

    if cb.on_tool_start is not None:
        try:
            try:
                await cb.on_tool_start(tool_name, tool_args, call_id)
            except TypeError:
                # Backwards compat: old callbacks without call_id param
                await cb.on_tool_start(tool_name, tool_args)  # type: ignore[call-arg]
        except asyncio.CancelledError:
            raise
        except Exception as cb_exc:
            # Surface callback errors at WARNING so they're visible in logs.
            # A failing callback should NOT block tool execution.
            logger.warning(
                "callback_error on_tool_start tool=%s: %s",
                tool_name, cb_exc, exc_info=True,
            )

    if cb.hook_runner is not None:
        await _run_tool_hooks(
            cb.hook_runner, "tool_start", messages, turn, max_turns,
            guard.counter["tools"], ctx, tool_name, tool_args,
        )

    rt = ctx.runtime_config
    tool_timeout = getattr(ctx, "tool_timeout", None) or getattr(rt, "tool_timeout", 600.0)
    if "wait" in tool_name.lower() and isinstance(tool_args, dict):
        wait_t = tool_args.get("timeout")
        if isinstance(wait_t, (int, float)) and wait_t > tool_timeout:
            # RT12: cap the per-tool wait timeout to the agent_turn timeout
            # so we don't exceed the overall turn budget. The agent_turn
            # timeout is the hard ceiling.
            turn_timeout = getattr(rt, "timeout", 3600.0)
            tool_timeout = min(wait_t + 30.0, turn_timeout)

    # ── Behavior enforcement: pre-tool check ──
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
            # DON'T inject system messages here — they'd break the
            # assistant→tool message sequence that LLM APIs require.
            # Instead, collect them and inject AFTER the tool result.
            _behavior_notes.extend(_violations)
        if not _allowed:
            # Tool was BLOCKED — return error as the tool result
            block_error = _violations[0] if _violations else "Blocked by behavior rule."
            result = {"success": False, "error": block_error}
            ok, err = False, block_error
            _append_tool_result(ctx, messages, call_id, tool_name, result, False, cb)
            # NOW inject behavior messages after the tool result
            for _vmsg in _behavior_notes:
                messages.append({"role": "system", "content": _vmsg})
            if cb.on_tool_call is not None:
                try:
                    await cb.on_tool_call(tool_name, tool_args, result, call_id)
                except Exception:
                    pass
            return result, tool_name, tool_args, call_id, False, err

    try:
        result = await asyncio.wait_for(
            execute_tool(ctx, tool_name, tool_args), timeout=tool_timeout,
        )
        # RT7: bounded retry loop with isolated handle_approval errors.
        # Max 3 approval re-execution attempts. If handle_approval itself
        # raises, we capture the error as a failure result instead of
        # letting it propagate (which would lose the original tool result).
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

    # ── Behavior enforcement: post-tool check ──
    # Notes are collected but NOT injected here — the caller injects them
    # after _append_tool_result to avoid breaking assistant→tool message order.
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
                # Backwards compat: old callbacks without call_id param
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

    # Inject behavior notes AFTER the tool result is appended by the caller.
    # We store them on ctx so the caller can flush them after _append_tool_result.
    if _behavior_notes:
        if not hasattr(ctx, "_pending_behavior_notes"):
            ctx._pending_behavior_notes = []
        ctx._pending_behavior_notes.extend(_behavior_notes)

    return result, tool_name, tool_args, call_id, ok, err


def _flush_behavior_notes(ctx: AgentContext, messages: list[dict[str, Any]]) -> None:
    """Inject pending behavior notes into messages (after tool results)."""
    notes = getattr(ctx, "_pending_behavior_notes", None)
    if notes:
        for note in notes:
            messages.append({"role": "system", "content": note})
        ctx._pending_behavior_notes = []


def _append_tool_result(
    ctx: AgentContext,
    messages: list[dict[str, Any]],
    call_id: str,
    tool_name: str,
    result: Any,
    ok: bool,
    cb: AgentTurnCallbacks,
) -> None:
    """Serialize and append tool result to messages."""
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

    # If tool result contains an image in metadata, inject it into messages
    # so the LLM can see it (vision models only)
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


# ── Middleware helpers ────────────────────────────────────────────────


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


# ── Post-LLM checks ─────────────────────────────────────────────────


def _check_unfinished_work(ctx: AgentContext, messages: list[dict[str, Any]]) -> bool:
    """Return True if we should continue the loop (unfinished work)."""
    if ctx.memory_module is None or ctx.completion_reminded:
        return False
    has_unfinished, details = ctx.memory_module.store.working.has_unfinished_work()
    if not has_unfinished:
        return False
    ctx.completion_reminded = True
    messages.append({
        "role": "system",
        "content": (
            f"⚠ You are about to finish, but you still have {details}. "
            f"Check your tasks and notes above. Review them before concluding."
        ),
    })
    return True


def _nudge_empty_response(
    ctx: AgentContext, messages: list[dict[str, Any]], content: str | None, tool_count: int,
) -> bool:
    """Return True if we should continue (empty response nudge)."""
    if not content or content.strip() or tool_count == 0 or ctx.nudged_response:
        return False
    ctx.nudged_response = True
    messages.append({
        "role": "system",
        "content": (
            "You called tools and received results but did not respond to the user. "
            "Now answer the user's original question based on the tool results above."
        ),
    })
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


# ── Thinking emission ────────────────────────────────────────────────


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

    # No synthetic thinking from tool calls — the UI already shows tool summaries.
    # Only emit real reasoning from the model (content or reasoning_content).


# ── Memory hooks ─────────────────────────────────────────────────────


async def _call_memory_turn_start(
    ctx: AgentContext, messages: list[dict[str, Any]], turn: int,
) -> None:
    if ctx.memory_module is not None:
        from digitorn.modules.memory.hooks import on_turn_start
        on_turn_start(ctx.memory_module, messages, turn, session_id=ctx.session_id)
    # Session-aware preview module — every session gets its own canvas
    # state map, and the preview SSE route filters fan-out by session_id.
    # On turn start we use the async ``activate_session`` variant so a
    # reopened session is hydrated from its persisted workspace snapshot
    # BEFORE any tool call runs. Falls back to the sync path for older
    # preview module versions that don't expose ``activate_session``.
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
    # Same wiring for the widget module — each session's mounted widgets
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
        # RT2: handle empty tool_calls case explicitly. Slicing with -0
        # would return the FULL list (Python: list[-0:] == list[0:]),
        # which is wrong — we want zero calls when this turn had none.
        n = len(tool_calls)
        if n == 0:
            turn_calls: list[dict[str, Any]] = []
        else:
            turn_calls = [
                {"name": c.name, "params": c.params, "success": c.success}
                for c in collected[-n:]
            ]
        on_turn_end(ctx.memory_module, messages, turn, turn_calls)


# ── Hook runners ─────────────────────────────────────────────────────


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
    # Attach error context when firing the `error` event so conditions
    # like `error_type` + action templates can inspect the failure.
    if error is not None:
        state._error = error  # type: ignore[attr-defined]
        state._error_code = _classify_error_code(error)  # type: ignore[attr-defined]
    try:
        await hook_runner.run(event, state)
    except Exception as exc:
        logger.warning("Hooks error (%s): %s", event, exc, exc_info=True)


def _classify_error_code(exc: Exception) -> str:
    """Heuristic mapping exception → short code used by the
    `error_type` condition. Mirrors the daemon's classification in
    ``api/apps.py::_classify_error``.
    """
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
) -> None:
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
    except Exception:
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
        except Exception:
            pass
    if not skip_out and cb.on_out_token is not None and ct > 0:
        try:
            cb.on_out_token(ct)
        except Exception:
            pass


# ── Backward-compatible imports ──────────────────────────────────────
# Other modules import these from agent_loop — keep the re-exports.

from digitorn.core.runtime.notifications import (  # noqa: E402, F401
    format_bg_task_notification as _format_bg_task_notification,  # noqa: F811
    format_watcher_notification as _format_watcher_notification,  # noqa: F811
)

def _get_session_metrics(ctx: Any) -> Any:
    """Get or create per-session metrics. Returns a no-op stub if unavailable."""
    try:
        from digitorn.core.runtime.session_metrics import get_session_metrics
        app_id = getattr(ctx, "app_id", "") or "default"
        session_id = getattr(ctx, "session_id", "") or "default"
        agent_id = getattr(ctx, "agent_id", "") or "main"
        return get_session_metrics(app_id, session_id, agent_id)
    except Exception as exc:
        # RT20: log fallback so operators can see when metrics are silently
        # dropped (e.g. database outage). Previously this was completely silent.
        logger.warning(
            "session_metrics_fallback agent=%s: %s — metrics will be dropped",
            getattr(ctx, "agent_id", "?"), exc,
        )
        # Return a no-op stub so metrics calls never crash the agent
        class _Noop:
            def __getattr__(self, _: str) -> Any:
                return lambda *a, **kw: None
        return _Noop()


# Strong refs to fire-and-forget persist tasks so they don't get GC'd
# before the DB write completes. Discard them opportunistically as
# they finish.
_BG_PERSIST_TASKS: set[asyncio.Task] = set()


def _persist_turn_bg(
    ctx: Any,
    messages: list[dict[str, Any]],
    turn: int,
    usage: Any,
    counter: dict[str, int],
    status: str = "active",
) -> None:
    """Fire-and-forget version of :func:`_persist_turn`.

    Schedules the DB write on the event loop and returns immediately.
    Used everywhere inside the turn so persistence never blocks the
    LLM/tool dispatch path. The agent-turn orchestration stays snappy;
    every accumulated message + checkpoint lands on disk on its own
    coroutine. Task references are retained in ``_BG_PERSIST_TASKS``
    so the asyncio GC can't silently cancel them mid-write.
    """
    try:
        # Snapshot the messages list so a later in-place mutation in
        # the agent loop doesn't alter what we're persisting. We pay
        # one list copy (~few dict refs) per fire — negligible.
        snapshot = list(messages)
        task = asyncio.create_task(
            _persist_turn(ctx, snapshot, turn, usage, counter, status=status),
        )
        _BG_PERSIST_TASKS.add(task)
        task.add_done_callback(_BG_PERSIST_TASKS.discard)
    except RuntimeError:
        # No running loop (test harness / shutdown) — fall back to a
        # best-effort synchronous no-op. Data loss here is impossible
        # in the normal daemon path.
        logger.debug("persist_turn_bg: no running loop, skipping")


async def _persist_turn(
    ctx: Any,
    messages: list[dict[str, Any]],
    turn: int,
    usage: Any,
    counter: dict[str, int],
    status: str = "active",
) -> None:
    """Persist session messages and checkpoint after each turn.

    Silently skips if the database is not initialized (standalone mode).
    """
    try:
        from digitorn.core.database import _engine
        if _engine is None:
            return

        from digitorn.core.runtime.persistence import SessionPersister

        app_id = getattr(ctx, "app_id", "") or "default"
        session_id = getattr(ctx, "session_id", "") or "default"
        agent_id = getattr(ctx, "agent_id", "") or "main"
        # Capture the session owner so the DB row is attributable.
        # Without it, user_sessions.user_id stays NULL and the row
        # becomes un-joinable on the users table — breaks "who did
        # this" queries and the rebuild-on-cache-miss path.
        user_id = getattr(ctx, "user_id", "") or ""
        if not user_id:
            sess = getattr(ctx, "session", None)
            user_id = getattr(sess, "user_id", "") if sess is not None else ""

        persister = SessionPersister(
            app_id, session_id, agent_id, user_id=user_id or None,
        )

        # Commit-on-first-success gate: only create the UserSession row
        # when the turn reaches status="completed". Intermediate
        # per-tool-call persistence during the first turn is skipped
        # silently so a failing first response leaves no trace in DB.
        # Subsequent turns persist normally because the row exists.
        _commit_now = (status == "completed")

        await persister.save_messages(messages, create_if_missing=_commit_now)

        # Capture memory snapshot
        memory_snap = None
        mem_module = getattr(ctx, "memory_module", None)
        if mem_module and hasattr(mem_module, "store") and mem_module.store:
            try:
                memory_snap = mem_module.store.to_dict()
            except Exception:
                pass

        # Capture metrics snapshot for analytics
        metrics_snap = None
        try:
            sm = _get_session_metrics(ctx)
            if hasattr(sm, "snapshot"):
                metrics_snap = sm.snapshot()
        except Exception:
            pass

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

        # Semantic title generation — only on first successful turn.
        # Fire-and-forget so response latency is untouched; silent on failure.
        if _commit_now and turn == 0:
            try:
                from digitorn.core.runtime.title_generator import maybe_update_session_title
                sess = getattr(ctx, "session", None)
                store = getattr(ctx, "session_store", None)
                if sess is not None and not getattr(sess, "_title_semantic_generated", False):
                    # Mark eagerly to prevent duplicate concurrent triggers.
                    try:
                        sess._title_semantic_generated = True  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    asyncio.create_task(
                        maybe_update_session_title(ctx, sess, session_store=store)
                    )
            except Exception as exc:
                logger.debug("title_gen_dispatch_failed: %s", exc)
    except Exception as exc:
        # RT21: log persistence failures at WARNING (was DEBUG) so operators
        # can see when sessions are silently not checkpointed. A persistence
        # failure means session state is lost on next crash.
        # EXCEPTION: shutdown noise (Ctrl+C while a turn is mid-persist)
        # is downgraded to DEBUG. The event loop is closing anyway,
        # the failure is intrinsic to the shutdown path, and warning
        # about it on every clean stop just trains operators to ignore
        # the warning channel. The persist will retry on next boot
        # via the rehydrate-on-boot recovery path.
        _msg = str(exc)
        _is_shutdown_noise = (
            "cannot schedule new futures after shutdown" in _msg
            or "Event loop is closed" in _msg
            or isinstance(exc, asyncio.CancelledError)
        )
        if _is_shutdown_noise:
            logger.debug("persist_turn_skipped_during_shutdown: %s", exc)
        else:
            logger.warning("persist_turn_failed: %s", exc, exc_info=True)


__all__ = [
    "agent_turn",
    "_format_bg_task_notification",
    "_format_watcher_notification",
]

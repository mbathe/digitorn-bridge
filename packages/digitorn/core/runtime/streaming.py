"""LLM streaming - token-by-token chat with think-tag filtering."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from digitorn.core.runtime.callbacks import AgentTurnCallbacks

logger = logging.getLogger(__name__)


def _exact_count(provider: Any, text: str) -> int:
    if not text:
        return 0
    if provider is not None and hasattr(provider, "count_tokens"):
        try:
            return int(provider.count_tokens(text))
        except Exception:
            logger.debug("provider.count_tokens failed", exc_info=True)
    try:
        from digitorn.core.runtime.session_metrics import _count_tokens
        return _count_tokens(text)
    except Exception:
        return 0


def _fix_win_backslashes(s: str) -> str:
    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        fixed = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)
        return fixed


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_THINK_CLOSE_LEN = len(_THINK_CLOSE)

_INLINE_TOOL_MARKERS = (
    "<tool_call>",
    "tool_calls:",
    "tool_call:",
    "run_parallel(",
    "parallel_tool_use(",
    "parallel_tools(",
    "batch_tools(",
    "工具调用:",
    "工具调用 :",
    "工具呼叫:",
    "ツール呼び出し:",
    "herramientas:",
)
_INLINE_TOOL_MAX_MARKER_LEN = max(len(m) for m in _INLINE_TOOL_MARKERS)

_CONTENT_WRAPPER_RE = re.compile(r'^\s*content\s*:\s*"', re.IGNORECASE)


def _coerce_tool_arguments_fragment(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return ""
    return str(value)


async def _finalize_streaming_on_abort(ctx: Any, state: Any) -> None:
    if ctx is None or state is None:
        return
    seq = getattr(ctx, "_streaming_assistant_seq", None)
    if not isinstance(seq, int) or seq < 0:
        return
    app_id = getattr(ctx, "app_id", None)
    session_id = getattr(ctx, "session_id", None)
    if not app_id or not session_id:
        return
    user_id = getattr(ctx, "user_id", "") or ""
    if not user_id:
        sess = getattr(ctx, "session", None)
        user_id = getattr(sess, "user_id", "") if sess is not None else ""
    try:
        parts = getattr(state, "content_parts", []) or []
        partial = "".join(parts)
        partial = re.sub(r"<think>.*?</think>", "", partial, flags=re.DOTALL)
        if not partial:
            return
        from digitorn.core.runtime.persistence import SessionPersister
        _workspace = getattr(sess, "workspace", "") if sess else ""
        _workdir = getattr(sess, "workdir", "") if sess else ""

        async def _do_finalize() -> None:
            persister = SessionPersister(
                app_id, session_id,
                getattr(ctx, "agent_id", "main"),
                user_id=user_id or None,
                workspace=_workspace,
                workdir=_workdir,
            )
            await persister.upsert_streaming_assistant(
                seq=seq,
                content=partial,
                status="complete",
                create_if_missing=True,
            )

        try:
            from digitorn.core.runtime.persist_worker import get_default_worker
            get_default_worker().submit(_do_finalize)
        except Exception as exc:
            logger.debug(
                "finalize_streaming_on_abort: worker submit failed (%s); falling back to inline await",
                exc,
            )
            await _do_finalize()
    except Exception as exc:
        logger.debug("finalize_streaming_on_abort failed: %s", exc)


_BG_STREAMING_PERSIST_TASKS: set[asyncio.Task] = set()

_BG_THINKING_CB_TASKS: set[asyncio.Task] = set()


def _schedule_streaming_persist(
    ctx: Any, content: str, *, status: str = "streaming",
) -> None:
    if ctx is None:
        return
    seq = getattr(ctx, "_streaming_assistant_seq", None)
    if not isinstance(seq, int) or seq < 0:
        return
    app_id = getattr(ctx, "app_id", None)
    session_id = getattr(ctx, "session_id", None)
    if not app_id or not session_id:
        return
    user_id = getattr(ctx, "user_id", "") or ""
    sess_obj = getattr(ctx, "session", None)
    if not user_id:
        user_id = getattr(sess_obj, "user_id", "") if sess_obj is not None else ""

    async def _run() -> None:
        try:
            from digitorn.core.runtime.persistence import SessionPersister
            persister = SessionPersister(
                app_id, session_id,
                getattr(ctx, "agent_id", "main"),
                user_id=user_id or None,
                workspace=getattr(sess_obj, "workspace", "") if sess_obj else "",
                workdir=getattr(sess_obj, "workdir", "") if sess_obj else "",
            )
            await persister.upsert_streaming_assistant(
                seq=seq,
                content=content,
                status=status,
                create_if_missing=(status == "complete"),
            )
        except Exception as exc:
            logger.debug("streaming_persist_scheduled_failed: %s", exc)

    submitted = False
    try:
        from digitorn.core.runtime.persist_worker import get_default_worker
        worker = get_default_worker()
        worker.submit(_run)
        submitted = True
    except Exception as exc:
        logger.debug("streaming_persist_worker_submit_failed: %s", exc)

    if not submitted:
        try:
            _t = asyncio.create_task(
                _run(), name=f"streaming-persist:{app_id}:{session_id}",
            )
            _BG_STREAMING_PERSIST_TASKS.add(_t)
            _t.add_done_callback(_BG_STREAMING_PERSIST_TASKS.discard)
        except RuntimeError:
            pass


async def streaming_chat(
    provider: Any,
    messages: list[dict],
    tools: list[dict] | None,
    generation_params: dict,
    cb: AgentTurnCallbacks,
    ctx: Any = None,
) -> tuple[str, list[dict], Any]:
    state = _StreamState(cb, ctx=ctx)
    state.input_messages = messages
    state.input_tools = tools

    _stream_completed = False
    try:
        async for chunk in provider.chat_stream(messages, tools=tools, **generation_params):
            await state.process_chunk(chunk)
        await state.flush()
        _stream_completed = True
    finally:
        if not _stream_completed:
            try:
                await _finalize_streaming_on_abort(ctx, state)
            except Exception:
                logger.debug("finalize_streaming_in_finally raised", exc_info=True)

    content = re.sub(r"<think>.*?</think>\s*", "", "".join(state.content_parts), flags=re.DOTALL)
    tool_calls = _finalize_tool_calls(state)

    _reasoning_parts = getattr(state, "_reasoning_full", None) or []
    _saw_thinking = bool(getattr(state, "_was_in_native_thinking", False))
    if _reasoning_parts:
        native_reasoning = "".join(_reasoning_parts)
    elif _saw_thinking:
        native_reasoning = ""
    else:
        native_reasoning = None

    class _FakeResponse:
        pass

    fake = _FakeResponse()
    fake.raw = {}
    fake.usage = state.last_usage
    fake.finish_reason = state.finish_reason
    fake.stop_reason = state.finish_reason
    fake.reasoning_content = native_reasoning
    return content, tool_calls, fake


class _StreamState:

    __slots__ = (
        "cb", "ctx", "content_parts", "tool_calls", "last_usage", "finish_reason",
        "_current_tool", "_tool_args_buf", "_tool_acc",
        "_stream_done_fired", "_think_buf", "_in_think", "_think_content",
        "_in_native_thinking", "_was_in_native_thinking", "_reasoning_full",
        "_prev_completion_tokens", "_last_snapshot_at",
        "_last_live_count_at", "_provider_streams_usage",
        "_prev_thinking_tokens", "_last_thinking_count_at",
        "_streaming_tool_calls",
        "_inline_tool_gate", "_inline_tool_hold",
        "_content_wrapper_active", "_content_wrapper_checked",
        "input_messages", "input_tools",
    )

    def __init__(self, cb: AgentTurnCallbacks, ctx: Any = None) -> None:
        self.cb = cb
        self.ctx = ctx
        self.content_parts: list[str] = []
        self.tool_calls: list[dict] = []
        self.last_usage: Any = None
        self.finish_reason: str | None = None
        self._current_tool: dict[str, Any] | None = None
        self._tool_args_buf: list[str] = []
        self._tool_acc: list[dict[str, Any]] = []
        self._stream_done_fired = False
        self._think_buf = ""
        # `_in_think` tracks <think> text-tag state; `_in_native_thinking` tracks provider chunk.thinking - mixing them pollutes thinking snapshots.
        self._in_think = False
        self._in_native_thinking = False
        # sticky bit - distinguishes "model never emitted reasoning" from "thinking-mode emitted nothing"; V4 requires the field on every assistant turn.
        self._was_in_native_thinking = False
        self._think_content: list[str] = []
        # full reasoning is never cleared - DeepSeek V4 requires replay; _think_content clears on flush.
        self._reasoning_full: list[str] = []
        self._inline_tool_gate = False
        self._inline_tool_hold = ""
        self._content_wrapper_active = False
        self._content_wrapper_checked = False
        self._prev_completion_tokens: int = 0
        self._last_snapshot_at: float = 0.0
        self._last_live_count_at: float = 0.0
        self._provider_streams_usage: bool = False
        self._prev_thinking_tokens: int = 0
        self._last_thinking_count_at: float = 0.0
        self._streaming_tool_calls: dict[str, dict[str, Any]] = {}
        self.input_messages: list[dict] | None = None
        self.input_tools: list[dict] | None = None

    async def process_chunk(self, chunk: Any) -> None:
        delta = getattr(chunk, "delta", "")
        finish = getattr(chunk, "finish_reason", None)
        tool_call_delta = getattr(chunk, "tool_call", None)
        chunk_tool_calls = getattr(chunk, "tool_calls", None)
        chunk_thinking = getattr(chunk, "thinking", None)

        self._track_usage(chunk)

        if chunk_thinking is not None:
            self._handle_native_thinking(chunk_thinking)

        if delta and self._in_native_thinking:
            await self._flush_native_thinking(delta)

        if finish:
            self.finish_reason = finish
            logger.info("stream_chunk finish=%s tool_calls=%s", finish, bool(chunk_tool_calls))
            await self._flush_native_thinking(delta)

        if delta:
            self.content_parts.append(delta)
            visible = await self._filter_think_tags(delta)
            if visible:
                visible = self._filter_inline_tool_markers(visible)
            if visible:
                import time as _time
                now = _time.monotonic()
                if now - self._last_live_count_at >= 0.25:
                    self._last_live_count_at = now
                    self._maybe_emit_live_out_count()
                await _fire_token(self.cb, visible, self._prev_completion_tokens)
                if now - self._last_snapshot_at >= 0.5:
                    self._last_snapshot_at = now
                    try:
                        await self._fire_assistant_snapshot()
                    except Exception as exc:
                        logger.debug("assistant_snapshot_fire_failed: %s", exc)

        if (chunk_tool_calls or tool_call_delta) and not self._stream_done_fired:
            self._fire_stream_done()

        if chunk_tool_calls:
            self._accumulate_tool_calls(chunk_tool_calls)

        if tool_call_delta:
            self._accumulate_tool_delta(tool_call_delta)

        if finish == "tool_calls" and self._current_tool is not None:
            self._current_tool["function"]["arguments"] = "".join(self._tool_args_buf)
            self.tool_calls.append(self._current_tool)
            self._current_tool = None
            self._tool_args_buf = []
            self._finalize_all_pending_streams()

    async def flush(self) -> None:
        if self._current_tool is not None:
            self._current_tool["function"]["arguments"] = "".join(self._tool_args_buf)
            self.tool_calls.append(self._current_tool)
        self._finalize_all_pending_streams()

        if self._in_native_thinking and self._think_content:
            text = "".join(self._think_content).strip()
            if text:
                try:
                    await emit_thinking(self.cb.on_thinking, text)
                except Exception:
                    logger.debug("flush native_thinking emit failed", exc_info=True)
            self._think_content.clear()
            self._in_native_thinking = False

        if self._think_buf and not self._in_think:
            try:
                if self.cb.on_token is not None:
                    self.cb.on_token(self._think_buf, self._prev_completion_tokens)
            except Exception:
                logger.debug("on_token callback error (finalize)", exc_info=True)

        _reported_prompt = (
            getattr(self.last_usage, "prompt_tokens", 0) or 0
            if self.last_usage is not None else 0
        )
        _reported_completion = (
            getattr(self.last_usage, "completion_tokens", 0) or 0
            if self.last_usage is not None else 0
        )
        _need_fallback = (
            self.last_usage is None
            or _reported_prompt == 0
            or _reported_completion == 0
        )
        if _need_fallback and (self.content_parts or self.tool_calls):
            from digitorn.modules.llm_provider.providers.base import TokenUsage
            import asyncio as _asyncio
            import json as _json
            text = "".join(self.content_parts)
            provider = getattr(self.ctx, "provider", None) if self.ctx else None

            input_messages = list(self.input_messages or [])
            input_tools = list(self.input_tools or [])

            def _count_all() -> tuple[int, int]:
                _out = max(_exact_count(provider, text), 1)
                _in = 0
                if input_messages:
                    try:
                        def _field(obj, name, default=""):
                            if isinstance(obj, dict):
                                return obj.get(name, default)
                            return getattr(obj, name, default)

                        msg_dicts: list[dict[str, Any]] = []
                        for m in input_messages:
                            c = _field(m, "content", "")
                            if isinstance(c, list):
                                _parts = []
                                for part in c:
                                    if isinstance(part, dict):
                                        _parts.append(part.get("text", "") or "")
                                    elif isinstance(part, str):
                                        _parts.append(part)
                                c = " ".join(t for t in _parts if t)
                            elif not isinstance(c, str):
                                c = str(c) if c is not None else ""
                            msg_dicts.append({
                                "role": str(_field(m, "role", "user")),
                                "content": c,
                            })
                        if provider is not None and hasattr(
                            provider, "count_message_tokens",
                        ):
                            _in += int(
                                provider.count_message_tokens(msg_dicts),
                            )
                        else:
                            for d in msg_dicts:
                                _in += _exact_count(provider, d["content"])
                            _in += 4 * len(msg_dicts)
                    except Exception:
                        logger.debug("prompt count: message walk failed", exc_info=True)
                if input_tools:
                    try:
                        tools_text = _json.dumps(
                            input_tools, ensure_ascii=False, default=str,
                        )
                        _in += _exact_count(provider, tools_text)
                        _in += 4 * len(input_tools)
                    except Exception:
                        logger.debug("prompt count: tools serialize failed", exc_info=True)
                return _in, _out

            estimated_in, estimated_out = await _asyncio.to_thread(_count_all)

            final_prompt = _reported_prompt if _reported_prompt > 0 else estimated_in
            final_completion = _reported_completion if _reported_completion > 0 else estimated_out
            self.last_usage = TokenUsage(
                prompt_tokens=final_prompt,
                completion_tokens=final_completion,
                total_tokens=final_prompt + final_completion,
            )
            if _reported_completion == 0 and self.cb.on_out_token is not None:
                remaining = estimated_out - self._prev_completion_tokens
                if remaining > 0:
                    try:
                        self.cb.on_out_token(remaining)
                        self._prev_completion_tokens = estimated_out
                    except Exception:
                        logger.debug("on_out_token callback error (estimate)", exc_info=True)
            if _reported_prompt == 0 and self.cb.on_in_token is not None:
                try:
                    self.cb.on_in_token(estimated_in)
                except Exception:
                    logger.debug("on_in_token callback error (estimate)", exc_info=True)
            logger.info(
                "usage_estimated prompt=%d (reported=%d) completion=%d (reported=%d)",
                final_prompt, _reported_prompt, final_completion, _reported_completion,
            )

        self._fire_stream_done()

    def _track_usage(self, chunk: Any) -> None:
        usage = getattr(chunk, "usage", None)
        if usage is None:
            return
        self.last_usage = usage
        ct = getattr(usage, "completion_tokens", 0) or 0
        if ct > 0 and self.cb.on_out_token is not None:
            self._provider_streams_usage = True
            delta = ct - self._prev_completion_tokens
            self._prev_completion_tokens = ct
            if delta > 0:
                try:
                    self.cb.on_out_token(delta)
                except Exception:
                    logger.debug("on_out_token callback error", exc_info=True)

    def _fire_tool_call_streaming(
        self,
        call_id: str,
        name: str,
        args_fragment: str = "",
        force: bool = False,
    ) -> None:
        cb = self.cb.on_tool_call_streaming
        if cb is None or not call_id:
            return
        entry = self._streaming_tool_calls.get(call_id)
        if entry is None:
            entry = {
                "name": name or "",
                "args_text": "",
                "count": 0,
                "last_at": 0.0,
                "intent": "",
            }
            self._streaming_tool_calls[call_id] = entry
        if name and not entry["name"]:
            entry["name"] = name
        if args_fragment:
            entry["args_text"] += args_fragment

        import time as _time
        now = _time.monotonic()
        if not force and now - entry["last_at"] < 0.25:
            return
        entry["last_at"] = now

        if entry["args_text"]:
            provider = getattr(self.ctx, "provider", None) if self.ctx else None
            try:
                count = _exact_count(provider, entry["args_text"])
            except Exception:
                count = entry["count"]
        else:
            count = 0
        if count < entry["count"]:
            count = entry["count"]
        entry["count"] = count

        if not entry["intent"]:
            captured = _scan_intent_value(entry["args_text"])
            if captured:
                entry["intent"] = captured
        try:
            cb(call_id, entry["name"], count, entry["intent"])
        except TypeError:
            try:
                cb(call_id, entry["name"], count)
            except Exception:
                logger.debug("on_tool_call_streaming callback error", exc_info=True)
        except Exception:
            logger.debug("on_tool_call_streaming callback error", exc_info=True)

    def _finalize_tool_call_streaming(self, call_id: str) -> None:
        if call_id and call_id in self._streaming_tool_calls:
            entry = self._streaming_tool_calls.get(call_id) or {}
            if not entry.get("intent"):
                fallback = _default_intent_for_tool(entry.get("name", ""))
                if fallback:
                    entry["intent"] = fallback
            self._fire_tool_call_streaming(call_id, "", force=True)
            self._streaming_tool_calls.pop(call_id, None)

    def _finalize_all_pending_streams(self) -> None:
        pending = list(self._streaming_tool_calls.keys())
        if pending:
            logger.info(
                "stream_finalize_sweep n=%d ids=%s",
                len(pending),
                [k[:12] for k in pending],
            )
        for call_id in pending:
            self._fire_tool_call_streaming(call_id, "", force=True)
        self._streaming_tool_calls.clear()

    def _thinking_token_count(self) -> int:
        if not self._think_content:
            return 0
        provider = getattr(self.ctx, "provider", None) if self.ctx else None
        try:
            text = "".join(self._think_content)
            return _exact_count(provider, text)
        except Exception:
            logger.debug("thinking token count failed", exc_info=True)
            return self._prev_thinking_tokens

    def _maybe_emit_live_out_count(self) -> None:
        """Emit incremental `out_token` deltas during streaming using"""
        if self.cb.on_out_token is None:
            return
        if self._provider_streams_usage:
            return
        if not self.content_parts:
            return
        provider = getattr(self.ctx, "provider", None) if self.ctx else None
        try:
            text = "".join(self.content_parts)
            count = _exact_count(provider, text)
        except Exception:
            logger.debug("live out_token count failed", exc_info=True)
            return
        if count <= self._prev_completion_tokens:
            return
        delta = count - self._prev_completion_tokens
        self._prev_completion_tokens = count
        try:
            self.cb.on_out_token(delta)
        except Exception:
            logger.debug("on_out_token callback error (live)", exc_info=True)

    def _handle_native_thinking(self, text: str) -> None:
        import asyncio as _asyncio
        import inspect as _inspect

        def _fire(cb: Any, *args: Any) -> None:
            if cb is None:
                return
            attempts = list(range(len(args), -1, -1))
            for n in attempts:
                try:
                    res = cb(*args[:n])
                    if _inspect.iscoroutine(res):
                        try:
                            loop = _asyncio.get_running_loop()
                            _t = loop.create_task(res)
                            _BG_THINKING_CB_TASKS.add(_t)
                            _t.add_done_callback(_BG_THINKING_CB_TASKS.discard)
                        except RuntimeError:
                            res.close()
                    return
                except TypeError:
                    if n == 0:
                        logger.debug("native_thinking callback signature mismatch", exc_info=True)
                        return
                    continue
                except Exception:
                    logger.debug("native_thinking callback error", exc_info=True)
                    return

        if not self._in_native_thinking:
            self._in_native_thinking = True
            self._was_in_native_thinking = True
            self._prev_thinking_tokens = 0
            self._last_thinking_count_at = 0.0
            _fire(self.cb.on_thinking_started)
        self._think_content.append(text)
        self._reasoning_full.append(text)
        import time as _time
        _now = _time.monotonic()
        if _now - self._last_thinking_count_at >= 0.25:
            self._last_thinking_count_at = _now
            cnt = self._thinking_token_count()
            if cnt > self._prev_thinking_tokens:
                self._prev_thinking_tokens = cnt
        _fire(self.cb.on_thinking_delta, text, self._prev_thinking_tokens)

    async def _flush_native_thinking(self, delta: str) -> None:
        if self._in_native_thinking and self._think_content:
            cnt = self._thinking_token_count()
            if cnt > self._prev_thinking_tokens:
                self._prev_thinking_tokens = cnt
            text = "".join(self._think_content).strip()
            if text:
                await emit_thinking(
                    self.cb.on_thinking, text, self._prev_thinking_tokens,
                )
            self._think_content.clear()
            self._prev_thinking_tokens = 0
            self._last_thinking_count_at = 0.0
            self._in_native_thinking = False
            self._in_think = False

    async def _fire_assistant_snapshot(self) -> None:
        ctx = self.ctx
        if ctx is None:
            return
        text = "".join(self.content_parts)
        import re as _re
        visible_only = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL)
        _schedule_streaming_persist(ctx, visible_only, status="streaming")
        await asyncio.sleep(0)

    def _filter_inline_tool_markers(self, visible: str) -> str:
        if self._inline_tool_gate:
            return ""

        if not self._content_wrapper_checked:
            combined_probe = self._inline_tool_hold + visible
            if len(combined_probe.lstrip()) >= len('content: "') or "\n" in combined_probe:
                self._content_wrapper_checked = True
                m = _CONTENT_WRAPPER_RE.match(combined_probe)
                if m:
                    self._content_wrapper_active = True
                    after = combined_probe[m.end():]
                    self._inline_tool_hold = ""
                    visible = after
                else:
                    pass
            else:
                self._inline_tool_hold = combined_probe
                return ""

        buf = self._inline_tool_hold + visible
        self._inline_tool_hold = ""

        low = buf.lower()
        earliest = len(buf)
        for marker in _INLINE_TOOL_MARKERS:
            idx = low.find(marker.lower())
            if idx != -1 and idx < earliest:
                earliest = idx

        if earliest < len(buf):
            self._inline_tool_gate = True
            out = buf[:earliest]
            if self._content_wrapper_active:
                out = out.rstrip()
                if out.endswith('"'):
                    out = out[:-1].rstrip()
            return out

        tail_len = min(_INLINE_TOOL_MAX_MARKER_LEN - 1, len(buf))
        tail = buf[-tail_len:] if tail_len > 0 else ""
        tail_low = tail.lower()
        held = ""
        for k in range(tail_len, 0, -1):
            suffix = tail_low[-k:]
            if any(m.lower().startswith(suffix) for m in _INLINE_TOOL_MARKERS):
                held = tail[-k:]
                break

        if self._content_wrapper_active and not held:
            stripped_tail = buf.rstrip()
            if stripped_tail.endswith('"'):
                quote_idx = buf.rfind('"')
                held = buf[quote_idx:]

        if held:
            self._inline_tool_hold = held
            return buf[: len(buf) - len(held)]
        return buf

    async def _filter_think_tags(self, delta: str) -> str:
        self._think_buf += delta
        visible = ""

        while self._think_buf:
            if self._in_think:
                visible_part = await self._consume_think_close()
                if visible_part is not None:
                    visible += visible_part
                    continue
                self._buffer_think_content()
                break
            else:
                result = self._consume_think_open()
                if result is None:
                    break
                visible += result

        return visible

    def _fire_thinking_delta(self, chunk: str) -> None:
        if not chunk or self.cb.on_thinking_delta is None:
            return
        cnt = self._thinking_token_count()
        if cnt > self._prev_thinking_tokens:
            self._prev_thinking_tokens = cnt
        try:
            try:
                self.cb.on_thinking_delta(chunk, self._prev_thinking_tokens)
            except TypeError:
                self.cb.on_thinking_delta(chunk)
        except Exception:
            logger.debug("on_thinking_delta callback error", exc_info=True)

    async def _consume_think_close(self) -> str | None:
        end_idx = self._think_buf.find(_THINK_CLOSE)
        if end_idx == -1:
            return None
        final_chunk = self._think_buf[:end_idx]
        if final_chunk:
            self._think_content.append(final_chunk)
            self._fire_thinking_delta(final_chunk)
        text = "".join(self._think_content).strip()
        if text:
            await emit_thinking(self.cb.on_thinking, text, self._prev_thinking_tokens)
        self._think_content.clear()
        self._prev_thinking_tokens = 0
        self._last_thinking_count_at = 0.0
        self._think_buf = self._think_buf[end_idx + _THINK_CLOSE_LEN:].lstrip()
        self._in_think = False
        return ""

    def _buffer_think_content(self) -> None:
        if len(self._think_buf) >= _THINK_CLOSE_LEN:
            chunk = self._think_buf[:-(_THINK_CLOSE_LEN - 1)]
            self._think_content.append(chunk)
            self._fire_thinking_delta(chunk)
            self._think_buf = self._think_buf[-(_THINK_CLOSE_LEN - 1):]

    def _consume_think_open(self) -> str | None:
        start_idx = self._think_buf.find(_THINK_OPEN)
        if start_idx != -1:
            before = self._think_buf[:start_idx]
            self._think_buf = self._think_buf[start_idx + len(_THINK_OPEN):]
            self._in_think = True
            self._prev_thinking_tokens = 0
            self._last_thinking_count_at = 0.0
            if self.cb.on_thinking_started is not None:
                try:
                    self.cb.on_thinking_started()
                except Exception:
                    logger.debug("on_thinking_started callback error (open)", exc_info=True)
            return before

        tag = _THINK_OPEN
        for k in range(1, min(len(tag), len(self._think_buf) + 1)):
            if self._think_buf.endswith(tag[:k]):
                if k >= len(self._think_buf):
                    return None
                before = self._think_buf[:-k]
                self._think_buf = self._think_buf[-k:]
                return before

        before = self._think_buf
        self._think_buf = ""
        return before

    def _fire_stream_done(self) -> None:
        if self._stream_done_fired or self.cb.on_stream_done is None:
            return
        self._stream_done_fired = True
        try:
            self.cb.on_stream_done()
        except Exception:
            logger.debug("on_stream_done callback error", exc_info=True)

    def _accumulate_tool_calls(self, chunk_tool_calls: list[dict]) -> None:
        for tc in chunk_tool_calls:
            idx = tc.get("index", 0)
            while len(self._tool_acc) <= idx:
                self._tool_acc.append({"id": "", "name": "", "args_parts": []})
            entry = self._tool_acc[idx]
            new_name = tc.get("name") and not entry["name"]
            if tc.get("id"):
                entry["id"] = tc["id"]
            if tc.get("name"):
                entry["name"] = tc["name"]
            arg_frag = ""
            if tc.get("arguments"):
                arg_frag = _coerce_tool_arguments_fragment(tc["arguments"])
                entry["args_parts"].append(arg_frag)
            call_id = entry.get("id") or ""
            if call_id and (new_name or arg_frag):
                self._fire_tool_call_streaming(
                    call_id, entry["name"], arg_frag, force=bool(new_name),
                )

    def _accumulate_tool_delta(self, tc: dict) -> None:
        new_name = False
        if tc.get("name"):
            if self._current_tool is not None:
                prev_id = self._current_tool.get("id") or ""
                self._current_tool["function"]["arguments"] = "".join(self._tool_args_buf)
                self.tool_calls.append(self._current_tool)
                self._tool_args_buf = []
                self._finalize_tool_call_streaming(prev_id)
            self._current_tool = {
                "id": tc.get("id", f"call_{len(self.tool_calls)}"),
                "type": "function",
                "function": {"name": tc["name"], "arguments": ""},
            }
            new_name = True
        arg_frag = ""
        if tc.get("arguments"):
            arg_frag = _coerce_tool_arguments_fragment(tc["arguments"])
            self._tool_args_buf.append(arg_frag)
        if self._current_tool is not None and (new_name or arg_frag):
            call_id = self._current_tool.get("id") or ""
            name = self._current_tool["function"]["name"]
            self._fire_tool_call_streaming(
                call_id, name, arg_frag, force=new_name,
            )


_INTENT_RX = __import__("re").compile(
    r'"intent"\s*:\s*"((?:[^"\\]|\\.)*)"',
)


def _scan_intent_value(args_text: str) -> str:
    if not args_text or '"intent"' not in args_text:
        return ""
    m = _INTENT_RX.search(args_text)
    if not m:
        return ""
    try:
        return __import__("json").loads(f'"{m.group(1)}"')
    except Exception:
        return m.group(1)


_FALLBACK_INTENT_BY_NAME: dict[str, str] = {
    "WsWrite": "Writing file",
    "workspace.write": "Writing file",
    "WsEdit": "Editing file",
    "workspace.edit": "Editing file",
    "WsRead": "Reading file",
    "workspace.read": "Reading file",
    "WsGlob": "Searching files",
    "workspace.glob": "Searching files",
    "WsGrep": "Searching code",
    "workspace.grep": "Searching code",
    "WsDelete": "Deleting file",
    "workspace.delete": "Deleting file",
    "Write": "Writing file",
    "Edit": "Editing file",
    "Read": "Reading file",
    "Glob": "Searching files",
    "Grep": "Searching code",
    "Delete": "Deleting file",
    "Bash": "Running command",
    "shell.bash": "Running command",
    "shell.background_run": "Running command",
    "background_run": "Running command",
    "WebFetch": "Fetching page",
    "web.fetch": "Fetching page",
    "WebSearch": "Searching the web",
    "web.search": "Searching the web",
    "web.extract": "Extracting page",
    "PreviewPublish": "Publishing preview",
    "preview.publish": "Publishing preview",
    "TaskCreate": "Creating task",
    "TaskUpdate": "Updating task",
    "Remember": "Remembering",
    "memory.task_create": "Creating task",
    "memory.task_update": "Updating task",
    "memory.remember": "Remembering",
    "memory.set_goal": "Setting goal",
    "SearchTools": "Searching tools",
    "GetTool": "Reading tool info",
    "ExecuteTool": "Executing tool",
    "Agent": "Spawning agent",
    "agent_spawn.agent": "Spawning agent",
}


def _default_intent_for_tool(name: str) -> str:
    if not name:
        return ""
    if name in _FALLBACK_INTENT_BY_NAME:
        return _FALLBACK_INTENT_BY_NAME[name]
    short = name.split(".", 1)[-1] if "." in name else name
    return f"Running {short}"


def _recover_partial_json(args_str: str, tool_name: str) -> dict:
    import re
    result = {}

    fixed = args_str.rstrip()
    for suffix in ['"}', '"}}}', '"}}', '"}]', '}']:
        try:
            parsed = json.loads(fixed + suffix)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    truncated_keys: list[str] = []
    for m in re.finditer(r'"(\w+)"\s*:\s*"((?:[^"\\]|\\.)*)("|$)', args_str):
        key, val, closer = m.group(1), m.group(2), m.group(3)
        try:
            result[key] = json.loads(f'"{val}"')
        except Exception:
            result[key] = val
        if closer != '"':
            truncated_keys.append(key)

    for m in re.finditer(r'"(\w+)"\s*:\s*(true|false|null|\d+(?:\.\d+)?)\b', args_str):
        key, val = m.group(1), m.group(2)
        if val == "true":
            result[key] = True
        elif val == "false":
            result[key] = False
        elif val == "null":
            result[key] = None
        elif "." in val:
            result[key] = float(val)
        else:
            result[key] = int(val)

    if result:
        logger.warning(
            "Recovered %d params from truncated JSON for %s (truncated_keys=%s)",
            len(result), tool_name, truncated_keys,
        )
        if truncated_keys:
            result["__truncated_keys"] = truncated_keys
    return result


def _finalize_tool_calls(state: _StreamState) -> list[dict]:
    tool_calls = list(state.tool_calls)

    for entry in state._tool_acc:
        if not entry["name"]:
            continue
        args_str = "".join(entry["args_parts"])
        parsed = {}
        if args_str.strip():
            try:
                parsed = json.loads(_fix_win_backslashes(args_str))
            except json.JSONDecodeError:
                parsed = _recover_partial_json(args_str, entry["name"])
        import uuid as _uuid
        final_id = entry["id"] or f"call_{_uuid.uuid4().hex[:12]}"
        tool_calls.append({
            "id": final_id,
            "type": "function",
            "function": {"name": entry["name"], "arguments": parsed},
        })
        state._finalize_tool_call_streaming(final_id)

    for tc in tool_calls:
        args = tc["function"].get("arguments", "")
        if isinstance(args, str) and args.strip():
            try:
                tc["function"]["arguments"] = json.loads(_fix_win_backslashes(args))
            except json.JSONDecodeError:
                tool_name = tc["function"].get("name", "")
                recovered = _recover_partial_json(args, tool_name)
                tc["function"]["arguments"] = recovered if recovered else {}

    return tool_calls


async def emit_thinking(on_thinking: Any, text: str, count: int = 0) -> None:
    if on_thinking is None or not text or not text.strip():
        return
    clean = text.strip()
    if clean.startswith(("{", "[")) and any(
        k in clean for k in ('"type":', '"properties":', '"required":')
    ):
        return
    try:
        try:
            result = on_thinking(clean, count)
        except TypeError:
            result = on_thinking(clean)
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:
        logger.debug("on_thinking callback error: %s", exc, exc_info=True)


async def _fire_token(cb: AgentTurnCallbacks, text: str, count: int = 0) -> None:
    if cb.on_token is None:
        return
    try:
        if asyncio.iscoroutinefunction(cb.on_token):
            try:
                await cb.on_token(text, count)
            except TypeError:
                await cb.on_token(text)
        else:
            try:
                cb.on_token(text, count)
            except TypeError:
                cb.on_token(text)
    except Exception as exc:
        logger.debug("on_token callback error: %s", exc, exc_info=True)

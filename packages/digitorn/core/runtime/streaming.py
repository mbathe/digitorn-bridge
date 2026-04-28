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
    """Return the exact token count of ``text`` for ``provider.model``
    via litellm. Used as the ground-truth fallback when the LLM stream
    didn't report a ``usage`` block (Ollama, some OpenAI-compat APIs,
    older Anthropic streams). Routes through ``BaseLLMProvider.count_tokens``
    which loads the right local tokenizer for the configured model -
    no estimation, no heuristic. Returns 0 only on truly empty input
    or a catastrophic failure (which the caller treats as no info)."""
    if not text:
        return 0
    if provider is not None and hasattr(provider, "count_tokens"):
        try:
            return int(provider.count_tokens(text))
        except Exception:
            logger.debug("provider.count_tokens failed", exc_info=True)
    # Provider unavailable (sub-agent ctx without provider attached).
    # Last-resort: tiktoken cl100k_base via the shared helper. Better
    # than char/4 and avoids the daemon stalling on a missing import.
    try:
        from digitorn.core.runtime.session_metrics import _count_tokens
        return _count_tokens(text)
    except Exception:
        return 0


def _fix_win_backslashes(s: str) -> str:
    """Fix unescaped Windows backslashes in JSON strings from LLMs.

    LLMs often produce ``"C:\\Users\\..."`` with single backslashes that are
    invalid JSON escapes (``\\U``, ``\\D``, etc.).  We selectively double
    backslashes that are not already valid JSON escape sequences.
    """
    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        # Replace \ not followed by a valid JSON escape char with \\
        fixed = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)
        return fixed


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_THINK_CLOSE_LEN = len(_THINK_CLOSE)

# Markers emitted by models that output tool calls inline in the content
# (qwen2.5, some deepseek fine-tunes). When one of these appears in the
# stream, we stop forwarding tokens to the client so the raw JSON / tag
# noise never flashes on screen. The extract_tool pass later rescues
# the call from the full content.
#
# Includes CJK translations because qwen switches language based on
# recent context - a Chinese user gets `工具调用:` instead of `tool_call:`.
_INLINE_TOOL_MARKERS = (
    "<tool_call>",
    "tool_calls:",
    "tool_call:",
    "run_parallel(",      # qwen Python-call-syntax
    "parallel_tool_use(",
    "parallel_tools(",
    "batch_tools(",
    "工具调用:",           # Chinese (Simplified)
    "工具调用 :",          # Chinese with space
    "工具呼叫:",           # Chinese (Traditional)
    "ツール呼び出し:",     # Japanese
    "herramientas:",      # Spanish (less common but seen in wild)
)
_INLINE_TOOL_MAX_MARKER_LEN = max(len(m) for m in _INLINE_TOOL_MARKERS)

# Leading wrapper some models emit - `content: "..."` around the
# actual assistant reply. We strip this prefix (and its matching
# closing quote) so the user sees clean text.
_CONTENT_WRAPPER_RE = re.compile(r'^\s*content\s*:\s*"', re.IGNORECASE)


def _coerce_tool_arguments_fragment(value: Any) -> str:
    """Normalize a streamed tool-argument fragment to a JSON string.

    Most providers stream tool arguments as JSON text fragments, but some
    providers (notably Anthropic final tool blocks) may already provide parsed
    ``dict`` objects.  The runtime accumulator stores fragments uniformly as
    strings, so dict/list payloads are serialized here.
    """
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


def _schedule_streaming_persist(
    ctx: Any, content: str, *, status: str = "streaming",
) -> None:
    """Kick off a fire-and-forget UPSERT of the in-flight assistant
    message in ``history_log``. Caller keeps streaming without waiting
    for the DB round-trip. ``status='streaming'`` during the turn;
    the flush path at turn-end flips it to ``'complete'``.

    Requires ``ctx`` to carry ``app_id``, ``session_id`` and a
    ``_streaming_assistant_seq`` int set by the agent loop right
    before it calls ``streaming_chat`` (the seq where the new
    assistant message will land when the turn's ``messages`` list is
    persisted). When that seed is missing (e.g. sub-agent contexts)
    we silently skip - no durability guarantee, but also no crash.
    """
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
    if not user_id:
        sess = getattr(ctx, "session", None)
        user_id = getattr(sess, "user_id", "") if sess is not None else ""

    async def _run() -> None:
        try:
            from digitorn.core.runtime.persistence import SessionPersister
            persister = SessionPersister(
                app_id, session_id,
                getattr(ctx, "agent_id", "main"),
                user_id=user_id or None,
            )
            await persister.upsert_streaming_assistant(
                seq=seq,
                content=content,
                status=status,
                create_if_missing=(status == "complete"),
            )
        except Exception as exc:
            logger.debug("streaming_persist_scheduled_failed: %s", exc)

    try:
        asyncio.create_task(_run())
    except RuntimeError:
        # No running loop - degrade gracefully (unit tests, shutdown).
        pass


async def streaming_chat(
    provider: Any,
    messages: list[dict],
    tools: list[dict] | None,
    generation_params: dict,
    cb: AgentTurnCallbacks,
    ctx: Any = None,
) -> tuple[str, list[dict], Any]:
    """Stream a chat response and return (content, tool_calls, response).

    All UI callbacks are wrapped in try/except - a callback error must
    never crash the agent loop or interrupt the LLM stream.

    ``ctx`` - optional AgentContext; when passed, the stream emits
    ``assistant_stream_snapshot`` events every 500ms so mid-turn
    reconnects can rehydrate the partial assistant content from the
    durable ``session_events`` log.
    """
    state = _StreamState(cb, ctx=ctx)
    state.input_messages = messages
    state.input_tools = tools

    async for chunk in provider.chat_stream(messages, tools=tools, **generation_params):
        await state.process_chunk(chunk)

    await state.flush()

    content = re.sub(r"<think>.*?</think>\s*", "", "".join(state.content_parts), flags=re.DOTALL)
    tool_calls = _finalize_tool_calls(state)

    # DeepSeek V4 thinking mode: accumulated native reasoning must be
    # returned on the response so agent_loop can replay it on next turn.
    # `_reasoning_full` is the full stream (never cleared by flush).
    _reasoning_parts = getattr(state, "_reasoning_full", None) or []
    native_reasoning = "".join(_reasoning_parts) if _reasoning_parts else None

    class _FakeResponse:
        pass

    fake = _FakeResponse()
    fake.raw = {}
    fake.usage = state.last_usage
    fake.finish_reason = state.finish_reason
    fake.stop_reason = state.finish_reason  # Anthropic uses stop_reason
    fake.reasoning_content = native_reasoning
    return content, tool_calls, fake


class _StreamState:
    """Mutable state accumulated during a single stream."""

    __slots__ = (
        "cb", "ctx", "content_parts", "tool_calls", "last_usage", "finish_reason",
        "_current_tool", "_tool_args_buf", "_tool_acc",
        "_stream_done_fired", "_think_buf", "_in_think", "_think_content",
        "_in_native_thinking", "_reasoning_full",
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
        # `_in_think` tracks the TEXT-TAG thinking state (inside
        # `<think>…</think>`). `_in_native_thinking` tracks the provider
        # NATIVE thinking mode (chunk.thinking field). Mixing these
        # flags caused the daemon to classify normal content deltas as
        # "still inside thinking" right after a native-thinking burst,
        # producing thinking snapshots that contained the actual
        # response text appended with no delimiter.
        self._in_think = False
        self._in_native_thinking = False
        self._think_content: list[str] = []
        # Full reasoning accumulated across the entire stream (never cleared).
        # DeepSeek V4 thinking mode requires this to be replayed to the API
        # on the next turn, unlike _think_content which is cleared on flush.
        self._reasoning_full: list[str] = []
        # Once a marker like `tool_calls:` or `<tool_call>` is seen, we
        # stop forwarding any further visible content to the client so
        # the raw call noise never flashes on screen. The full content
        # is still accumulated in content_parts for extract_tool.
        self._inline_tool_gate = False
        # Rolling tail buffer - holds the last N chars that might be the
        # prefix of a marker (e.g. "<too" waiting for "l_call>").
        self._inline_tool_hold = ""
        # Track if the response starts with `content: "...` wrapper.
        # When active, we strip the prefix AND the matching trailing `"`
        # right before the tool-call marker.
        self._content_wrapper_active = False
        self._content_wrapper_checked = False
        self._prev_completion_tokens: int = 0
        self._last_snapshot_at: float = 0.0
        self._last_live_count_at: float = 0.0
        # Set once the provider sends a streaming usage chunk with a
        # real ``completion_tokens`` value - disables our litellm live
        # counter so the two sources don't fight over the cursor.
        self._provider_streams_usage: bool = False
        # Per-thinking-block cursor. Reset to 0 each time a thinking
        # block opens (`thinking_started` / `_in_think` flips True),
        # so each block carries its OWN tokenized count instead of the
        # message-wide cumulative. Same rolling-tokenize trick as the
        # text counter, scoped to ``self._think_content``.
        self._prev_thinking_tokens: int = 0
        self._last_thinking_count_at: float = 0.0
        # Per-tool live progress while the LLM streams the args JSON.
        # Keyed by call_id; each entry tracks the accumulated args
        # text, last emitted token count, and last emit timestamp so
        # we can throttle the litellm tokenize to 250ms regardless of
        # how fast args fragments arrive. Emptied as each call is
        # finalized - the existing tool_start / tool_call lifecycle
        # takes over from there.
        self._streaming_tool_calls: dict[str, dict[str, Any]] = {}
        # Captured by streaming_chat() so the flush-time fallback can
        # estimate prompt_tokens for providers that don't send usage
        # (Ollama, DeepSeek without include_usage, older llama.cpp).
        self.input_messages: list[dict] | None = None
        self.input_tools: list[dict] | None = None

    async def process_chunk(self, chunk: Any) -> None:
        delta = getattr(chunk, "delta", "")
        finish = getattr(chunk, "finish_reason", None)
        tool_call_delta = getattr(chunk, "tool_call", None)
        chunk_tool_calls = getattr(chunk, "tool_calls", None)
        chunk_thinking = getattr(chunk, "thinking", None)

        self._track_usage(chunk)

        if chunk_thinking:
            self._handle_native_thinking(chunk_thinking)

        # If a content delta arrives while native thinking is active,
        # that signals the model exited thinking - flush the accumulated
        # thinking block BEFORE routing the delta to content. Without
        # this, the text-tag filter (`_filter_think_tags`) saw delta
        # while `_in_think` was True (set by text-tag parser) OR the
        # native-thinking buffer kept growing with response text
        # appended, producing a polluted `thinking` snapshot at flush.
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
                # Refresh the live completion-token count BEFORE
                # firing the token event so the count we attach is
                # accurate at the moment the delta lands. Throttled
                # to 250ms - litellm is local but still O(n) on the
                # accumulated text.
                import time as _time
                now = _time.monotonic()
                if now - self._last_live_count_at >= 0.25:
                    self._last_live_count_at = now
                    self._maybe_emit_live_out_count()
                await _fire_token(self.cb, visible, self._prev_completion_tokens)
                # Mid-stream snapshot for reconnect durability: every
                # 500ms emit + persist the accumulated content so late
                # joiners see what the assistant has written so far
                # (tokens themselves are ephemeral by design).
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
            self._finalize_tool_call_streaming(self._current_tool.get("id") or "")
            self._current_tool = None
            self._tool_args_buf = []

    async def flush(self) -> None:
        if self._current_tool is not None:
            self._current_tool["function"]["arguments"] = "".join(self._tool_args_buf)
            self.tool_calls.append(self._current_tool)
            self._finalize_tool_call_streaming(self._current_tool.get("id") or "")

        # If the stream ends while still in native-thinking mode (no
        # content ever followed), emit the accumulated thinking now.
        # Without this, a short model reply where the whole answer fit
        # inside native thinking would never produce a `thinking` event.
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

        # Fallback: if the provider never reported usage OR reported it
        # with prompt_tokens=0 (Ollama's openai-compat streams a final
        # usage chunk with only completion_tokens populated - prompt
        # stays at 0 which would otherwise freeze the UI pressure gauge
        # at 0), estimate BOTH prompt_tokens (from captured input
        # messages + tools schema) AND completion_tokens (from accumulated
        # output). Without this, the session's cumulative `tokens.prompt`
        # stays stuck at 0 forever.
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
            import json as _json
            text = "".join(self.content_parts)
            # Ground-truth via litellm tokenizer for THIS provider's
            # model - same path as ``BaseLLMProvider.count_tokens``.
            # No char/4 heuristic, no CJK approximation: the exact
            # token count the LLM API would have charged us, computed
            # locally without a network round-trip.
            provider = getattr(self.ctx, "provider", None) if self.ctx else None
            estimated_out = max(_exact_count(provider, text), 1)

            estimated_in = 0
            if self.input_messages:
                try:
                    # ``input_messages`` may be dicts OR ChatMessage
                    # dataclass instances (``to_chat_messages()``
                    # converts before passing to the provider). Build
                    # a uniform list-of-dicts and let the provider's
                    # message tokenizer count the boilerplate too.
                    def _field(obj, name, default=""):
                        if isinstance(obj, dict):
                            return obj.get(name, default)
                        return getattr(obj, name, default)

                    msg_dicts: list[dict[str, Any]] = []
                    for m in self.input_messages:
                        c = _field(m, "content", "")
                        if isinstance(c, list):
                            text_parts = []
                            for part in c:
                                if isinstance(part, dict):
                                    text_parts.append(part.get("text", "") or "")
                                elif isinstance(part, str):
                                    text_parts.append(part)
                            c = " ".join(t for t in text_parts if t)
                        elif not isinstance(c, str):
                            c = str(c) if c is not None else ""
                        msg_dicts.append({
                            "role": str(_field(m, "role", "user")),
                            "content": c,
                        })
                    if provider is not None and hasattr(
                        provider, "count_message_tokens",
                    ):
                        estimated_in += int(
                            provider.count_message_tokens(msg_dicts),
                        )
                    else:
                        # Fallback: per-string count + per-message
                        # boilerplate when the provider isn't reachable.
                        for d in msg_dicts:
                            estimated_in += _exact_count(provider, d["content"])
                        estimated_in += 4 * len(msg_dicts)
                except Exception:
                    logger.debug("prompt count: message walk failed", exc_info=True)
            if self.input_tools:
                try:
                    tools_text = _json.dumps(
                        self.input_tools, ensure_ascii=False, default=str,
                    )
                    estimated_in += _exact_count(provider, tools_text)
                    estimated_in += 4 * len(self.input_tools)
                except Exception:
                    logger.debug("prompt count: tools serialize failed", exc_info=True)

            # Prefer reported values when they look real; substitute
            # estimates only for the missing/zero fields.
            final_prompt = _reported_prompt if _reported_prompt > 0 else estimated_in
            final_completion = _reported_completion if _reported_completion > 0 else estimated_out
            self.last_usage = TokenUsage(
                prompt_tokens=final_prompt,
                completion_tokens=final_completion,
                total_tokens=final_prompt + final_completion,
            )
            if _reported_completion == 0 and self.cb.on_out_token is not None:
                # Subtract whatever the live counter already emitted
                # during streaming so the cumulative total stays exact.
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
        """Emit a ``tool_call_streaming`` callback for live progress.

        ``args_fragment`` is appended to the per-call buffer; the count
        is then re-tokenized via litellm (throttled to 250ms unless
        ``force=True`` - used for the very first emit when the name
        appears, so the UI gets the placeholder immediately even
        though count=0)."""
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
        try:
            cb(call_id, entry["name"], count)
        except Exception:
            logger.debug("on_tool_call_streaming callback error", exc_info=True)

    def _finalize_tool_call_streaming(self, call_id: str) -> None:
        """One last emit when a call is finalized so the UI sees the
        end value, then drop the entry. Lets the frontend swap the
        placeholder for the real tool_start card without missing the
        last few tokens that arrived between the previous tick and
        finalization."""
        if call_id and call_id in self._streaming_tool_calls:
            self._fire_tool_call_streaming(call_id, "", force=True)
            self._streaming_tool_calls.pop(call_id, None)

    def _thinking_token_count(self) -> int:
        """Tokenize the currently-active thinking block via litellm
        and return the cumulative count (always monotonically rising
        within a single thinking block - drops back to 0 only when
        ``_think_content`` is cleared on block close)."""
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
        """Emit incremental ``out_token`` deltas during streaming using
        litellm-tokenized accumulated content. Only fires when the
        provider hasn't been streaming ``usage`` chunks itself - once a
        real usage update lands, ``_track_usage`` takes over via the
        same ``_prev_completion_tokens`` cursor, and we step aside."""
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
            # Backward-compat: callers may pass extra trailing args
            # (e.g. token count) that older callback definitions don't
            # accept. On TypeError, retry with one fewer arg until the
            # call succeeds or we run out of args.
            attempts = list(range(len(args), -1, -1))
            for n in attempts:
                try:
                    res = cb(*args[:n])
                    if _inspect.iscoroutine(res):
                        try:
                            loop = _asyncio.get_running_loop()
                            loop.create_task(res)
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

        # `_in_native_thinking` is distinct from `_in_think` (which is
        # owned by the text-tag parser in `_filter_think_tags`). Mixing
        # the two caused normal content deltas after a native-thinking
        # burst to be classified as more thinking - the daemon then
        # emitted a `thinking` snapshot containing the real response
        # text glued after the thinking, no delimiter.
        if not self._in_native_thinking:
            self._in_native_thinking = True
            self._prev_thinking_tokens = 0
            self._last_thinking_count_at = 0.0
            _fire(self.cb.on_thinking_started)
        self._think_content.append(text)
        self._reasoning_full.append(text)
        # Throttled per-block tokenize: once every 250ms refresh the
        # cumulative thinking-token count for THIS block. Each delta
        # carries the latest known count as its second positional arg.
        import time as _time
        _now = _time.monotonic()
        if _now - self._last_thinking_count_at >= 0.25:
            self._last_thinking_count_at = _now
            cnt = self._thinking_token_count()
            if cnt > self._prev_thinking_tokens:
                self._prev_thinking_tokens = cnt
        _fire(self.cb.on_thinking_delta, text, self._prev_thinking_tokens)

    async def _flush_native_thinking(self, delta: str) -> None:
        """Close an ongoing native-thinking session and emit the
        snapshot. Called when a content delta arrives (native thinking
        is complete) or when the stream terminates. Content deltas
        themselves must NOT be captured here - only the accumulated
        `_think_content` is flushed.
        """
        if self._in_native_thinking and self._think_content:
            # Final tokenize before clearing - gives the snapshot
            # event the exact per-block count.
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
        """Emit an ``assistant_stream_snapshot`` event with the full
        accumulated content to date. The event is persisted in
        ``session_events`` so a client that reconnects mid-turn gets
        the latest "so far" text and can resume the token stream in
        sync.
        """
        ctx = self.ctx
        if ctx is None:
            return
        # Build the full visible content accumulated so far.
        text = "".join(self.content_parts)
        # Strip thinking tags from the snapshot - we want the visible
        # text only (tokens stream that visible view too).
        # Cheap approach: drop anything inside <think>...</think>.
        import re as _re
        visible_only = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL)
        bus = getattr(ctx, "_event_bus", None)
        bus_key = getattr(ctx, "_bus_key", None)
        if bus is not None and bus_key is not None:
            try:
                await bus.publish(bus_key, {
                    "type": "assistant_stream_snapshot",
                    "data": {
                        "content": visible_only,
                        "chars": len(visible_only),
                    },
                })
            except Exception as exc:
                logger.debug(
                    "assistant_stream_snapshot_publish_failed: %s", exc,
                )
        # Durable snapshot - keeps the ``history_log`` row for the
        # in-flight assistant message in sync with whatever has been
        # streamed so far. A daemon crash mid-turn no longer loses the
        # response: on next load, the row (with its last known content
        # + ``streaming_status='streaming'`` flag) is replayed to the
        # client exactly like any other message. Fire-and-forget so
        # DB latency never blocks the stream loop.
        _schedule_streaming_persist(ctx, visible_only, status="streaming")

    def _filter_inline_tool_markers(self, visible: str) -> str:
        """Suppress output once we see a tool-call marker in the stream.

        Models like qwen2.5 emit their tool calls as text inside the
        content stream, e.g.::

            content: "I will read ..."
            tool_calls: [<tool_call>{"name": "read", "arguments": {...}}]

        Also emit CJK variants (`工具调用:`) when prompted in Chinese.
        Also wrap their whole reply with `content: "..."`.

        We:
        1. Strip the leading `content: "` wrapper if present at start.
        2. Buffer tail chars to catch markers split across chunks.
        3. On marker detection, close the gate + strip trailing wrapper `"`.
        """
        if self._inline_tool_gate:
            return ""

        # Step 1: check for `content: "` wrapper on first real chunk.
        if not self._content_wrapper_checked:
            combined_probe = self._inline_tool_hold + visible
            # Need enough chars to decide (up to `content: "` length)
            if len(combined_probe.lstrip()) >= len('content: "') or "\n" in combined_probe:
                self._content_wrapper_checked = True
                m = _CONTENT_WRAPPER_RE.match(combined_probe)
                if m:
                    self._content_wrapper_active = True
                    # Consume the matched prefix
                    after = combined_probe[m.end():]
                    self._inline_tool_hold = ""
                    visible = after
                else:
                    # No wrapper - carry on with normal flow.
                    pass
            else:
                # Not enough yet - keep buffering.
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
            # Strip trailing wrapper quote + whitespace if the model
            # closed its `content: "..."` before the marker.
            if self._content_wrapper_active:
                out = out.rstrip()
                if out.endswith('"'):
                    out = out[:-1].rstrip()
            return out

        # No marker yet - but the tail may be the start of one.
        tail_len = min(_INLINE_TOOL_MAX_MARKER_LEN - 1, len(buf))
        tail = buf[-tail_len:] if tail_len > 0 else ""
        tail_low = tail.lower()
        held = ""
        for k in range(tail_len, 0, -1):
            suffix = tail_low[-k:]
            if any(m.lower().startswith(suffix) for m in _INLINE_TOOL_MARKERS):
                held = tail[-k:]
                break

        # When the `content: "..."` wrapper is active, also hold back the
        # trailing closing quote (and any whitespace after it) so that if
        # the marker arrives in the next chunk we can strip the quote
        # without it ever reaching the client.
        if self._content_wrapper_active and not held:
            stripped_tail = buf.rstrip()
            if stripped_tail.endswith('"'):
                # Hold the quote + trailing whitespace
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
                # Buffer ends on a k-char prefix of ``<think>``. Keep
                # those chars for the next delta so a split open tag
                # is still detected.
                # Edge case: when the ENTIRE buffer is the partial
                # tag (k == len(buf)), ``before`` is empty and the
                # buffer is unchanged. The caller's ``while
                # self._think_buf`` would spin forever. Signal
                # no-progress with ``None`` so the outer loop breaks
                # and waits for more input. This hits every time a
                # stream chunk ends exactly on ``<``, ``<t``, ``<th``
                # - common when the LLM emits HTML like ``<table>``
                # or ``<title>``.
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
            # Live UX: fire the placeholder immediately on the first
            # chunk that carries the tool name (force=True so count=0
            # lands right away), then throttled 250ms ticks as args
            # stream in.
            call_id = entry.get("id") or ""
            if call_id and (new_name or arg_frag):
                self._fire_tool_call_streaming(
                    call_id, entry["name"], arg_frag, force=bool(new_name),
                )

    def _accumulate_tool_delta(self, tc: dict) -> None:
        new_name = False
        if tc.get("name"):
            if self._current_tool is not None:
                # Previous in-flight call ends here - finalize its
                # streaming progress so the UI swaps the placeholder
                # for the real card.
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


def _recover_partial_json(args_str: str, tool_name: str) -> dict:
    """Try to recover parameters from truncated/malformed JSON.

    Common case: the LLM generates a huge 'content' field that gets
    truncated in streaming, breaking the JSON. We try to extract
    whatever key-value pairs we can.

    RT22: tracks whether the recovery is "complete" (full JSON repaired)
    or "partial" (regex extraction from truncated text). Partial results
    include a __truncated marker so the caller can refuse to execute
    operations that should not run with incomplete params (e.g. SQL).
    """
    import re
    result = {}

    # Try adding closing braces to fix truncated JSON
    fixed = args_str.rstrip()
    for suffix in ['"}', '"}}}', '"}}', '"}]', '}']:
        try:
            parsed = json.loads(fixed + suffix)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    # Extract string values with regex: "key": "value" or "key": "value..."
    truncated_keys: list[str] = []
    # Use a positive look-ahead anchor to detect TRUNCATED values (no closing ")
    for m in re.finditer(r'"(\w+)"\s*:\s*"((?:[^"\\]|\\.)*)("|$)', args_str):
        key, val, closer = m.group(1), m.group(2), m.group(3)
        try:
            result[key] = json.loads(f'"{val}"')
        except Exception:
            result[key] = val
        if closer != '"':
            # Value was truncated mid-string - mark it
            truncated_keys.append(key)

    # Extract non-string values: "key": true/false/null/number
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
    """Merge accumulated and delta tool calls, parse JSON arguments."""
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
                # Streaming may produce truncated JSON - try to recover
                parsed = _recover_partial_json(args_str, entry["name"])
        # RT14: use uuid suffix instead of len() to avoid collision across
        # multiple turns in the same session.
        import uuid as _uuid
        final_id = entry["id"] or f"call_{_uuid.uuid4().hex[:12]}"
        tool_calls.append({
            "id": final_id,
            "type": "function",
            "function": {"name": entry["name"], "arguments": parsed},
        })
        # Final live-progress emit so the frontend gets the end value
        # before the placeholder is swapped for the real tool_start
        # card. No-op when streaming was never seen for this entry.
        state._finalize_tool_call_streaming(final_id)

    for tc in tool_calls:
        args = tc["function"].get("arguments", "")
        if isinstance(args, str) and args.strip():
            try:
                tc["function"]["arguments"] = json.loads(_fix_win_backslashes(args))
            except json.JSONDecodeError:
                # RT3: try to recover partial JSON before falling back to {}
                tool_name = tc["function"].get("name", "")
                recovered = _recover_partial_json(args, tool_name)
                tc["function"]["arguments"] = recovered if recovered else {}

    return tool_calls


async def emit_thinking(on_thinking: Any, text: str, count: int = 0) -> None:
    """Call on_thinking safely - handles sync and async callbacks.

    ``count`` is the litellm-tokenized cumulative count for THIS
    thinking block - used by the frontend to render the per-block
    counter. Backward-compat: callbacks that only accept ``(text)``
    are called without the count via TypeError fallback.
    """
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
    """Fire on_token callback, handling sync/async.

    ``count`` is the running cumulative completion-token count at the
    moment this delta was emitted - sourced from either the provider's
    streaming ``usage`` chunks or our litellm live counter (whichever
    is active). Passed through to the SSE ``token`` event so the
    frontend can update its counter without a separate event stream.
    """
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

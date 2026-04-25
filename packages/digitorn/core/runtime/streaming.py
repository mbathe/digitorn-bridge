"""LLM streaming — token-by-token chat with think-tag filtering."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from digitorn.core.runtime.callbacks import AgentTurnCallbacks

logger = logging.getLogger(__name__)


def _estimate_tokens(text: str) -> int:
    """Estimate token count more accurately than len//4.

    Uses a weighted approach: CJK/emoji characters ≈ 2-3 tokens each,
    ASCII words ≈ 1.3 tokens each (code has more symbols/short tokens).
    Still heuristic but much closer to real tokenizer output.
    """
    if not text:
        return 0
    cjk_count = 0
    ascii_chars = 0
    for ch in text:
        cp = ord(ch)
        # CJK Unified, Katakana, Hiragana, Hangul, emoji ranges
        if (0x4E00 <= cp <= 0x9FFF or 0x3040 <= cp <= 0x30FF
                or 0xAC00 <= cp <= 0xD7AF or cp >= 0x1F600):
            cjk_count += 1
        else:
            ascii_chars += 1
    # CJK chars average ~2 tokens each; ASCII ~1 token per 3.5 chars
    # (tighter than //4 which under-counts code with many short symbols)
    return int(cjk_count * 2 + ascii_chars / 3.5) or 1


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
# recent context — a Chinese user gets `工具调用:` instead of `tool_call:`.
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

# Leading wrapper some models emit — `content: "..."` around the
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


async def streaming_chat(
    provider: Any,
    messages: list[dict],
    tools: list[dict] | None,
    generation_params: dict,
    cb: AgentTurnCallbacks,
    ctx: Any = None,
) -> tuple[str, list[dict], Any]:
    """Stream a chat response and return (content, tool_calls, response).

    All UI callbacks are wrapped in try/except — a callback error must
    never crash the agent loop or interrupt the LLM stream.

    ``ctx`` — optional AgentContext; when passed, the stream emits
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

    class _FakeResponse:
        pass

    fake = _FakeResponse()
    fake.raw = {}
    fake.usage = state.last_usage
    fake.finish_reason = state.finish_reason
    fake.stop_reason = state.finish_reason  # Anthropic uses stop_reason
    return content, tool_calls, fake


class _StreamState:
    """Mutable state accumulated during a single stream."""

    __slots__ = (
        "cb", "ctx", "content_parts", "tool_calls", "last_usage", "finish_reason",
        "_current_tool", "_tool_args_buf", "_tool_acc",
        "_stream_done_fired", "_think_buf", "_in_think", "_think_content",
        "_in_native_thinking",
        "_prev_completion_tokens", "_last_snapshot_at",
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
        # Once a marker like `tool_calls:` or `<tool_call>` is seen, we
        # stop forwarding any further visible content to the client so
        # the raw call noise never flashes on screen. The full content
        # is still accumulated in content_parts for extract_tool.
        self._inline_tool_gate = False
        # Rolling tail buffer — holds the last N chars that might be the
        # prefix of a marker (e.g. "<too" waiting for "l_call>").
        self._inline_tool_hold = ""
        # Track if the response starts with `content: "...` wrapper.
        # When active, we strip the prefix AND the matching trailing `"`
        # right before the tool-call marker.
        self._content_wrapper_active = False
        self._content_wrapper_checked = False
        self._prev_completion_tokens: int = 0
        self._last_snapshot_at: float = 0.0
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
        # that signals the model exited thinking — flush the accumulated
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
                await _fire_token(self.cb, visible)
                # Mid-stream snapshot for reconnect durability: every
                # 500ms emit + persist the accumulated content so late
                # joiners see what the assistant has written so far
                # (tokens themselves are ephemeral by design).
                import time as _time
                now = _time.monotonic()
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

    async def flush(self) -> None:
        if self._current_tool is not None:
            self._current_tool["function"]["arguments"] = "".join(self._tool_args_buf)
            self.tool_calls.append(self._current_tool)

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
                    self.cb.on_token(self._think_buf)
            except Exception:
                logger.debug("on_token callback error (finalize)", exc_info=True)

        # Fallback: if the provider never reported usage OR reported it
        # with prompt_tokens=0 (Ollama's openai-compat streams a final
        # usage chunk with only completion_tokens populated — prompt
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
            from digitorn.core.runtime.session_metrics import _count_tokens
            import json as _json
            text = "".join(self.content_parts)
            # Use the shared tiktoken path when available — gives a count
            # that's within a few % of what a real tokenizer would bill,
            # instead of the older CJK/ASCII heuristic which could be off
            # by ±25%.
            estimated_out = max(_count_tokens(text), 1)

            estimated_in = 0
            if self.input_messages:
                try:
                    # `input_messages` may be dicts OR ChatMessage dataclass
                    # instances (to_chat_messages() converts before passing
                    # to the provider). Handle both shapes uniformly via
                    # getattr-with-dict-fallback.
                    def _field(obj, name, default=""):
                        if isinstance(obj, dict):
                            return obj.get(name, default)
                        return getattr(obj, name, default)

                    msg_text = ""
                    tc_text = ""
                    for m in self.input_messages:
                        c = _field(m, "content", "")
                        if isinstance(c, str):
                            msg_text += c + "\n"
                        elif isinstance(c, list):
                            for part in c:
                                if isinstance(part, dict):
                                    msg_text += (part.get("text", "") or "") + "\n"
                                elif isinstance(part, str):
                                    msg_text += part + "\n"
                        tcs = _field(m, "tool_calls", None) or []
                        if tcs:
                            tc_text += _json.dumps(tcs, ensure_ascii=False, default=str)
                    estimated_in += _count_tokens(msg_text) + _count_tokens(tc_text)
                    estimated_in += 4 * len(self.input_messages)
                except Exception:
                    logger.debug("prompt estimation: message walk failed", exc_info=True)
            if self.input_tools:
                try:
                    tools_text = _json.dumps(
                        self.input_tools, ensure_ascii=False, default=str,
                    )
                    estimated_in += _count_tokens(tools_text) + 4 * len(self.input_tools)
                except Exception:
                    logger.debug("prompt estimation: tools serialize failed", exc_info=True)

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
                try:
                    self.cb.on_out_token(estimated_out)
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
            delta = ct - self._prev_completion_tokens
            self._prev_completion_tokens = ct
            if delta > 0:
                try:
                    self.cb.on_out_token(delta)
                except Exception:
                    logger.debug("on_out_token callback error", exc_info=True)

    def _handle_native_thinking(self, text: str) -> None:
        import asyncio as _asyncio
        import inspect as _inspect

        def _fire(cb: Any, *args: Any) -> None:
            if cb is None:
                return
            try:
                res = cb(*args)
                if _inspect.iscoroutine(res):
                    try:
                        loop = _asyncio.get_running_loop()
                        loop.create_task(res)
                    except RuntimeError:
                        res.close()
            except Exception:
                logger.debug("native_thinking callback error", exc_info=True)

        # `_in_native_thinking` is distinct from `_in_think` (which is
        # owned by the text-tag parser in `_filter_think_tags`). Mixing
        # the two caused normal content deltas after a native-thinking
        # burst to be classified as more thinking — the daemon then
        # emitted a `thinking` snapshot containing the real response
        # text glued after the thinking, no delimiter.
        if not self._in_native_thinking:
            self._in_native_thinking = True
            _fire(self.cb.on_thinking_started)
        self._think_content.append(text)
        _fire(self.cb.on_thinking_delta, text)

    async def _flush_native_thinking(self, delta: str) -> None:
        """Close an ongoing native-thinking session and emit the
        snapshot. Called when a content delta arrives (native thinking
        is complete) or when the stream terminates. Content deltas
        themselves must NOT be captured here — only the accumulated
        `_think_content` is flushed.
        """
        if self._in_native_thinking and self._think_content:
            text = "".join(self._think_content).strip()
            if text:
                await emit_thinking(self.cb.on_thinking, text)
            self._think_content.clear()
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
        bus = getattr(ctx, "_event_bus", None)
        bus_key = getattr(ctx, "_bus_key", None)
        if bus is None or bus_key is None:
            return
        # Build the full visible content accumulated so far.
        text = "".join(self.content_parts)
        # Strip thinking tags from the snapshot — we want the visible
        # text only (tokens stream that visible view too).
        # Cheap approach: drop anything inside <think>...</think>.
        import re as _re
        visible_only = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL)
        try:
            await bus.publish(bus_key, {
                "type": "assistant_stream_snapshot",
                "data": {
                    "content": visible_only,
                    "chars": len(visible_only),
                },
            })
        except Exception as exc:
            logger.debug("assistant_stream_snapshot_publish_failed: %s", exc)

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
                    # No wrapper — carry on with normal flow.
                    pass
            else:
                # Not enough yet — keep buffering.
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

        # No marker yet — but the tail may be the start of one.
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

    async def _consume_think_close(self) -> str | None:
        end_idx = self._think_buf.find(_THINK_CLOSE)
        if end_idx == -1:
            return None
        final_chunk = self._think_buf[:end_idx]
        if final_chunk:
            self._think_content.append(final_chunk)
            if self.cb.on_thinking_delta is not None:
                try:
                    self.cb.on_thinking_delta(final_chunk)
                except Exception:
                    logger.debug("on_thinking_delta callback error (close)", exc_info=True)
        text = "".join(self._think_content).strip()
        if text:
            await emit_thinking(self.cb.on_thinking, text)
        self._think_content.clear()
        self._think_buf = self._think_buf[end_idx + _THINK_CLOSE_LEN:].lstrip()
        self._in_think = False
        return ""

    def _buffer_think_content(self) -> None:
        if len(self._think_buf) >= _THINK_CLOSE_LEN:
            chunk = self._think_buf[:-(_THINK_CLOSE_LEN - 1)]
            self._think_content.append(chunk)
            if self.cb.on_thinking_delta is not None and chunk:
                try:
                    self.cb.on_thinking_delta(chunk)
                except Exception:
                    logger.debug("on_thinking_delta callback error (buffer)", exc_info=True)
            self._think_buf = self._think_buf[-(_THINK_CLOSE_LEN - 1):]

    def _consume_think_open(self) -> str | None:
        start_idx = self._think_buf.find(_THINK_OPEN)
        if start_idx != -1:
            before = self._think_buf[:start_idx]
            self._think_buf = self._think_buf[start_idx + len(_THINK_OPEN):]
            self._in_think = True
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
                # — common when the LLM emits HTML like ``<table>``
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
            if tc.get("id"):
                entry["id"] = tc["id"]
            if tc.get("name"):
                entry["name"] = tc["name"]
            if tc.get("arguments"):
                entry["args_parts"].append(_coerce_tool_arguments_fragment(tc["arguments"]))

    def _accumulate_tool_delta(self, tc: dict) -> None:
        if tc.get("name"):
            if self._current_tool is not None:
                self._current_tool["function"]["arguments"] = "".join(self._tool_args_buf)
                self.tool_calls.append(self._current_tool)
                self._tool_args_buf = []
            self._current_tool = {
                "id": tc.get("id", f"call_{len(self.tool_calls)}"),
                "type": "function",
                "function": {"name": tc["name"], "arguments": ""},
            }
        if tc.get("arguments"):
            self._tool_args_buf.append(_coerce_tool_arguments_fragment(tc["arguments"]))


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
            # Value was truncated mid-string — mark it
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
                # Streaming may produce truncated JSON — try to recover
                parsed = _recover_partial_json(args_str, entry["name"])
        # RT14: use uuid suffix instead of len() to avoid collision across
        # multiple turns in the same session.
        import uuid as _uuid
        tool_calls.append({
            "id": entry["id"] or f"call_{_uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {"name": entry["name"], "arguments": parsed},
        })

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


async def emit_thinking(on_thinking: Any, text: str) -> None:
    """Call on_thinking safely — handles sync and async callbacks."""
    if on_thinking is None or not text or not text.strip():
        return
    clean = text.strip()
    if clean.startswith(("{", "[")) and any(
        k in clean for k in ('"type":', '"properties":', '"required":')
    ):
        return
    try:
        result = on_thinking(clean)
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:
        logger.debug("on_thinking callback error: %s", exc, exc_info=True)


async def _fire_token(cb: AgentTurnCallbacks, text: str) -> None:
    """Fire on_token callback, handling sync/async."""
    if cb.on_token is None:
        return
    try:
        if asyncio.iscoroutinefunction(cb.on_token):
            await cb.on_token(text)
        else:
            cb.on_token(text)
    except Exception as exc:
        logger.debug("on_token callback error: %s", exc, exc_info=True)

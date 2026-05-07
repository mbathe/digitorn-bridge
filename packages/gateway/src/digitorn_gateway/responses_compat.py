"""Translate the OpenAI ``/v1/responses`` API to/from ``/v1/chat/completions``.

The dispatch path keeps a single user-facing endpoint
(``POST /v1/chat/completions``). When the requested model is
"responses-only" (newer reasoning-tier models that OpenAI / Copilot
expose ONLY on the ``/responses`` API), we transparently:

  1. Convert the chat-completions request body into ``aresponses()`` kwargs.
  2. Call ``litellm.aresponses(...)`` instead of ``litellm.acompletion(...)``.
  3. Convert the returned ``Response`` object back into the
     chat-completions shape so the caller never has to know.

Same idea for streaming: every typed SSE event the responses API
emits (``response.output_text.delta``, ``response.completed``, ...)
is reshaped into the chat-completions SSE the OpenAI SDK expects.

This file is the ONLY place that knows about both shapes.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


# Models OpenAI / Copilot serve ONLY via /v1/responses. Adding a
# new entry here is the supported way to opt a fresh reasoning model
# into the auto-route. The runtime ALSO falls through if LiteLLM
# returns ``unsupported_api_for_model`` -- so unknown future models
# get the right routing on their first call without a code release.
RESPONSES_ONLY_MODELS: frozenset[str] = frozenset({
    # OpenAI direct
    "o1-pro", "o1-pro-2025-03-19",
    # GitHub Copilot (mirrors OpenAI, plus a few Copilot-internal codex models)
    "gpt-5.5", "gpt-5.4-mini",
})


def is_responses_only(real_model_id: str) -> bool:
    """Return True when ``real_model_id`` is known to live only on the
    OpenAI ``/responses`` API. The runtime layer also detects
    ``unsupported_api_for_model`` errors at dispatch time as a
    fallback for unknown models that turn out to need /responses."""
    return real_model_id in RESPONSES_ONLY_MODELS


# ── Body conversion: chat → responses kwargs ────────────────────


def chat_body_to_responses_kwargs(body: dict[str, Any]) -> dict[str, Any]:
    """Map a chat-completions body to the kwargs ``litellm.aresponses``
    accepts.

    LiteLLM's responses API takes ``input`` (a list of message-like
    dicts OR a plain string) instead of ``messages``. Most other
    fields are pass-through. ``max_tokens`` is renamed
    ``max_output_tokens``."""
    out: dict[str, Any] = {}

    messages = body.get("messages") or []
    # ``input`` can be a list of message dicts OR a string. We pass
    # the messages list as-is; LiteLLM accepts either shape.
    out["input"] = messages

    if "max_tokens" in body and body["max_tokens"] is not None:
        out["max_output_tokens"] = body["max_tokens"]

    # Sampling: temperature, top_p flow as-is. The new responses API
    # accepts the same names.
    for k in ("temperature", "top_p", "stop", "user", "metadata", "seed"):
        if k in body and body[k] is not None:
            out[k] = body[k]

    # Tools: the responses API uses a FLATTER tool shape than chat
    # completions:
    #   chat:      {"type": "function", "function": {"name": "...", "parameters": ...}}
    #   responses: {"type": "function", "name": "...", "parameters": ...}
    # We auto-flatten so the caller never has to know.
    if body.get("tools"):
        flat: list[dict[str, Any]] = []
        for t in body["tools"]:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "function" and "function" in t:
                fn = t["function"] or {}
                flat.append({
                    "type": "function",
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters") or {},
                })
            else:
                flat.append(t)
        out["tools"] = flat
    if body.get("tool_choice") is not None:
        out["tool_choice"] = body["tool_choice"]

    return out


# ── Response conversion: responses → chat shape ─────────────────


def _extract_text_from_responses_output(output: list[Any]) -> tuple[str, list[Any]]:
    """Walk the typed ``output`` array and pull out the assistant text
    + any tool calls, mirroring the chat completions ``message`` shape."""
    text_parts: list[str] = []
    tool_calls: list[Any] = []
    for item in output or []:
        # Items can be dicts OR pydantic models -- normalise to dict first.
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        elif hasattr(item, "dict"):
            item = item.dict()
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "")
        if item_type == "message":
            for c in item.get("content", []) or []:
                if isinstance(c, dict) and c.get("type") in (
                    "output_text", "text",
                ):
                    text_parts.append(str(c.get("text", "")))
        elif item_type in ("tool_call", "function_call"):
            tool_calls.append({
                "id": item.get("id") or item.get("call_id") or "",
                "type": "function",
                "function": {
                    "name": item.get("name") or item.get("function", {}).get("name", ""),
                    "arguments": item.get("arguments") or item.get(
                        "function", {},
                    ).get("arguments", ""),
                },
            })
    return "".join(text_parts), tool_calls


def responses_to_chat_completion(
    resp: Any, *, fallback_model: str | None = None,
) -> dict[str, Any]:
    """Convert a LiteLLM responses-API result into the OpenAI chat
    completions response shape so the gateway's caller is unaffected."""
    if hasattr(resp, "model_dump"):
        d = resp.model_dump()
    elif hasattr(resp, "dict"):
        d = resp.dict()
    elif isinstance(resp, dict):
        d = resp
    else:
        d = {}

    output = d.get("output") or []
    text, tool_calls = _extract_text_from_responses_output(output)

    # Some servers expose ``output_text`` as a top-level convenience.
    if not text and isinstance(d.get("output_text"), str):
        text = d["output_text"]

    # Usage shape: responses uses ``input_tokens`` / ``output_tokens``;
    # chat completions uses ``prompt_tokens`` / ``completion_tokens``.
    raw_usage = d.get("usage") or {}
    usage = {
        "prompt_tokens": raw_usage.get(
            "input_tokens", raw_usage.get("prompt_tokens", 0),
        ),
        "completion_tokens": raw_usage.get(
            "output_tokens", raw_usage.get("completion_tokens", 0),
        ),
        "total_tokens": raw_usage.get("total_tokens", 0),
    }
    # Reasoning tokens: surface them under ``completion_tokens_details``
    # so the chat-completions shape stays valid AND we expose the count.
    rt = (raw_usage.get("output_tokens_details") or {}).get("reasoning_tokens")
    if rt is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": rt}

    finish = d.get("status") or "stop"
    if finish == "completed":
        finish = "stop"

    msg: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        msg["tool_calls"] = tool_calls

    return {
        "id": d.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": d.get("created_at") or int(time.time()),
        "model": d.get("model") or fallback_model or "",
        "choices": [{
            "index": 0,
            "message": msg,
            "finish_reason": finish,
        }],
        "usage": usage,
    }


# ── Streaming: responses SSE events → chat.completion.chunk ──────


async def stream_responses_as_chat_chunks(
    stream: AsyncIterator[Any], *, model_hint: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Re-shape a ``litellm.aresponses(stream=True)`` event stream as
    chat-completions chunks, matching what the OpenAI SDK expects so
    the caller sees a uniform SSE flow regardless of which API the
    gateway dispatched to."""
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    sent_role = False

    async for ev in stream:
        # Normalise to dict.
        if hasattr(ev, "model_dump"):
            ev = ev.model_dump()
        elif hasattr(ev, "dict"):
            ev = ev.dict()
        if not isinstance(ev, dict):
            continue
        ev_type = ev.get("type", "")
        delta_text: str | None = None
        finish_reason: str | None = None

        if ev_type == "response.output_text.delta":
            delta_text = ev.get("delta") or ""
        elif ev_type == "response.output_text.done":
            # No delta to emit; the per-chunk deltas already covered it.
            continue
        elif ev_type == "response.completed":
            finish_reason = "stop"
        elif ev_type == "response.failed":
            finish_reason = "error"
        elif ev_type in (
            "response.output_item.added", "response.created",
            "response.in_progress",
        ):
            # Metadata events; nothing user-visible to emit yet.
            continue
        else:
            # Unknown event type: ignore quietly so a future server-side
            # event addition doesn't crash the stream.
            continue

        delta: dict[str, Any] = {}
        if not sent_role:
            delta["role"] = "assistant"
            sent_role = True
        if delta_text is not None:
            delta["content"] = delta_text

        chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": ev.get("model") or model_hint or "",
            "choices": [{
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }],
        }
        yield chunk
        if finish_reason is not None:
            return

    # Stream ended without a ``response.completed`` event -- still emit
    # a terminator so the OpenAI SDK closes cleanly.
    yield {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_hint or "",
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": "stop",
        }],
    }


# ── Detection at dispatch time: catch unknown new models ────────


_UPSTREAM_RESPONSES_HINT = "is not accessible via the /chat/completions endpoint"


def looks_like_responses_only_error(exc: Exception) -> bool:
    """When a /chat/completions call hits an upstream model that's
    only on /responses, the error message says exactly that. We
    detect it so the dispatch loop can retry via ``aresponses()``
    without hardcoding every new model name."""
    return _UPSTREAM_RESPONSES_HINT in str(exc).lower() or \
        "unsupported_api_for_model" in str(exc).lower()


# ── Reasoning models: max_tokens must leave room for reasoning ──


# Substring patterns -- match the canonical real_model_id Copilot /
# OpenAI use. We don't have to be exhaustive: any reasoning model not
# listed still works, it just won't get the auto-bump.
_REASONING_MODEL_HINTS = (
    "o1", "o3", "o4",
    "gemini-2.5", "gemini-3", "gemini-3.1",
    "claude-opus-4", "claude-sonnet-4", "claude-haiku-4",
    "gpt-5",
    "deepseek-r1",
)

# When a reasoning model is detected and the caller's max_tokens is
# below this floor, we silently bump to it. Reasoning consumes 20-200
# tokens before the model emits visible output; leaving 8 tokens of
# headroom (the OpenAI default for ``max_tokens``) makes the response
# come back EMPTY because the budget got eaten by the reasoning trace.
_REASONING_MAX_TOKENS_FLOOR = 512


def is_reasoning_model(real_model_id: str) -> bool:
    name = (real_model_id or "").lower()
    return any(h in name for h in _REASONING_MODEL_HINTS)


def bump_max_tokens_for_reasoning(
    body_or_kwargs: dict[str, Any], real_model_id: str,
) -> None:
    """Mutate the request body in place so a reasoning model has
    enough budget to emit visible output AFTER its reasoning trace
    (which can consume 30-200 tokens transparently).

    No-op for non-reasoning models so we never silently raise costs
    on classic chat models."""
    if not is_reasoning_model(real_model_id):
        return
    current = body_or_kwargs.get("max_tokens") or body_or_kwargs.get(
        "max_output_tokens",
    )
    if current is None:
        # Caller didn't set one; let upstream pick its default.
        return
    if int(current) >= _REASONING_MAX_TOKENS_FLOOR:
        return
    # Bump and keep both keys in sync (responses uses max_output_tokens,
    # chat completions uses max_tokens).
    if "max_tokens" in body_or_kwargs:
        body_or_kwargs["max_tokens"] = _REASONING_MAX_TOKENS_FLOOR
    if "max_output_tokens" in body_or_kwargs:
        body_or_kwargs["max_output_tokens"] = _REASONING_MAX_TOKENS_FLOOR


def recover_content_from_empty_response(resp: dict[str, Any]) -> dict[str, Any]:
    """Some Copilot / Gemini responses come back with empty
    ``choices[0].message.content`` even though the model produced
    text -- the content lives in ``reasoning_text``, ``thinking``,
    or a non-standard field. We do a best-effort recovery so the
    caller never sees a blank assistant message when text was
    actually generated.

    Mutates and returns ``resp``."""
    choices = resp.get("choices") or []
    if not choices:
        return resp
    msg = (choices[0].get("message") or {})
    content = msg.get("content") or ""
    if content.strip():
        return resp  # nothing to recover

    # Try a few alternate fields the upstream may have used.
    for alt in ("reasoning_text", "reasoning", "thinking", "thought"):
        v = msg.get(alt)
        if isinstance(v, str) and v.strip():
            msg["content"] = v
            return resp
    # If finish_reason == "length" and reasoning_tokens > 0, surface a
    # synthetic message so the caller sees WHY content is empty
    # instead of getting a blank string.
    finish = choices[0].get("finish_reason")
    usage = resp.get("usage") or {}
    rt = (usage.get("completion_tokens_details") or {}).get(
        "reasoning_tokens",
    )
    if finish == "length" and rt:
        msg["content"] = (
            f"[truncated: {rt} reasoning tokens consumed the budget; "
            f"increase max_tokens above {_REASONING_MAX_TOKENS_FLOOR}]"
        )
    return resp

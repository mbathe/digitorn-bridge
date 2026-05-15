"""OpenAI-compatible provider.

Covers all providers that expose an OpenAI-compatible API:
OpenAI, DeepSeek, Groq, Mistral, Together, Ollama, vLLM, LM Studio, etc.

Uses the ``openai`` async client which works with any compatible endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, AsyncIterator

from digitorn.modules.llm_provider.providers.base import (
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    ProviderCapabilities,
    ProviderInfo,
    StreamChunk,
    TokenUsage,
)

logger = logging.getLogger(__name__)


_KNOWN_PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "env_key": "DEEPSEEK_API_KEY",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "env_key": "GROQ_API_KEY",
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "env_key": "MISTRAL_API_KEY",
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "env_key": "TOGETHER_API_KEY",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "env_key": "",
    },
    "lm_studio": {
        "base_url": "http://localhost:1234/v1",
        "env_key": "",
    },
    "vllm": {
        "base_url": "http://localhost:8000/v1",
        "env_key": "",
    },
}


_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-chat": 131_072,
    "deepseek-coder": 131_072,
    "deepseek-reasoner": 131_072,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    "o4-mini": 200_000,
    "mistral-large-latest": 128_000,
    "mistral-small-latest": 128_000,
    "codestral-latest": 256_000,
    "llama-3.3-70b-versatile": 128_000,
    "llama-3.1-8b-instant": 128_000,
    "mixtral-8x7b-32768": 32_768,
    "gemma2-9b-it": 8_192,
}


# Maximum ``max_tokens`` value each model will accept. The provider
# API returns 400 when the caller exceeds this - apps typically set a
# generous ``max_tokens: 16384`` that works on Anthropic but breaks on
# e.g. DeepSeek (hard cap 8192). We clamp rather than error so the
# same app yaml runs across backends. Only known-capped models need
# an entry; unknown models pass through unchanged.
_MODEL_MAX_OUTPUT_TOKENS: dict[str, int] = {
    "deepseek-chat": 8_192,
    "deepseek-coder": 8_192,
    "deepseek-reasoner": 8_192,
    "gpt-3.5-turbo": 4_096,
    "gpt-4": 8_192,
    "gpt-4-turbo": 4_096,
    "gpt-4o": 16_384,
    "gpt-4o-mini": 16_384,
    "gemma2-9b-it": 8_192,
    "mixtral-8x7b-32768": 8_192,
}


def _lookup_context_window(model: str) -> int:
    """Look up context window for a model. Returns 0 if unknown."""
    if model in _MODEL_CONTEXT_WINDOWS:
        return _MODEL_CONTEXT_WINDOWS[model]
    for known, size in _MODEL_CONTEXT_WINDOWS.items():
        if model.startswith(known):
            return size
    return 0


def _lookup_max_output_tokens(model: str) -> int | None:
    """Return the hard cap on ``max_tokens`` for this model, or None if
    unknown (pass through unchanged)."""
    if model in _MODEL_MAX_OUTPUT_TOKENS:
        return _MODEL_MAX_OUTPUT_TOKENS[model]
    for known, cap in _MODEL_MAX_OUTPUT_TOKENS.items():
        if model.startswith(known):
            return cap
    return None


def _clamp_max_tokens(model: str, requested: int) -> int:
    """Clamp ``requested`` to what ``model`` accepts. Logs once per
    (model, requested) pair when it actually clamps."""
    cap = _lookup_max_output_tokens(model)
    if cap is None or requested <= cap:
        return requested
    key = (model, requested)
    if key not in _MAX_TOKENS_CLAMP_LOG:
        _MAX_TOKENS_CLAMP_LOG.add(key)
        logger.warning(
            "max_tokens=%d exceeds %s cap (%d) - clamped. "
            "Fix the app yaml to avoid the warning.",
            requested, model, cap,
        )
    return cap


_MAX_TOKENS_CLAMP_LOG: set[tuple[str, int]] = set()


_TOOL_USE_PROVIDERS = {
    "openai", "deepseek", "groq", "mistral", "together",
}

_TOOL_USE_MODELS: set[str] = {
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "llama-3.1-8b-instant",
    "mixtral-8x7b-32768",
}

_NO_TOOL_USE_MODELS: set[str] = set()

_NO_TOOL_USE_PROVIDERS = {
    "lm_studio", "vllm", "ollama",
}


def _uses_openai_responses_api(provider_hint: str | None, base_url: str | None, model: str) -> bool:
    """Return True when this model must be called via OpenAI Responses API."""
    hint = (provider_hint or "").lower()
    url = (base_url or "").lower()
    model_lower = model.lower()

    is_openai = hint == "openai" or "api.openai.com" in url
    if not is_openai:
        return False

    return (
        model_lower.startswith("gpt-5")
        or "codex" in model_lower
    )


def _has_tool_use(provider_hint: str | None, model: str) -> bool:
    """Determine if a provider+model combo supports native tool calling."""
    if model in _NO_TOOL_USE_MODELS:
        return False
    if model in _TOOL_USE_MODELS:
        return True
    if provider_hint in _TOOL_USE_PROVIDERS:
        return True
    if provider_hint in _NO_TOOL_USE_PROVIDERS:
        return False
    return True


def resolve_base_url(provider_hint: str | None, base_url: str | None) -> str:
    """Resolve the base URL from explicit value or provider hint."""
    if base_url:
        return base_url
    if provider_hint and provider_hint in _KNOWN_PROVIDERS:
        return _KNOWN_PROVIDERS[provider_hint]["base_url"]
    return "https://api.openai.com/v1"


def _is_local_provider(provider_hint: str | None) -> bool:
    """Return True for providers running locally (Ollama, LM Studio, vLLM)."""
    return provider_hint in {"ollama", "lm_studio", "vllm"}


# Sentinel api_key value: when the brain is routed through the digitorn
# gateway, the inject_session_time hot-swap puts this string in
# ``brain.config.api_key``. The provider sees it, knows the bearer
# isn't a real provider key, and pulls the user's JWT from the
# RequestContext at call time instead.
USER_JWT_PLACEHOLDER = "{{user.jwt}}"


def _inject_digitorn_request_headers(
    self_api_key: str | None, params: dict[str, Any],
) -> None:
    """Mutates ``params['extra_headers']`` in place with the per-request
    Digitorn identity headers (X-Digitorn-{User,App,Session,Run,Agent}-Id)
    and, when the brain is routed through the gateway, an ``Authorization``
    bearer carrying the user's JWT (overrides the SDK's default header
    derived from ``self.api_key``).

    Best-effort: any error here is swallowed so a missing context
    doesn't break the call.
    """
    try:
        from digitorn.core.runtime.request_context import get_request_context
        rc = get_request_context()
        if rc is None:
            return
        digitorn_headers = rc.to_headers()
        if digitorn_headers:
            existing = params.get("extra_headers") or {}
            params["extra_headers"] = {**existing, **digitorn_headers}
        # Gateway-routed brain: api_key holds the placeholder, the real
        # bearer is the user's JWT from the inbound HTTP request. The
        # SDK has already injected ``Authorization: Bearer {{user.jwt}}``
        # in its own headers from self.api_key; ``extra_headers`` wins
        # at the httpx layer so we override here.
        if rc.user_jwt and self_api_key == USER_JWT_PLACEHOLDER:
            existing = params.get("extra_headers") or {}
            params["extra_headers"] = {
                **existing,
                "Authorization": f"Bearer {rc.user_jwt}",
            }
    except Exception:  # noqa: BLE001
        pass


def _is_connection_error(exc: Exception) -> bool:
    """Check if an exception is a connection/timeout error."""
    cls_name = type(exc).__name__
    if "Connect" in cls_name or "Timeout" in cls_name:
        return True
    msg = str(exc).lower()
    return "connection" in msg or "timeout" in msg or "timed out" in msg


def _is_retriable(exc: Exception) -> bool:
    """Check if an LLM error is retriable (transient).

    Special-case: a 429 with body ``code == "quota_exceeded"`` (the
    digitorn-gateway's structured shape) is NOT retriable - the user
    is over budget, not throttled. Returning False here lets the
    caller raise immediately, and a separate post-classification step
    converts the exception into ``QuotaExceededError`` for the
    runtime to react properly.
    """
    if _is_connection_error(exc):
        return True
    cls_name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if status == 429 and _looks_like_quota_exceeded(exc):
        return False
    if "RateLimitError" in cls_name:
        return True
    if "InternalServerError" in cls_name or "ServiceUnavailableError" in cls_name:
        return True
    if status and status in (429, 500, 502, 503, 504):
        return True
    return False


def _looks_like_quota_exceeded(exc: Exception) -> bool:
    """True if the exception advertises quota_exceeded.

    First inspects ``exc.body`` / ``exc.response`` (the typed path the
    openai-python SDK exposes when it parsed the 429 body itself).
    Falls back to scanning ``str(exc)`` for the gateway's structured
    body — LiteLLM wraps the upstream 429 as
    ``"Error code: 429 - {'detail': {'code': 'quota_exceeded', ...}}"``
    and discards the typed body in the process. Without the string
    fallback the agent loop sees a "generic 429" and burns 155 s on
    exponential-backoff retries before the user-facing error event
    fires, well past the 30 s spinner watchdog.
    """
    body = getattr(exc, "body", None) or getattr(exc, "response", None)
    if body is not None:
        if hasattr(body, "json"):
            try:
                body = body.json()
            except Exception:
                body = None
        if isinstance(body, dict):
            detail = body.get("detail", body)
            if isinstance(detail, dict) and detail.get("code") == "quota_exceeded":
                return True
    # String fallback — no typed body, parse the literal dict tail.
    msg = str(exc)
    if "quota_exceeded" not in msg:
        return False
    import ast
    start = msg.find("{")
    end = msg.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return False
    try:
        parsed = ast.literal_eval(msg[start:end + 1])
    except (ValueError, SyntaxError):
        return False
    if not isinstance(parsed, dict):
        return False
    detail = parsed.get("detail", parsed)
    return isinstance(detail, dict) and detail.get("code") == "quota_exceeded"


def _enrich_error(exc: Exception, base_url: str | None, provider_hint: str | None) -> Exception:
    """Enrich LLM errors with actionable context."""
    msg = str(exc)
    cls = type(exc).__name__

    # Gateway quota_exceeded (HTTP 429 with structured body). Surface
    # as a typed ``QuotaExceededError`` so the runtime can render the
    # right user-facing message + thread ``retry_after`` to the UI.
    # Body is recovered from the typed `exc.body` first, then falls
    # back to parsing the literal dict tail of ``str(exc)`` — LiteLLM
    # wraps openai-sdk 429s with the body baked into the message but
    # no typed attribute.
    if _looks_like_quota_exceeded(exc):
        from digitorn.modules.llm_provider.errors import parse_quota_exceeded
        body = getattr(exc, "body", None) or getattr(exc, "response", None)
        if body is not None and hasattr(body, "json"):
            try:
                body = body.json()
            except Exception:
                body = None
        if not isinstance(body, dict):
            # String fallback — same parser used in `_looks_like_quota_exceeded`.
            import ast as _ast
            start = msg.find("{")
            end = msg.rfind("}")
            if start >= 0 and end > start:
                try:
                    parsed = _ast.literal_eval(msg[start:end + 1])
                    if isinstance(parsed, dict):
                        body = parsed
                except (ValueError, SyntaxError):
                    pass
        qe = parse_quota_exceeded(429, body, fallback_message=msg or "quota exceeded")
        if qe is not None:
            return qe

    # Connection errors masquerading as auth errors
    if _is_connection_error(exc):
        url = base_url or "unknown"
        return RuntimeError(
            f"Cannot connect to {url} - check your network, "
            f"proxy settings, or verify the base_url is correct. "
            f"(Original: {cls}: {msg})"
        )

    # Auth errors - add hints
    status = getattr(exc, "status_code", None)
    if status == 401 or "401" in msg or "unauthorized" in msg.lower() or "api key" in msg.lower():
        provider = provider_hint or "provider"
        url = base_url or ""
        # Check if it might actually be a network/DNS issue
        if "connect" in msg.lower() or "network" in msg.lower():
            return RuntimeError(
                f"Cannot reach {url} - this looks like a network issue, not an API key problem. "
                f"Check your internet connection and base_url. (Original: {msg})"
            )
        return RuntimeError(
            f"Authentication failed for {provider} at {url}. "
            f"Verify your API key is valid and the base_url is correct. "
            f"(Original: {msg})"
        )

    # Rate limit
    if status == 429 or "rate" in msg.lower():
        return RuntimeError(f"Rate limited by {provider_hint or 'provider'}. Try again shortly. ({msg})")

    return exc


_LOCAL_PROVIDER_TIMEOUT = 300.0


#
#
#

_LLAMA_FUNC_RE = re.compile(
    r"<function=(\w+)(\{[^}]*\})?\s*>?\s*</function>",
    re.DOTALL,
)

_TOOL_CALL_XML_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    re.DOTALL,
)

_FUNC_CALL_RE = re.compile(
    r"\b(\w+)\(\s*(\{.*?\})\s*\)",
    re.DOTALL,
)

_FUNC_CALL_NO_ARGS_RE = re.compile(
    r"\b(\w+)\(\s*\)",
)

_MARKDOWN_JSON_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```",
    re.DOTALL,
)

_ALL_TAGS_RE = re.compile(
    r"<function=\w+[^<]*</function>|<tool_call>.*?</tool_call>",
    re.DOTALL,
)


def _parse_tool_calls_from_text(
    text: str,
    known_tools: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Extract tool calls from raw text, trying all known LLM formats.

    Args:
        text: Raw text that may contain tool calls.
        known_tools: Optional list of valid tool names for JSON validation.

    Returns:
        List of parsed tool call dicts (OpenAI format), or empty list.
    """
    calls: list[dict[str, Any]] = []

    text = _normalize_quotes(text)

    for name, args_str in _LLAMA_FUNC_RE.findall(text):
        args = _safe_json_parse(args_str.strip()) if args_str.strip() else {}
        calls.append(_make_tool_call(name, args, len(calls)))

    if calls:
        return calls

    for match in _TOOL_CALL_XML_RE.findall(text):
        parsed = _safe_json_parse(match)
        if parsed:
            name = parsed.get("name") or parsed.get("function", "")
            if name and isinstance(name, str):
                args = parsed.get("arguments") or parsed.get("parameters") or parsed.get("params") or {}
                if isinstance(args, str):
                    args = _safe_json_parse(args) or {}
                calls.append(_make_tool_call(name, args, len(calls)))

    if calls:
        return calls

    for name, args_str in _FUNC_CALL_RE.findall(text):
        if known_tools and name in known_tools:
            args = _safe_json_parse(args_str) or {}
            calls.append(_make_tool_call(name, args, len(calls)))

    if not calls:
        for name in _FUNC_CALL_NO_ARGS_RE.findall(text):
            if known_tools and name in known_tools:
                calls.append(_make_tool_call(name, {}, len(calls)))

    if calls:
        return calls

    for match in _MARKDOWN_JSON_RE.findall(text):
        parsed = _safe_json_parse(match)
        if parsed and _looks_like_tool_call(parsed, known_tools):
            name = parsed.get("name") or parsed.get("function", "")
            args = parsed.get("arguments") or parsed.get("parameters") or parsed.get("params") or {}
            if isinstance(args, str):
                args = _safe_json_parse(args) or {}
            calls.append(_make_tool_call(name, args, len(calls)))

    if calls:
        return calls

    json_objects = _extract_json_objects(text)
    for obj in json_objects:
        if _looks_like_tool_call(obj, known_tools):
            name = obj.get("name") or obj.get("function", "")
            args = obj.get("arguments") or obj.get("parameters") or obj.get("params") or {}
            if isinstance(args, str):
                args = _safe_json_parse(args) or {}
            calls.append(_make_tool_call(name, args, len(calls)))

    return calls


def _make_tool_call(name: str, args: dict, index: int) -> dict[str, Any]:
    """Build an OpenAI-format tool call dict."""
    if not isinstance(args, dict):
        args = {}
    return {
        "id": f"call_recovered_{index}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }


def _looks_like_tool_call(
    obj: dict,
    known_tools: list[str] | None = None,
) -> bool:
    """Check if a dict looks like a tool call (has name + args-like field)."""
    if not isinstance(obj, dict):
        return False
    name = obj.get("name") or obj.get("function")
    if not name or not isinstance(name, str):
        return False
    has_args = any(
        k in obj for k in ("arguments", "parameters", "params")
    )
    is_known = known_tools and name in known_tools
    return has_args or bool(is_known)


def _normalize_quotes(s: str) -> str:
    """Replace smart/curly quotes with straight ASCII quotes.

    Many LLMs (especially smaller ones) output Unicode quotes like
    \u201c \u201d \u2018 \u2019 instead of standard ASCII " and '.
    """
    return (
        s.replace("\u201c", '"')   # "
         .replace("\u201d", '"')   # "
         .replace("\u2018", "'")   # '
         .replace("\u2019", "'")   # '
         .replace("\u00ab", '"')   # «
         .replace("\u00bb", '"')   # »
    )


def _safe_json_parse(s: str) -> dict | None:
    """Parse JSON string, return dict or None.

    Normalizes smart quotes before parsing - handles LLM output quirks.
    """
    if not s:
        return None
    s = _normalize_quotes(s)
    try:
        result = json.loads(s)
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_json_objects(text: str) -> list[dict]:
    """Extract top-level JSON objects from text using brace matching."""
    objects: list[dict] = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            depth = 0
            start = i
            in_string = False
            escape = False
            for j in range(i, len(text)):
                c = text[j]
                if escape:
                    escape = False
                    continue
                if c == "\\":
                    escape = True
                    continue
                if c == '"' and not escape:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:j + 1]
                        parsed = _safe_json_parse(candidate)
                        if parsed is not None:
                            objects.append(parsed)
                        i = j + 1
                        break
            else:
                i += 1
        else:
            i += 1
    return objects


def _extract_prose_from_mixed_content(text: str) -> str:
    """Extract the reasoning/prose text from mixed content+tool_calls.

    LLMs often emit responses like:
        content: "I'll analyze the file now."
        tool_calls: [{...}]

    Or:
        I'll analyze the file now.
        <tool_call>{"name": "filesystem.read", ...}</tool_call>

    This function extracts the prose part before any tool-call patterns.
    """
    m = re.match(
        r'^\s*(?:content\s*:\s*)?["\'](.+?)["\']'
        r'\s*(?:tool_calls?\s*:|<tool_call>|\{)',
        text, re.DOTALL,
    )
    if m:
        return m.group(1).strip()

    markers = [
        r'tool_calls?\s*:\s*\[',
        r'tool_calls?\s*:\s*\n',
        r'<tool_call>',
        r'<function=',
        r'\{"name"\s*:',
        r'\[\s*\{\s*"name"',
        r'\n\s*-\s*name\s*:',
    ]
    earliest = len(text)
    for marker in markers:
        match = re.search(marker, text)
        if match and match.start() < earliest:
            earliest = match.start()

    if earliest > 10:
        prose = text[:earliest].strip()
        prose = re.sub(r'^\s*content\s*:\s*', '', prose, flags=re.IGNORECASE).strip()
        if len(prose) >= 2 and prose[0] in ('"', "'") and prose[-1] == prose[0]:
            prose = prose[1:-1].strip()
        if prose:
            return prose

    return ""


def _strip_tool_call_tags(text: str) -> str:
    """Remove all tool call tags/patterns from text, leaving only prose."""
    result = _ALL_TAGS_RE.sub("", text)
    result = _MARKDOWN_JSON_RE.sub("", result)
    return result.strip()


def _extract_tool_names(tools: list[dict[str, Any]] | None) -> list[str] | None:
    """Extract tool names from OpenAI-format tool schemas.

    Returns a list of function names, or None if no tools provided.
    This replaces the old hardcoded _META_TOOLS list - the known tool
    names are now derived dynamically from whatever tools the agent has.
    """
    if not tools:
        return None
    names: list[str] = []
    for tool in tools:
        fn = tool.get("function", {})
        name = fn.get("name", "")
        if name:
            names.append(name)
    return names or None


def _stringify_content(content: Any) -> str:
    """Convert response or tool content to a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))
            else:
                text_parts.append(str(item))
        return " ".join(part for part in text_parts if part)
    if content is None:
        return ""
    return str(content)


def _response_item_to_dict(item: Any) -> dict[str, Any]:
    """Normalize an SDK response item to a dict."""
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        try:
            dumped = item.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    data = getattr(item, "__dict__", None)
    return data if isinstance(data, dict) else {}


def _event_to_dict(event: Any) -> dict[str, Any]:
    """Normalize a streaming event object to a dict."""
    if isinstance(event, dict):
        return event
    if hasattr(event, "model_dump"):
        try:
            dumped = event.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    data = getattr(event, "__dict__", None)
    return data if isinstance(data, dict) else {}


def _responses_input_message(role: str, content: str) -> dict[str, Any]:
    return {
        "role": role,
        "content": [{"type": "input_text", "text": content}],
    }


def _convert_messages_to_responses_input(
    messages: list[ChatMessage],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert runtime chat messages into Responses API input items."""
    instructions_parts: list[str] = []
    input_items: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.role or "user"
        content = _stringify_content(msg.content)

        if role == "system":
            if content:
                instructions_parts.append(content)
            continue

        if role == "tool":
            input_items.append({
                "type": "function_call_output",
                "call_id": msg.tool_call_id or "",
                "output": content,
            })
            continue

        if role == "assistant":
            if content:
                input_items.append(_responses_input_message("assistant", content))
            for tc in msg.tool_calls or []:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                args = fn.get("arguments", "{}")
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                input_items.append({
                    "type": "function_call",
                    "call_id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "arguments": args or "{}",
                })
            continue

        input_items.append(_responses_input_message(role, content))

    instructions = "\n\n".join(part for part in instructions_parts if part) or None
    return instructions, input_items


def _extract_responses_text(output: list[Any]) -> str:
    """Extract plain text from Responses API output items."""
    text_parts: list[str] = []
    for item in output:
        data = _response_item_to_dict(item)
        if data.get("type") == "message":
            for part in data.get("content", []) or []:
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                    text_parts.append(part.get("text", ""))
    return "".join(text_parts)


def _extract_responses_tool_calls(output: list[Any]) -> list[dict[str, Any]] | None:
    """Extract function calls from Responses API output items."""
    tool_calls: list[dict[str, Any]] = []
    for item in output:
        data = _response_item_to_dict(item)
        if data.get("type") != "function_call":
            continue
        args = data.get("arguments", "{}")
        tool_calls.append({
            "id": data.get("call_id") or data.get("id") or f"call_{len(tool_calls)}",
            "type": "function",
            "function": {
                "name": data.get("name", ""),
                "arguments": args,
            },
        })
    return tool_calls or None


def _parse_responses_usage(response: Any) -> TokenUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        usage = getattr(response, "output", None)
    if usage is None:
        return TokenUsage()

    prompt_tokens = getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0) or 0
    total_tokens = getattr(usage, "total_tokens", 0) or (prompt_tokens + completion_tokens)
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def _extract_usage_from_event_dict(data: dict[str, Any]) -> TokenUsage | None:
    """Extract token usage from a streaming event dict if present."""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
    completion_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
    total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens) or 0
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def _normalize_responses_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Convert chat.completions-style tools to Responses API tool definitions."""
    if not tools:
        return None

    normalized: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            normalized.append(tool)
            continue

        fn = tool.get("function")
        if isinstance(fn, dict):
            schema = _normalize_openai_function_schema(
                fn.get("parameters", {"type": "object", "properties": {}})
            )
            normalized.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": schema,
                # Responses API strict mode requires every property to be listed in
                # `required`, which does not match our tool schemas with optional
                # params. Keep schemas explicit but disable strict enforcement.
                "strict": False,
            })
        else:
            normalized.append(tool)

    return normalized or None


def _normalize_openai_function_schema(schema: Any) -> dict[str, Any]:
    """Make a function JSON schema acceptable to OpenAI Responses API."""
    if not isinstance(schema, dict):
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    normalized = _normalize_openai_schema_node(schema)
    if normalized.get("type") != "object":
        normalized = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
            "required": [],
        }
    normalized.setdefault("properties", {})
    normalized.setdefault("required", [])
    normalized["additionalProperties"] = False
    return normalized


def _normalize_openai_schema_node(node: Any) -> Any:
    """Recursively harden a JSON Schema node for OpenAI strict validation."""
    if isinstance(node, list):
        return [_normalize_openai_schema_node(item) for item in node]
    if not isinstance(node, dict):
        return node

    normalized = {
        key: _normalize_openai_schema_node(value)
        for key, value in node.items()
        if key not in {"title", "$schema", "examples", "default"}
    }

    node_type = normalized.get("type")
    if node_type == "object" or "properties" in normalized:
        normalized.setdefault("type", "object")
        normalized["properties"] = {
            key: _normalize_openai_schema_node(value)
            for key, value in (normalized.get("properties") or {}).items()
        }
        normalized["additionalProperties"] = False

    if node_type == "array" and "items" in normalized:
        normalized["items"] = _normalize_openai_schema_node(normalized["items"])

    return normalized


def _normalize_responses_tool_choice(tool_choice: str | dict | None) -> str | dict | None:
    """Convert chat.completions tool_choice to Responses API format."""
    if not isinstance(tool_choice, dict):
        return tool_choice

    if tool_choice.get("type") == "function":
        fn = tool_choice.get("function")
        if isinstance(fn, dict):
            return {
                "type": "function",
                "name": fn.get("name", ""),
            }
        if "name" in tool_choice:
            return {
                "type": "function",
                "name": tool_choice.get("name", ""),
            }
    return tool_choice


def _recover_from_error(
    exc: Exception,
    known_tools: list[str] | None = None,
) -> ChatResponse | None:
    """Recover tool calls from a provider API error (e.g. Groq tool_use_failed).

    Returns a synthetic ChatResponse or None if not recoverable.
    """
    error_body = _extract_error_body(exc)
    if not error_body:
        return None

    err = error_body.get("error", {})
    if err.get("code") != "tool_use_failed":
        return None

    failed = err.get("failed_generation", "")
    if not failed:
        return None

    tool_calls = _parse_tool_calls_from_text(failed, known_tools)
    if not tool_calls:
        return None

    content = _strip_tool_call_tags(failed)

    logger.warning(
        "Recovered %d tool call(s) from provider error: %s",
        len(tool_calls),
        [tc["function"]["name"] for tc in tool_calls],
    )
    return ChatResponse(
        content=content,
        model="recovered",
        finish_reason="tool_calls",
        usage=TokenUsage(),
        tool_calls=tool_calls,
        raw={"recovered_from": "tool_use_failed", "original": failed},
    )


def _recover_from_content(
    response: "ChatResponse",
    known_tools: list[str] | None = None,
) -> "ChatResponse":
    """Check if a successful response has tool calls hidden in text content.

    Some LLMs write tool calls as text instead of using the tool_calls
    field.  If the response has no tool_calls but the content contains
    parseable tool calls, we extract them.

    Returns the response as-is if no recovery is needed, or a new
    ChatResponse with extracted tool_calls.
    """
    if response.tool_calls:
        logger.debug(
            "recover_skip: already has %d tool_calls", len(response.tool_calls),
        )
        return response

    content = response.content or ""
    if not content:
        return response

    tool_calls = _parse_tool_calls_from_text(content, known_tools)
    if not tool_calls:
        return response

    clean_content = _extract_prose_from_mixed_content(content)

    if not clean_content.strip():
        clean_content = _strip_tool_call_tags(content)
        for tc in tool_calls:
            name = tc["function"]["name"]
            clean_content = clean_content.replace(f'"name": "{name}"', "")
        clean_content = re.sub(r'\{[^{}]*\}', '', clean_content).strip()
        clean_content = re.sub(r'\n{3,}', '\n\n', clean_content).strip()

    logger.warning(
        "Recovered %d tool call(s) from response content: %s",
        len(tool_calls),
        [tc["function"]["name"] for tc in tool_calls],
    )
    return ChatResponse(
        content=clean_content,
        model=response.model,
        finish_reason="tool_calls",
        usage=response.usage,
        tool_calls=tool_calls,
        raw=response.raw,
    )


def _extract_error_body(exc: Exception) -> dict | None:
    """Extract the JSON error body from an openai API error."""
    if hasattr(exc, "body") and isinstance(exc.body, dict):
        return exc.body

    msg = str(exc)
    start = msg.find("{")
    if start >= 0:
        try:
            return json.loads(msg[start:])
        except (json.JSONDecodeError, ValueError):
            pass

    return None


class OpenAICompatProvider(BaseLLMProvider):
    """OpenAI-compatible provider for any endpoint following the OpenAI API spec.

    Works out-of-the-box with: OpenAI, DeepSeek, Groq, Mistral, Together,
    Ollama, vLLM, LM Studio, and any other OpenAI-compatible endpoint.
    """

    BACKEND = "openai_compat"

    def __init__(
        self,
        provider_id: str,
        model: str,
        *,
        api_key: str = "",
        base_url: str | None = None,
        provider_hint: str | None = None,
        timeout: float | None = None,
        max_retries: int = 2,
        default_params: dict[str, Any] | None = None,
    ) -> None:
        resolved_url = resolve_base_url(provider_hint, base_url)
        if timeout is None:
            timeout = _LOCAL_PROVIDER_TIMEOUT if _is_local_provider(provider_hint) else 120.0
        super().__init__(
            provider_id=provider_id,
            model=model,
            api_key=api_key,
            base_url=resolved_url,
            timeout=timeout,
            max_retries=max_retries,
            default_params=default_params,
        )
        self.provider_hint = provider_hint

    def clone(self, *, provider_id_suffix: str = "") -> "OpenAICompatProvider":
        """Override to forward ``provider_hint``.

        Each clone gets its own ``AsyncOpenAI`` client (initialised
        lazily on first use), so sub-agents don't share the parent's
        httpx connection pool.
        """
        new_id = (
            f"{self.provider_id}:{provider_id_suffix}"
            if provider_id_suffix else self.provider_id
        )
        clone = type(self)(
            provider_id=new_id,
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            provider_hint=self.provider_hint,
            timeout=self.timeout,
            max_retries=self.max_retries,
            default_params=dict(self.default_params),
        )
        clone._is_clone = True  # type: ignore[attr-defined]
        return clone

    async def initialize(self) -> None:
        # The openai SDK defers a huge import tree (resources.beta.realtime
        # + chat.completions + responses) behind ``@cached_property`` on
        # the client. The FIRST access to ``client.chat`` or
        # ``client.responses`` triggers 1–3 s of sync imports that freeze
        # the asyncio loop - the watchdog catches this as a 2.3 s stall
        # on the first HTML turn. Warm the client off-loop now so all
        # subsequent requests hit already-loaded modules.
        import asyncio as _asyncio

        def _build_and_warm() -> Any:
            import openai
            client = openai.AsyncOpenAI(
                api_key=self.api_key or "not-needed",
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=self.max_retries,
            )
            # Touching the cached_property forces the underlying import
            # + ctor to run now - on this worker thread, not on the loop.
            _ = client.chat
            _ = client.chat.completions
            try:
                _ = client.responses
            except Exception:
                pass
            return client

        try:
            self._client = await _asyncio.to_thread(_build_and_warm)
        except ImportError as exc:
            raise ImportError(
                "openai package required: pip install openai"
            ) from exc

    def _ensure_client(self):
        """Return the warm client; rebuild if somehow None (crash recovery).

        Sync fallback - kept for legacy callers. Building an
        ``openai.AsyncOpenAI`` synchronously instantiates an
        ``httpx.AsyncClient`` whose ``__init__`` calls
        ``ssl.create_default_context()``. On Windows that loads the
        OS CA store and routinely takes 100-500ms - long enough to
        stall the loop and drop Socket.IO. Prefer
        :meth:`_ensure_client_async` from any async context.
        """
        if self._client is not None:
            return self._client
        import openai
        self._client = openai.AsyncOpenAI(
            api_key=self.api_key or "not-needed",
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )
        return self._client

    async def _ensure_client_async(self):
        """Async-safe variant: off-loads the client build.

        Fast path is a single attribute check so the per-call cost is
        the same as the sync version once warm. Cold path runs the
        full SSL/httpx init in a worker thread.
        """
        if self._client is not None:
            return self._client
        import asyncio as _asyncio
        self._client = await _asyncio.to_thread(self._ensure_client)
        return self._client

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ChatResponse:
        if _uses_openai_responses_api(self.provider_hint, self.base_url, self.model):
            return await self._chat_via_responses(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                stop=stop,
                tools=tools,
                tool_choice=tool_choice,
                response_format=response_format,
                extra=extra,
            )

        params = self._build_params(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            extra=extra,
        )

        known_tools = _extract_tool_names(tools) or getattr(self, "_known_tool_names", None)

        # Inject the per-request identity headers (run_id, agent_id, …)
        # if the agent loop set a RequestContext. The SDK forwards
        # ``extra_headers`` to the underlying httpx call. No-op when
        # the call is issued outside an ``agent_turn`` scope.
        _inject_digitorn_request_headers(self.api_key, params)

        max_retries = 3
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                client = await self._ensure_client_async()
                response = await client.chat.completions.create(**params)
                parsed = self._parse_response(response)
                return _recover_from_content(parsed, known_tools)
            except Exception as exc:
                last_exc = exc
                recovered = _recover_from_error(exc, known_tools)
                if recovered is not None:
                    return recovered
                if attempt < max_retries and _is_retriable(exc):
                    delay = min(2 ** attempt, 16)
                    logger.warning(
                        "LLM call to %s failed (attempt %d/%d), retrying in %ds: %s",
                        self.provider_hint, attempt + 1, max_retries, delay, exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise _enrich_error(exc, self.base_url, self.provider_hint)
        # LP6: if all retries exhausted without a single successful try OR
        # raise, surface the last exception instead of generic "Unreachable".
        if last_exc is not None:
            raise _enrich_error(last_exc, self.base_url, self.provider_hint)
        raise RuntimeError(
            f"LLM chat failed after {max_retries + 1} attempts (no exception captured)"
        )

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        if _uses_openai_responses_api(self.provider_hint, self.base_url, self.model):
            async for chunk in self._chat_stream_via_responses(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                stop=stop,
                tools=tools,
                tool_choice=tool_choice,
                extra=extra,
            ):
                yield chunk
            return

        params = self._build_params(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            tools=tools,
            tool_choice=tool_choice,
            extra=extra,
            stream=True,
        )

        # Inject the per-request identity headers (see chat() above).
        _inject_digitorn_request_headers(self.api_key, params)

        # DIAG: write the exact params sent to the provider to disk once
        # per call so tests can verify the daemon's reported token counts
        # against the real on-the-wire payload. Gated by env var so it's
        # zero-cost in production.
        if os.environ.get("DIGITORN_DIAG_WIRE_PAYLOAD"):
            try:
                import json as _json
                import time as _time
                from pathlib import Path as _Path
                diag_dir = _Path(os.environ.get("DIGITORN_DIAG_DIR", "/tmp"))
                diag_dir.mkdir(parents=True, exist_ok=True)
                path = diag_dir / f"wire_payload_{int(_time.time()*1000)}.json"
                path.write_text(_json.dumps({
                    "model": params.get("model"),
                    "messages": params.get("messages"),
                    "tools": params.get("tools"),
                }, ensure_ascii=False, default=str), encoding="utf-8")
            except Exception:
                pass

        try:
            client = await self._ensure_client_async()
            # Streaming: pass an explicit ``httpx.Timeout`` per-request
            # with a generous ``read`` so the stream survives extended
            # thinking pauses (Anthropic adaptive thinking can pause
            # 60-180 s between tokens). Default client-level scalar
            # ``timeout=self.timeout`` (120 s) was applied to read AND
            # connect, so the stream died mid-response under long
            # reasoning. Connect stays short (fail fast on dead
            # gateway). The bigger read also covers slow upstream
            # providers (Copilot first-token, gateway queue).
            import httpx as _httpx
            _stream_timeout = _httpx.Timeout(
                connect=30.0,
                read=600.0,
                write=30.0,
                pool=30.0,
            )
            stream = await client.chat.completions.create(
                **params, timeout=_stream_timeout,
            )
        except Exception as exc:
            raise _enrich_error(exc, self.base_url, self.provider_hint) from exc

        async for chunk in stream:
            # IMPORTANT: the provider emits a FINAL usage-only chunk with
            # choices=[] when stream_options.include_usage=True (OpenAI,
            # Ollama, DeepSeek all behave this way). Skipping chunks with
            # empty choices dropped that usage entirely and forced the
            # fallback estimator to run even when the provider had given
            # us the exact billed numbers. Emit a synthetic chunk carrying
            # just the usage so downstream receives it.
            if not chunk.choices:
                if hasattr(chunk, "usage") and chunk.usage:
                    usage = TokenUsage(
                        prompt_tokens=chunk.usage.prompt_tokens or 0,
                        completion_tokens=chunk.usage.completion_tokens or 0,
                        total_tokens=chunk.usage.total_tokens or 0,
                    )
                    if os.environ.get("DIGITORN_DIAG_WIRE_PAYLOAD"):
                        try:
                            import json as _json
                            import time as _time
                            from pathlib import Path as _Path
                            diag_dir = _Path(
                                os.environ.get("DIGITORN_DIAG_DIR", "/tmp"),
                            )
                            (diag_dir / f"wire_usage_{int(_time.time()*1000)}.json"
                             ).write_text(_json.dumps({
                                "prompt_tokens": usage.prompt_tokens,
                                "completion_tokens": usage.completion_tokens,
                                "total_tokens": usage.total_tokens,
                            }), encoding="utf-8")
                        except Exception:
                            pass
                    yield StreamChunk(delta="", finish_reason=None, usage=usage)
                continue
            delta = chunk.choices[0].delta
            finish = chunk.choices[0].finish_reason

            text = delta.content or ""
            usage = None
            if hasattr(chunk, "usage") and chunk.usage:
                usage = TokenUsage(
                    prompt_tokens=chunk.usage.prompt_tokens or 0,
                    completion_tokens=chunk.usage.completion_tokens or 0,
                    total_tokens=chunk.usage.total_tokens or 0,
                )

            # DeepSeek reasoning models emit reasoning_content in the
            # delta. Preserve empty strings - V4 thinking mode requires
            # the field to be replayed on every subsequent API call,
            # even when reasoning is empty for trivial turns. `or None`
            # would have collapsed `""` to None and the streaming
            # aggregator would lose the "saw thinking" signal.
            thinking = getattr(delta, "reasoning_content", None)

            sc = StreamChunk(delta=text, finish_reason=finish, usage=usage, thinking=thinking)

            if delta.tool_calls:
                tc_list = []
                for tc in delta.tool_calls:
                    fn = tc.function
                    name = (fn.name if fn else None) or ""
                    args = (fn.arguments if fn else None) or ""
                    tc_id = getattr(tc, "id", None) or ""
                    if not name and not args and not tc_id:
                        continue
                    tc_list.append({
                        "index": tc.index,
                        "id": tc_id,
                        "name": name,
                        "arguments": args,
                    })
                if tc_list:
                    sc.tool_calls = tc_list

            yield sc

    def get_info(self) -> ProviderInfo:
        ctx_window = _lookup_context_window(self.model)
        tool_use = _has_tool_use(self.provider_hint, self.model)
        # Vision capability is model-specific. Previously this was hard-
        # coded to True which caused the runtime to keep image_ref
        # content blocks in the payload for DeepSeek-chat (text-only);
        # DeepSeek rejected the turn silently, `message_done` fired with
        # zero assistant content, the user saw a ghost reply (BUG-046).
        model_low = (self.model or "").lower()
        vision = any(k in model_low for k in (
            "gpt-4o", "gpt-4-vision", "gpt-4-turbo", "gpt-4.1",
            "qwen-vl", "qwen2-vl", "qwen2.5-vl",
            "llava", "gemini", "pixtral", "-vision", "vision-",
            "claude-3", "claude-sonnet", "claude-opus", "claude-haiku",
            "moondream",
        ))
        return ProviderInfo(
            provider_id=self.provider_id,
            backend=self.BACKEND,
            model=self.model,
            capabilities=ProviderCapabilities(
                streaming=True,
                tool_use=tool_use,
                vision=vision,
                json_mode=True,
                system_message=True,
                max_context_window=ctx_window,
            ),
            extra={
                "base_url": self.base_url or "",
                "provider_hint": self.provider_hint or "",
            },
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
        self._client = None

    async def _chat_via_responses(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ChatResponse:
        instructions, input_items = _convert_messages_to_responses_input(messages)
        params = self._build_responses_params(
            input_items,
            instructions=instructions,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            extra=extra,
        )

        # Inject the per-request identity headers (see chat() above).
        _inject_digitorn_request_headers(self.api_key, params)

        max_retries = 3
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                client = await self._ensure_client_async()
                response = await client.responses.create(**params)
                return self._parse_responses_response(response)
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries and _is_retriable(exc):
                    delay = min(2 ** attempt, 16)
                    logger.warning(
                        "Responses API call to %s failed (attempt %d/%d), retrying in %ds: %s",
                        self.provider_hint, attempt + 1, max_retries, delay, exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise _enrich_error(exc, self.base_url, self.provider_hint)
        # LP6: surface the actual last exception, not "Unreachable"
        if last_exc is not None:
            raise _enrich_error(last_exc, self.base_url, self.provider_hint)
        raise RuntimeError(
            f"Responses API call failed after {max_retries + 1} attempts"
        )

    async def _chat_stream_via_responses(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        instructions, input_items = _convert_messages_to_responses_input(messages)
        params = self._build_responses_params(
            input_items,
            instructions=instructions,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            extra=extra,
        )
        params["stream"] = True

        # Inject the per-request identity headers (see chat() above).
        _inject_digitorn_request_headers(self.api_key, params)

        try:
            client = await self._ensure_client_async()
            stream = await client.responses.create(**params)
        except Exception as exc:
            raise _enrich_error(exc, self.base_url, self.provider_hint) from exc

        tool_indexes: dict[str, int] = {}
        pending_tools: dict[str, dict[str, Any]] = {}
        last_usage: TokenUsage | None = None
        # LP5: track tool call IDs already emitted to avoid duplication
        # at response.completed (which re-emits all tool calls).
        emitted_tool_call_ids: set[str] = set()

        async for event in stream:
            data = _event_to_dict(event)
            event_type = data.get("type", "")

            usage = _extract_usage_from_event_dict(data)
            if usage is not None:
                last_usage = usage

            if event_type == "response.output_text.delta":
                delta = data.get("delta", "") or ""
                if delta:
                    yield StreamChunk(delta=delta, usage=usage)
                continue

            if event_type in {"response.output_item.added", "response.output_item.done"}:
                item = _response_item_to_dict(data.get("item"))
                if item.get("type") == "function_call":
                    call_id = item.get("call_id") or item.get("id") or f"call_{len(tool_indexes)}"
                    if call_id not in tool_indexes:
                        tool_indexes[call_id] = len(tool_indexes)
                    pending = pending_tools.setdefault(call_id, {
                        "index": tool_indexes[call_id],
                        "id": call_id,
                        "name": item.get("name"),
                        "arguments": "",
                    })
                    if item.get("name"):
                        pending["name"] = item["name"]
                    if item.get("arguments"):
                        pending["arguments"] = item["arguments"]
                    emitted_tool_call_ids.add(call_id)
                    yield StreamChunk(
                        delta="",
                        usage=usage,
                        tool_calls=[dict(pending)],
                    )
                continue

            if event_type in {"response.function_call_arguments.delta", "response.output_item.delta"}:
                call_id = data.get("call_id") or data.get("item_id") or ""
                delta = data.get("delta") or data.get("arguments_delta") or ""
                if call_id and delta:
                    if call_id not in tool_indexes:
                        tool_indexes[call_id] = len(tool_indexes)
                    pending = pending_tools.setdefault(call_id, {
                        "index": tool_indexes[call_id],
                        "id": call_id,
                        "name": data.get("name"),
                        "arguments": "",
                    })
                    if data.get("name"):
                        pending["name"] = data["name"]
                    pending["arguments"] = pending.get("arguments", "") + delta
                    yield StreamChunk(
                        delta="",
                        usage=usage,
                        tool_calls=[{
                            "index": pending["index"],
                            "id": pending["id"],
                            "name": pending.get("name"),
                            "arguments": delta,
                        }],
                    )
                continue

            if event_type == "response.function_call_arguments.done":
                call_id = data.get("call_id") or data.get("item_id") or ""
                if call_id:
                    if call_id not in tool_indexes:
                        tool_indexes[call_id] = len(tool_indexes)
                    pending = pending_tools.setdefault(call_id, {
                        "index": tool_indexes[call_id],
                        "id": call_id,
                        "name": data.get("name"),
                        "arguments": "",
                    })
                    if data.get("name"):
                        pending["name"] = data["name"]
                    final_args = data.get("arguments") or pending.get("arguments", "")
                    pending["arguments"] = final_args
                    emitted_tool_call_ids.add(pending["id"])
                    yield StreamChunk(
                        delta="",
                        usage=usage,
                        tool_calls=[{
                            "index": pending["index"],
                            "id": pending["id"],
                            "name": pending.get("name"),
                            "arguments": final_args,
                        }],
                    )
                continue

            if event_type == "response.completed":
                response_obj = data.get("response")
                if response_obj is not None:
                    parsed = self._parse_responses_response(response_obj)
                    if parsed.tool_calls:
                        # LP5: skip tool_calls already emitted during the stream
                        # to avoid duplicate execution downstream.
                        new_calls = [
                            (idx, tc) for idx, tc in enumerate(parsed.tool_calls)
                            if tc.get("id") not in emitted_tool_call_ids
                        ]
                        if new_calls:
                            yield StreamChunk(
                                delta="",
                                finish_reason=parsed.finish_reason,
                                usage=parsed.usage,
                                tool_calls=[
                                    {
                                        "index": idx,
                                        "id": tc.get("id"),
                                        "name": tc.get("function", {}).get("name"),
                                        "arguments": tc.get("function", {}).get("arguments"),
                                    }
                                    for idx, tc in new_calls
                                ],
                            )
                        else:
                            yield StreamChunk(
                                delta="",
                                finish_reason=parsed.finish_reason,
                                usage=parsed.usage,
                            )
                    else:
                        yield StreamChunk(
                            delta="",
                            finish_reason=parsed.finish_reason,
                            usage=parsed.usage,
                        )
                else:
                    yield StreamChunk(delta="", finish_reason="stop", usage=last_usage)
                continue


    def _build_params(
        self,
        messages: list[ChatMessage],
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build OpenAI API params."""
        merged = self._merge_params(**kwargs)
        extra = merged.pop("extra", None) or {}

        api_messages: list[dict[str, Any]] = []
        for msg in messages:
            # Tolerate raw dicts mixed in (callers occasionally prepend a
            # plain ``{"role": "system", "content": ...}`` to a list of
            # ChatMessage dataclasses). Normalise to the dataclass shape
            # so the attribute access below works uniformly.
            if isinstance(msg, dict):
                msg = ChatMessage(
                    role=msg.get("role", ""),
                    content=msg.get("content", ""),
                    name=msg.get("name"),
                    tool_call_id=msg.get("tool_call_id"),
                    tool_calls=msg.get("tool_calls"),
                    reasoning_content=msg.get("reasoning_content"),
                )
            content = msg.content
            # Convert multimodal blocks to OpenAI format
            if isinstance(content, list):
                blocks = []
                for block in content:
                    if not isinstance(block, dict):
                        blocks.append({"type": "text", "text": str(block)})
                    elif block.get("type") == "text":
                        blocks.append(block)
                    elif block.get("type") == "image_url":
                        blocks.append(block)  # Already OpenAI format
                    elif block.get("type") == "image":
                        # Anthropic format → convert to OpenAI
                        source = block.get("source", {})
                        if source.get("type") == "base64":
                            mime = source.get("media_type", "image/png")
                            data = source.get("data", "")
                            blocks.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{data}"},
                            })
                        elif source.get("type") == "url":
                            blocks.append({
                                "type": "image_url",
                                "image_url": {"url": source.get("url", "")},
                            })
                    elif block.get("type") == "image_ref":
                        alt = block.get("alt_text", "image")
                        blocks.append({"type": "text", "text": f"[Image: {alt}]"})
                    else:
                        blocks.append(block)
                content = blocks

            m: dict[str, Any] = {"role": msg.role, "content": content}
            if msg.name:
                m["name"] = msg.name
            # LP4: tool_call_id is ONLY valid on role="tool" messages.
            # OpenAI rejects it on assistant/user/system messages.
            if msg.tool_call_id and msg.role == "tool":
                m["tool_call_id"] = msg.tool_call_id
            if msg.tool_calls:
                m["tool_calls"] = msg.tool_calls
            # DeepSeek V4 thinking mode requires reasoning_content on
            # EVERY assistant message replay. Include it for any
            # assistant message targeting a DeepSeek model. Empty
            # string is the safe default - V4 accepts it as "no
            # reasoning for this turn", whereas a missing field
            # raises 400 "The reasoning_content in the thinking mode
            # must be passed back to the API."
            #
            # Why default to "" instead of conditional: the streaming
            # path or DB reload may drop the field for older messages
            # in the conversation history (model didn't emit reasoning
            # on that turn, or the field was created before the V4
            # rollout). Without a default, replaying that history
            # 400's the API. Empty string is forwards-compat with
            # V3 (the field is silently ignored) and required by V4.
            if msg.role == "assistant":
                stored = getattr(msg, "reasoning_content", None)
                model_lower = (self.model or "").lower()
                if "deepseek" in model_lower:
                    m["reasoning_content"] = stored if stored is not None else ""
                elif stored is not None:
                    m["reasoning_content"] = stored
            api_messages.append(m)

        params: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
        }

        if stream:
            params["stream"] = True
            params["stream_options"] = {"include_usage": True}

        if merged.get("temperature") is not None:
            params["temperature"] = merged["temperature"]
        if merged.get("max_tokens") is not None:
            params["max_tokens"] = _clamp_max_tokens(
                self.model, int(merged["max_tokens"]),
            )
        if merged.get("top_p") is not None:
            params["top_p"] = merged["top_p"]
        if merged.get("stop"):
            params["stop"] = merged["stop"]
        if merged.get("tools"):
            # Defense in depth: normalise the tool schemas to OpenAI strict
            # shape right before they go on the wire. The proactive pass in
            # context_builder.build_direct_tools covers the standard path,
            # but any custom builder (MCP late-binding, sub-agent override,
            # external caller) that hands us tools is normalised here too.
            #
            # Hot path: ``normalize_strict_tools`` returns False when the
            # list was already seen (id()-keyed cache) -- one set lookup,
            # ~100 ns, no recursion. We only run the heavier validator on
            # the first call for each list, so a session of 50 turns pays
            # the validation cost ONCE.
            from digitorn.core.runtime.strict_schema import (
                assert_strict_tools,
                normalize_strict_tools,
            )
            tools_for_api = merged["tools"]
            if normalize_strict_tools(tools_for_api):
                # Fresh list - validate after normalisation. If anything
                # remains it means the walker missed a JSON-Schema shape
                # and the next 400 from the gateway will mention the same
                # path. Log loud so we can extend the walker.
                remaining = assert_strict_tools(tools_for_api)
                if remaining:
                    for tool_name, path, reason in remaining[:5]:
                        logger.error(
                            "strict_schema_violation_after_normalize tool=%s "
                            "path=%s reason=%s",
                            tool_name, ".".join(path) or "<root>", reason,
                        )
            params["tools"] = tools_for_api
        if merged.get("tool_choice"):
            params["tool_choice"] = merged["tool_choice"]
        if merged.get("response_format"):
            params["response_format"] = merged["response_format"]

        params.update(extra)
        return params

    def _build_responses_params(
        self,
        input_items: list[dict[str, Any]],
        *,
        instructions: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        merged = self._merge_params(**kwargs)
        extra = merged.pop("extra", None) or {}

        params: dict[str, Any] = {
            "model": self.model,
            "input": input_items,
        }

        if instructions:
            params["instructions"] = instructions
        if merged.get("temperature") is not None:
            params["temperature"] = merged["temperature"]
        if merged.get("max_tokens") is not None:
            params["max_output_tokens"] = _clamp_max_tokens(
                self.model, int(merged["max_tokens"]),
            )
        if merged.get("top_p") is not None:
            params["top_p"] = merged["top_p"]
        if merged.get("stop"):
            params["stop"] = merged["stop"]
        if merged.get("tools"):
            params["tools"] = _normalize_responses_tools(merged["tools"])
        if merged.get("tool_choice"):
            params["tool_choice"] = _normalize_responses_tool_choice(merged["tool_choice"])

        response_format = merged.get("response_format")
        if isinstance(response_format, dict):
            if response_format.get("type") == "json_schema":
                params["text"] = {"format": response_format}
            elif response_format.get("type") == "json_object":
                params["text"] = {"format": {"type": "json_object"}}

        params.update(extra)
        return params

    def _parse_response(self, response: Any) -> ChatResponse:
        """Parse OpenAI API response into ChatResponse."""
        choice = response.choices[0] if response.choices else None

        content = choice.message.content or "" if choice else ""
        finish_reason = choice.finish_reason if choice else None

        # DeepSeek V4 thinking mode emits `reasoning_content` on message.
        # Must be preserved and replayed on next turn (API enforces it).
        # Use the raw value - empty string is meaningful (V4 emitted an
        # empty reasoning block but the field is still required on the
        # next API call). `or None` would have collapsed it to None and
        # downstream would have dropped the field entirely.
        reasoning_content = None
        if choice and hasattr(choice.message, "reasoning_content"):
            reasoning_content = getattr(choice.message, "reasoning_content", None)

        tool_calls = None
        if choice and choice.message.tool_calls:
            tool_calls = []
            for tc in choice.message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                })

        usage = TokenUsage()
        if response.usage:
            usage = TokenUsage(
                prompt_tokens=response.usage.prompt_tokens or 0,
                completion_tokens=response.usage.completion_tokens or 0,
                total_tokens=response.usage.total_tokens or 0,
            )

        return ChatResponse(
            content=content,
            model=response.model,
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=tool_calls,
            raw={"id": response.id, "object": response.object},
            reasoning_content=reasoning_content,
        )

    def _parse_responses_response(self, response: Any) -> ChatResponse:
        output = list(getattr(response, "output", []) or [])
        tool_calls = _extract_responses_tool_calls(output)
        content = _extract_responses_text(output) or getattr(response, "output_text", "") or ""
        finish_reason = "tool_calls" if tool_calls else getattr(response, "status", None)
        usage = _parse_responses_usage(response)

        return ChatResponse(
            content=content,
            model=getattr(response, "model", self.model),
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=tool_calls,
            raw={
                "id": getattr(response, "id", ""),
                "object": getattr(response, "object", "response"),
                "status": getattr(response, "status", None),
            },
        )

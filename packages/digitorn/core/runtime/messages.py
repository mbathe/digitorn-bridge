"""Message building - serialization, extraction, reasoning synthesis."""

from __future__ import annotations

import json
import re
from typing import Any

from digitorn.core.runtime.types import AgentContext


def _fix_win_backslashes(s: str) -> str:
    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        return re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def _strip_think_blocks(text: str) -> str:
    """Remove DeepSeek-R1 / reasoner `<think>...</think>` blocks from"""
    if not text or "<think>" not in text.lower():
        return text
    return _THINK_BLOCK_RE.sub("", text).strip()


def _sanitize_orphan_tool_calls(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Strip `tool_calls` that have no matching `tool` response"""
    if not messages:
        return messages
    needs_fix = False
    for m in messages:
        if (
            m.get("role") == "assistant"
            and isinstance(m.get("tool_calls"), list)
            and m["tool_calls"]
        ):
            needs_fix = True
            break
        if m.get("role") == "tool":
            needs_fix = True
            break
    if not needs_fix:
        return messages

    out: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant" and isinstance(m.get("tool_calls"), list) and m["tool_calls"]:
            responded: set[str] = set()
            for later in messages[i + 1:]:
                if later.get("role") == "tool":
                    tcid = later.get("tool_call_id")
                    if isinstance(tcid, str) and tcid:
                        responded.add(tcid)
                    continue
                if later.get("role") in ("user", "system"):
                    break
            kept = [
                c for c in m["tool_calls"]
                if isinstance(c, dict) and c.get("id") in responded
            ]
            content = m.get("content")
            has_text = bool(
                content if isinstance(content, str) else content
            )
            if not kept and not has_text:
                continue
            fixed = dict(m)
            if kept:
                fixed["tool_calls"] = kept
            else:
                fixed.pop("tool_calls", None)
            out.append(fixed)
        elif role == "tool":
            tcid = m.get("tool_call_id")
            valid = False
            if isinstance(tcid, str) and tcid:
                for prev in reversed(out):
                    if prev.get("role") == "assistant":
                        calls = prev.get("tool_calls") or []
                        if any(
                            isinstance(c, dict) and c.get("id") == tcid
                            for c in calls
                        ):
                            valid = True
                        break
            if valid:
                out.append(m)
        else:
            out.append(m)
    return out


def to_chat_messages(messages: list[dict[str, Any]]) -> list:
    """Convert dict messages to ChatMessage objects for the LLM provider."""
    from digitorn.modules.llm_provider.providers.base import ChatMessage

    messages = _sanitize_orphan_tool_calls(messages)
    result = []
    for msg in messages:
        content = msg.get("content", "")
        role = msg.get("role", "user")
        if role == "assistant" and isinstance(content, str):
            content = _strip_think_blocks(content)
        if isinstance(content, list):
            has_images = any(
                isinstance(p, dict) and p.get("type") in ("image", "image_url", "image_ref")
                for p in content
            )
            if has_images:
                # Drop text blocks where text is None to avoid downstream crashes; keep image blocks intact.
                content = [
                    p for p in content
                    if isinstance(p, dict) and (
                        p.get("type") in ("image", "image_url", "image_ref")
                        or (p.get("type") == "text" and p.get("text") is not None)
                    )
                ]
            else:
                text_parts = [
                    str(p.get("text", "") or "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                content = " ".join(t for t in text_parts if t) or ""
        # Final safety: ensure content is never None for str-typed messages
        if content is None:
            content = ""
        result.append(ChatMessage(
            role=msg.get("role", "user"),
            content=content,
            name=msg.get("name"),
            tool_call_id=msg.get("tool_call_id"),
            tool_calls=msg.get("tool_calls"),
            reasoning_content=msg.get("reasoning_content"),
        ))
    return result


_CONTENT_WRAPPER_RE = re.compile(
    r'^\s*content\s*:\s*"(?P<body>(?:\\.|[^"\\])*)"\s*(?:,\s*(?:tool_calls|tool_use)\s*:.*)?\s*$',
    re.IGNORECASE | re.DOTALL,
)


def _strip_content_wrapper(text: str) -> str:
    """Strip the `content: "…"` wrapper some local models (qwen, etc.) emit."""
    if not text or "content" not in text.lower():
        return text
    m = _CONTENT_WRAPPER_RE.match(text)
    if not m:
        return text
    body = m.group("body")
    try:
        return json.loads(f'"{body}"')
    except Exception:
        return body.encode("utf-8", "replace").decode("unicode_escape", "replace")


def extract_content(response: Any) -> str:
    """Extract text content from a provider response."""
    if isinstance(response, dict):
        raw = response.get("content", "") or ""
    elif hasattr(response, "content"):
        raw = response.content or ""
    else:
        raw = str(response)
    return _strip_content_wrapper(raw)


def extract_tool_calls(response: Any) -> list[dict[str, Any]]:
    """Extract tool_calls from a provider response."""
    if isinstance(response, dict):
        return response.get("tool_calls") or []
    if hasattr(response, "tool_calls"):
        return response.tool_calls or []
    return []


def build_assistant_message(
    content: str, tool_calls: list[dict[str, Any]],
    reasoning_content: str | None = None,
) -> dict[str, Any]:
    """Build an assistant message with serialized tool_calls."""
    msg: dict[str, Any] = {"role": "assistant"}
    if content:
        msg["content"] = content
    if tool_calls:
        serialized = []
        for tc in tool_calls:
            tc_copy = dict(tc)
            if not tc_copy.get("type"):
                tc_copy["type"] = "function"
            fn = tc_copy.get("function", {})
            if isinstance(fn.get("arguments"), dict):
                fn = dict(fn)
                fn["arguments"] = json.dumps(fn["arguments"], ensure_ascii=False)
                tc_copy["function"] = fn
            serialized.append(tc_copy)
        msg["tool_calls"] = serialized
    if reasoning_content is not None:
        msg["reasoning_content"] = reasoning_content
    return msg


def serialize_result(result: Any) -> str:
    """Serialize a tool result for the messages list."""
    if isinstance(result, str):
        return result
    if hasattr(result, "data") and hasattr(result, "success"):
        if result.success:
            return json.dumps(result.data, default=str, ensure_ascii=False)
        # Include BOTH error AND data so the LLM has full context
        payload: dict[str, Any] = {"error": result.error}
        if result.data:
            if isinstance(result.data, dict):
                payload.update(result.data)
            else:
                payload["data"] = result.data
        return json.dumps(payload, default=str, ensure_ascii=False)
    try:
        return json.dumps(result, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        try:
            return str(result)
        except Exception:
            return f"<unserializable result: {type(result).__name__}>"


def max_tool_result_chars(ctx: AgentContext) -> int:
    """Max chars for a single tool result (~50% of context window)."""
    cc = ctx.context_config
    budget_tokens = (cc.max_tokens - cc.output_reserved) // 2
    return max(budget_tokens * 4, 4000)


def truncate_tool_result(
    text: str, max_chars: int, tool_name: str,
) -> str:
    """Truncate a tool result to max_chars, preserving structure."""
    guidance_budget = 500
    data_budget = max_chars - guidance_budget

    try:
        data = json.loads(text)
        if isinstance(data, list) and len(data) > 10:
            return _truncate_json_list(data, data_budget, tool_name)
        if isinstance(data, dict) and "entries" in data and isinstance(data["entries"], list):
            return _truncate_json_entries(data, data_budget, tool_name)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    cut = max(data_budget, 1000)
    return text[:cut] + (
        f"\n\nRESULT TRUNCATED: showing first {cut} of {len(text)} characters "
        f"from {tool_name}. The full result was too large for the context window.\n"
        f"To get more specific results, use a narrower query or filter.\n"
        f"Do NOT guess or invent content you haven't seen. "
        f"Only report what is shown above."
    )


def parse_tool_args(raw: str) -> dict[str, Any]:
    """Parse tool arguments from a string, handling common LLM quirks."""
    s = raw.strip()

    try:
        parsed = json.loads(_fix_win_backslashes(s))
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, json.JSONDecodeError):
        pass

    start = s.find("{")
    if start >= 0:
        return _extract_first_json_object(s, start) or {"raw": raw}

    return {"raw": raw}


def synthesize_reasoning(tool_calls: list[dict[str, Any]]) -> str:
    """Synthesize a human-readable summary from tool calls."""
    if not tool_calls:
        return ""

    parts: list[str] = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        name = fn.get("name", "")
        args = _ensure_dict_args(fn.get("arguments", {}))
        parts.append(_describe_tool_call(name, args))

    return " → ".join(parts)


def _truncation_guidance(tool_name: str, shown: int, total: int) -> str:
    return (
        f"\n\n RESULT TRUNCATED: showing {shown} of {total} results "
        f"from {tool_name}. The full result was too large for the context window.\n"
        f"To see more results, you can:\n"
        f"- Use a more specific pattern or filter (e.g. '*.py', 'src/**')\n"
        f"- Search for a specific filename or keyword instead of listing everything\n"
        f"- Ask the user to narrow their request\n"
        f"Do NOT guess or invent results you haven't seen. "
        f"Only report what is shown above."
    )


def _truncate_json_list(
    data: list, budget: int, tool_name: str,
) -> str:
    total = len(data)
    lo, hi = 1, total
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(json.dumps(data[:mid], default=str, ensure_ascii=False)) <= budget:
            lo = mid
        else:
            hi = mid - 1
    truncated = json.dumps(data[:lo], default=str, ensure_ascii=False)
    return truncated + _truncation_guidance(tool_name, lo, total)


def _truncate_json_entries(
    data: dict, budget: int, tool_name: str,
) -> str:
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        return json.dumps(data, default=str, ensure_ascii=False)[:budget]
    total = len(entries)
    lo, hi = 1, total
    while lo < hi:
        mid = (lo + hi + 1) // 2
        data["entries"] = entries[:mid]
        if len(json.dumps(data, default=str, ensure_ascii=False)) <= budget:
            lo = mid
        else:
            hi = mid - 1
    data["entries"] = entries[:lo]
    truncated = json.dumps(data, default=str, ensure_ascii=False)
    return truncated + _truncation_guidance(tool_name, lo, total)


def _extract_first_json_object(s: str, start: int) -> dict[str, Any] | None:
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(_fix_win_backslashes(s[start:i + 1]))
                    if isinstance(parsed, dict):
                        return parsed
                except (ValueError, json.JSONDecodeError):
                    pass
                break
    return None


def _ensure_dict_args(args: Any) -> dict[str, Any]:
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(_fix_win_backslashes(args))
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return {}


def _describe_tool_call(name: str, args: dict[str, Any]) -> str:
    if "memory" in name:
        return _describe_memory_call(name, args)

    if "read" in name:
        return f"Reading {args.get('path', args.get('name', '?'))}"
    if "ls" in name or "list" in name:
        return f"Listing {args.get('path', args.get('directory', '?'))}"
    if "find" in name:
        return f"Searching for {args.get('pattern', '?')}"
    if "grep" in name:
        return f"Grepping {args.get('pattern', '?')}"
    if "write" in name:
        return f"Writing {args.get('path', '?')}"

    if name == "execute_tool":
        inner = args.get("name", "?")
        inner_params = _ensure_dict_args(args.get("params", {}))
        synth = _describe_tool_call(inner, inner_params)
        return synth if synth != f"Calling {inner.replace('__', '.').replace('_', ' ')}" else f"Executing {inner}"
    if name == "search_tools":
        return f'Searching tools: "{args.get("query", "?")}"'
    if name == "run_parallel":
        return f"Running {len(args.get('actions', []))} actions in parallel"

    clean_name = name.replace("__", ".").replace("_", " ")
    return f"Calling {clean_name}"


def _describe_memory_call(name: str, args: dict[str, Any]) -> str:
    action = name.split(".")[-1] if "." in name else name.split("__")[-1] if "__" in name else name

    descriptions = {
        "set_goal": lambda: f"Setting goal: {args.get('goal', '?')}",
        "remember": lambda: f"Remembering: {args.get('content', '?')[:60]}",
        "task_create": lambda: f"Adding task: {args.get('subject', '?')}",
    }

    if action in descriptions:
        return descriptions[action]()

    if action == "task_update":
        status = args.get("status", "?")
        tid = args.get("taskId", "?")
        icons = {"completed": "done", "done": "done", "in_progress": "run", "blocked": "block"}
        icon = icons.get(status, "")
        return f"{icon} Task {tid} -> {status}"

    return f"Memory: {action}"

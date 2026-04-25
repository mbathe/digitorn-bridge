from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def _fix_win_backslashes(s: str) -> str:
    """Fix unescaped Windows backslashes in JSON strings from LLMs."""
    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        return re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)


def _parse_yaml_params(raw: str) -> dict[str, Any]:
    """Parse simple YAML-like params that LLMs produce instead of JSON.

    Handles: ``key: "value"`` and ``key: value`` (single or multi-key).
    Returns empty dict if parsing fails completely.
    """
    result: dict[str, Any] = {}
    for m in re.finditer(
        r'([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(?:"([^"]*)"'
        r"|'([^']*)'|(\S+))",
        raw,
    ):
        key = m.group(1)
        value = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else m.group(4)
        )
        if value and re.match(r'^-?\d+$', value):
            result[key] = int(value)
        elif value and re.match(r'^-?\d+\.\d+$', value):
            result[key] = float(value)
        elif value in ('true', 'True'):
            result[key] = True
        elif value in ('false', 'False'):
            result[key] = False
        else:
            result[key] = value
    return result


_INLINE_TOOL_PATTERNS = [
    re.compile(
        r'tool_calls?\s*:\s*\[([a-zA-Z0-9_.]+(?:__[a-zA-Z0-9_]+)?)'
        r'(?:\((\{.*?\})\))?\]',
        re.DOTALL | re.IGNORECASE,
    ),
    re.compile(
        r'<tool_call>\s*([a-zA-Z0-9_.]+(?:__[a-zA-Z0-9_]+)?)'
        r'(?:\((.*?)\))?\s*</tool_call>',
        re.DOTALL | re.IGNORECASE,
    ),
    re.compile(
        r'\{\s*"tool"\s*:\s*"([a-zA-Z0-9_.]+(?:__[a-zA-Z0-9_]+)*)"\s*'
        r'(?:,\s*"params"\s*:\s*(\{.*?\}))?\s*\}',
        re.DOTALL,
    ),
    # Same YAML-ish ``content: ...\ntool_calls: [NAME]`` format. The
    # body between the two anchors is bounded to <= 64 lines to prevent
    # catastrophic backtracking on adversarial content that has many
    # ``content:`` lines without a matching ``tool_calls:`` (measured
    # at 12s+ on 250 KB before the bound). The previous unbounded
    # ``.*?`` with MULTILINE+DOTALL made ``^`` match every line start
    # and made the engine retry every pair.
    re.compile(
        r'^content\s*:(?:[^\n]*\n){0,64}?[^\n]*?'
        r'^tool_calls?\s*:\s*\[([a-zA-Z0-9_.]+(?:__[a-zA-Z0-9_]+)?)'
        r'(?:\((\{[^{}]{0,4096}\})\))?\]',
        re.MULTILINE | re.IGNORECASE,
    ),
    # Bulletised ``tool_calls:\n  name: X\n  params: {...}``. The
    # param body is bounded so pathological nested braces can't force
    # exponential scanning.
    re.compile(
        r'tool_calls?\s*:\s*(?:\n\s*)?(?:[•\-\*]\s*)?name\s*:\s*'
        r'([a-zA-Z0-9_.]+(?:__[a-zA-Z0-9_]+)*)\s*'
        r'(?:params?\s*:\s*(\{[^{}]{0,4096}\}|[^\n]{0,1024}))?$',
        re.MULTILINE | re.IGNORECASE,
    ),
    re.compile(
        r'(?:^|\n)\s*(?:[•\-\*]\s*)?name\s*:\s*'
        r'([a-zA-Z0-9_.]+(?:__[a-zA-Z0-9_]+)*)\s+'
        r'params?\s*:\s*(\{[^{}]{0,4096}\}|[^\n]{0,1024})\s*$',
        re.MULTILINE | re.IGNORECASE,
    ),
]


_JSON_TOOL_CALL_RE = re.compile(
    # Tolerate missing closing </tool_call> tag — some models (qwen2.5)
    # emit the opening tag + JSON then stop at max_tokens before the
    # closing tag, or just omit it entirely.
    r'<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|\Z|(?=<tool_call>))',
    re.DOTALL,
)


# qwen2.5 patterns:
#   `tool_calls: [ {"name": "...", "arguments": {...}} ]`
#   `tool_calls: [<tool_call>{"name": "...", "arguments": {...}}</tool_call>]`
#   `tool_calls: [<tool_call>{...}` (unterminated — model hit max_tokens)
#
# We match everything from `tool_calls: [` up to end-of-content (or a
# closing `]` if present). The inside is scanned for call objects — we
# strip any `<tool_call>` / `</tool_call>` tags since they're noise.
_TOOL_CALLS_ARRAY_RE = re.compile(
    r'tool_calls?\s*:\s*\[(.*?)(?:\]|\Z)',
    re.DOTALL | re.IGNORECASE,
)
# qwen sometimes wraps calls in a Python-style function call:
#   run_parallel(actions=[{"name":"X","params":{...}}, ...])
# or variants like parallel_tool_use(...). We capture the array payload.
_RUN_PARALLEL_RE = re.compile(
    r'\b(?:run_parallel|parallel_tool_use|parallel_tools|batch_tools)\s*\(\s*'
    r'(?:actions|tools|calls)?\s*=?\s*(\[.*?\])\s*\)?',
    re.DOTALL | re.IGNORECASE,
)
_SINGLE_CALL_IN_ARRAY_RE = re.compile(
    # Accept "arguments", "params", or "args" — qwen alternates.
    r'\{\s*"name"\s*:\s*"([^"]+)"\s*,\s*'
    r'"(?:arguments|params|args)"\s*:\s*(\{.*?\})\s*\}',
    re.DOTALL,
)


def _extract_inline_tool_calls(
    content: str,
) -> tuple[str, list[dict]] | None:
    """Detect tool calls embedded in text content by the LLM.

    Returns (clean_text_before_tool_call, synthetic_tool_calls) or None.
    """
    # ── NEW: unified call detector (handles all known formats via
    # balanced-bracket parsing + registered format extractors). ──
    try:
        from digitorn.core.runtime.call_detector import extract_all_calls
        from digitorn.core.runtime.tool_names import to_fqn
        result = extract_all_calls(content)
        if result is not None:
            text_before, raw_calls = result
            synthetic = [
                {
                    "id": f"inline_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": to_fqn(name),
                        "arguments": json.dumps(args) if isinstance(args, dict) else "{}",
                    },
                }
                for name, args in raw_calls
            ]
            if synthetic:
                logger.info(
                    "call_detector extracted %d call(s) from content",
                    len(synthetic),
                )
                return text_before, synthetic
    except Exception as exc:
        logger.debug("call_detector failed, falling back to legacy regex: %s", exc)

    # ── Legacy regex fallbacks (kept in case detector misses something) ──
    rp_match = _RUN_PARALLEL_RE.search(content)
    if rp_match:
        body = rp_match.group(1)
        body = re.sub(r'</?tool_call>', '', body, flags=re.IGNORECASE)
        calls = []
        for call_m in _SINGLE_CALL_IN_ARRAY_RE.finditer(body):
            name = call_m.group(1)
            try:
                args = json.loads(_fix_win_backslashes(call_m.group(2)))
            except (json.JSONDecodeError, ValueError):
                args = {}
            if name:
                from digitorn.core.runtime.tool_names import to_fqn
                resolved = to_fqn(name)
                calls.append({
                    "id": f"inline_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": resolved,
                        "arguments": json.dumps(args),
                    },
                })
        if calls:
            text_before = content[:rp_match.start()].strip()
            if text_before.lower().startswith("content:"):
                nl = text_before.find("\n")
                text_before = text_before[nl + 1:] if nl >= 0 else ""
            logger.info(
                "Extracted %d run_parallel call(s) from qwen-style content",
                len(calls),
            )
            return text_before, calls

    # qwen2.5 patterns:
    #   `tool_calls: [{"name":..., "arguments":...}]`
    #   `tool_calls: [<tool_call>{...}</tool_call>]`  (hybrid)
    #   `tool_calls: [<tool_call>{...}` (unterminated)
    # Checked BEFORE the <tool_call> pattern since qwen often wraps
    # the call array with inner tags.
    arr_match = _TOOL_CALLS_ARRAY_RE.search(content)
    if arr_match:
        # Strip <tool_call>/</tool_call> noise from the captured body
        # before scanning for call objects.
        body = arr_match.group(1)
        body = re.sub(r'</?tool_call>', '', body, flags=re.IGNORECASE)
        calls = []
        for call_m in _SINGLE_CALL_IN_ARRAY_RE.finditer(body):
            name = call_m.group(1)
            try:
                args = json.loads(_fix_win_backslashes(call_m.group(2)))
            except (json.JSONDecodeError, ValueError):
                args = {}
            if name:
                from digitorn.core.runtime.tool_names import to_fqn
                resolved = to_fqn(name)
                calls.append({
                    "id": f"inline_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": resolved,
                        "arguments": json.dumps(args),
                    },
                })
        if calls:
            text_before = content[:arr_match.start()].strip()
            # Strip leading `content: "..."` noise qwen emits
            if text_before.lower().startswith("content:"):
                nl = text_before.find("\n")
                text_before = text_before[nl + 1:] if nl >= 0 else ""
            logger.info("Extracted %d tool_calls-array call(s) from qwen-style content", len(calls))
            return text_before, calls

    json_matches = list(_JSON_TOOL_CALL_RE.finditer(content))
    if json_matches:
        calls = []
        for m in json_matches:
            try:
                obj = json.loads(_fix_win_backslashes(m.group(1)))
                name = obj.get("name", "")
                args = obj.get("arguments", obj.get("params", obj.get("args", {})))
                if isinstance(args, str):
                    try:
                        args = json.loads(_fix_win_backslashes(args))
                    except (json.JSONDecodeError, ValueError):
                        args = {}
                if name:
                    from digitorn.core.runtime.tool_names import to_fqn
                    resolved = to_fqn(name)
                    # RT17: warn when extraction produces an unresolvable name —
                    # the runtime will fail later but here we surface it.
                    if resolved == name and "." not in name:
                        logger.warning(
                            "extract_tool: inline call name '%s' did not resolve to FQN",
                            name,
                        )
                    calls.append({
                        "id": f"inline_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": resolved,
                            "arguments": json.dumps(args) if isinstance(args, dict) else "{}",
                        },
                    })
            except (json.JSONDecodeError, ValueError):
                pass

        if calls:
            text_before = content[:json_matches[0].start()].strip()
            logger.info("Extracted %d JSON tool_call(s) from content", len(calls))
            return text_before, calls

    for pattern in _INLINE_TOOL_PATTERNS:
        match = pattern.search(content)
        if match is None:
            continue

        tool_name_raw = match.group(1).strip()
        args_raw = match.group(2).strip() if len(match.groups()) >= 2 and match.group(2) else None

        tool_args: dict = {}
        if args_raw:
            try:
                tool_args = json.loads(_fix_win_backslashes(args_raw))
            except (json.JSONDecodeError, ValueError):
                tool_args = _parse_yaml_params(args_raw)

        from digitorn.core.runtime.tool_names import to_fqn
        tool_name = to_fqn(tool_name_raw)

        text_before = content[: match.start()].strip()
        text_before = re.sub(
            r'^\s*content\s*:\s*',
            "",
            text_before,
            flags=re.IGNORECASE,
        ).strip()
        if (text_before.startswith('"') and text_before.endswith('"')) or \
           (text_before.startswith("'") and text_before.endswith("'")):
            text_before = text_before[1:-1].strip()

        synthetic_call = {
            "id": f"inline_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(tool_args),
            },
        }

        logger.info(
            "Detected inline tool call in content: '%s'", tool_name,
        )
        return text_before, [synthetic_call]

    return None



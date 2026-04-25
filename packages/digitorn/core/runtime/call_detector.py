"""Tool-call detector — robust, format-agnostic, balanced-bracket aware.

Replaces a dozen ad-hoc regexes with one module that:

1. ``find_balanced_close(text, start)`` — generic matcher that finds
   the matching ``]`` / ``}`` / ``)`` for an opening bracket at *start*.
   Respects strings (`"..."`, `'...'`) and escape sequences so a quoted
   bracket inside a value doesn't confuse the match.

2. ``parse_call_object(json_text)`` — recognises every common shape of
   a "call" JSON object:
        {"name": X, "arguments": {...}}
        {"name": X, "params": {...}}
        {"name": X, "args": {...}}
        {"tool": X, "params": {...}}
   Returns ``(name, args_dict)`` or ``None``.

3. Format extractors — each is a small function that returns a list of
   ``(name, args)`` tuples. Registered formats so far:
        - ``<tool_call>{...}</tool_call>`` — open/close tags (also
          tolerates missing close)
        - ``tool_calls: [ {...} , {...} ]`` — JSON array after a label
        - ``run_parallel(actions=[...])`` — Python-style function call
        - Bare ``{...}`` object containing a ``name`` + args key
        - ``工具调用:`` + JSON / tag (CJK label variants)

4. ``extract_all_calls(content)`` — runs every extractor, returns the
   first non-empty result plus the text preceding it (to preserve any
   explanatory prose the model emitted before the calls).

Adding a new format = one function + a line in the registry.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)


# ── Bracket matchers (strings + escapes aware) ─────────────────────

_BRACKET_PAIRS = {"[": "]", "{": "}", "(": ")"}


def find_balanced_close(text: str, start: int) -> int:
    """Return the index of the matching closer for the bracket at *start*.

    Returns -1 if no match is found.

    Respects string literals ``"..."`` / ``'...'`` and ``\\``-escapes
    so brackets inside strings don't count toward the balance.
    """
    if start >= len(text):
        return -1
    opener = text[start]
    closer = _BRACKET_PAIRS.get(opener)
    if closer is None:
        return -1

    depth = 0
    i = start
    in_string: str | None = None  # holds the quote char when inside a string
    n = len(text)

    while i < n:
        c = text[i]
        if in_string:
            if c == "\\":
                i += 2
                continue
            if c == in_string:
                in_string = None
        else:
            if c == '"' or c == "'":
                in_string = c
            elif c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1  # unbalanced


def try_parse_json_relaxed(text: str) -> Any:
    """Try strict JSON then several gentle repairs.

    - Strips trailing commas
    - Fixes bare Windows backslashes (``C:\\Users`` → ``C:\\\\Users``)
    - Strips surrounding whitespace / outer parens
    """
    text = text.strip()
    if not text:
        return None
    # Try strict first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Remove trailing commas before closers
    cleaned = re.sub(r",\s*([\]}])", r"\1", text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Double up unescaped backslashes (common Windows-path issue)
    fixed = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', cleaned)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        return None


# ── Call-object parser ──────────────────────────────────────────────

_NAME_KEYS = ("name", "tool", "function", "action", "tool_name")
_ARGS_KEYS = ("arguments", "params", "args", "parameters", "input")


def parse_call_object(obj: Any) -> tuple[str, dict] | None:
    """Extract (name, args) from a parsed JSON-like dict.

    Accepts the full cross-section of call shapes emitted by LLMs.
    """
    if not isinstance(obj, dict):
        return None
    name = None
    for k in _NAME_KEYS:
        if k in obj and isinstance(obj[k], str):
            name = obj[k]
            break
    if not name:
        return None
    args: dict = {}
    for k in _ARGS_KEYS:
        if k in obj:
            v = obj[k]
            if isinstance(v, dict):
                args = v
                break
            if isinstance(v, str):
                # Some providers stringify the args
                parsed = try_parse_json_relaxed(v)
                if isinstance(parsed, dict):
                    args = parsed
                    break
    return (name, args)


# ── Helpers to scan for structured blocks ──────────────────────────

def _strip_call_noise(body: str) -> str:
    """Remove inner ``<tool_call>``/``</tool_call>`` tags that some
    models nest inside their arrays."""
    return re.sub(r"</?tool_call\s*/?>", "", body, flags=re.IGNORECASE)


def _iter_json_objects(body: str) -> Iterable[str]:
    """Yield each balanced ``{...}`` substring in *body*."""
    i = 0
    while i < len(body):
        if body[i] == "{":
            close = find_balanced_close(body, i)
            if close == -1:
                # Unterminated — emit everything up to end so callers can
                # still try a lenient parse on the partial object.
                yield body[i:]
                return
            yield body[i : close + 1]
            i = close + 1
        else:
            i += 1


_LABEL_MARKERS = (
    "tool_calls:", "tool_call:",
    "工具调用:", "工具调用 :", "工具呼叫:",
    "ツール呼び出し:",
    "herramientas:",
)


def _trim_preceding_label(content: str, pos: int) -> int:
    """Walk back from *pos* skipping whitespace + any known label
    (CJK/English/Spanish) so text_before doesn't include the marker."""
    i = pos
    while i > 0 and content[i - 1] in " \t\n\r":
        i -= 1
    for lbl in _LABEL_MARKERS:
        if content[:i].lower().endswith(lbl.lower()):
            i -= len(lbl)
            while i > 0 and content[i - 1] in " \t\n\r":
                i -= 1
            break
    return i


# ── Format extractors (return list of (name, args_dict)) ────────────

def extract_tool_call_tags(content: str) -> tuple[int, list[tuple[str, dict]]] | None:
    """Matches ``<tool_call>{...}</tool_call>`` and unterminated variants.

    Also strips any preceding label marker (``工具调用:``, etc.) from
    the reported start position so text_before is clean.
    """
    start = content.lower().find("<tool_call>")
    if start == -1:
        return None
    # Trim any preceding label so text_before doesn't include it
    report_start = _trim_preceding_label(content, start)
    # Find all tag-enclosed JSON objects
    calls: list[tuple[str, dict]] = []
    cursor = start
    while True:
        tag_start = content.lower().find("<tool_call>", cursor)
        if tag_start == -1:
            break
        json_start = tag_start + len("<tool_call>")
        # Skip whitespace
        while json_start < len(content) and content[json_start] in " \t\n\r":
            json_start += 1
        if json_start >= len(content) or content[json_start] != "{":
            cursor = json_start
            continue
        close = find_balanced_close(content, json_start)
        if close == -1:
            obj_text = content[json_start:]
        else:
            obj_text = content[json_start : close + 1]
        parsed = try_parse_json_relaxed(obj_text)
        call = parse_call_object(parsed)
        if call:
            calls.append(call)
        cursor = close + 1 if close != -1 else len(content)
    if calls:
        return (report_start, calls)
    return None


def extract_tool_calls_label(content: str) -> tuple[int, list[tuple[str, dict]]] | None:
    """Matches ``tool_calls: [ ... ]`` / ``tool_call: [...]`` labels.

    Also covers the CJK/ES variants via the aliased labels list.
    """
    labels = [
        "tool_calls:",
        "tool_call:",
        "工具调用:",
        "工具调用 :",
        "工具呼叫:",
        "ツール呼び出し:",
        "herramientas:",
    ]
    low = content.lower()
    best_pos = -1
    best_label_len = 0
    for lbl in labels:
        i = low.find(lbl.lower())
        if i != -1 and (best_pos == -1 or i < best_pos):
            best_pos = i
            best_label_len = len(lbl)
    if best_pos == -1:
        return None
    after = content.find("[", best_pos + best_label_len)
    if after == -1:
        return None
    close = find_balanced_close(content, after)
    body = content[after + 1 : close] if close != -1 else content[after + 1:]
    body = _strip_call_noise(body)
    calls = []
    for obj_text in _iter_json_objects(body):
        parsed = try_parse_json_relaxed(obj_text)
        call = parse_call_object(parsed)
        if call:
            calls.append(call)
    if calls:
        return (best_pos, calls)
    return None


def extract_python_call(content: str) -> tuple[int, list[tuple[str, dict]]] | None:
    """Matches Python-style function calls that wrap an array of tool calls.

    Recognised prefixes: ``run_parallel``, ``parallel_tool_use``,
    ``parallel_tools``, ``batch_tools``, ``execute_tools``, ``invoke_tools``.
    """
    fn_names = (
        "run_parallel", "parallel_tool_use", "parallel_tools",
        "batch_tools", "execute_tools", "invoke_tools",
    )
    low = content.lower()
    best_pos = -1
    for name in fn_names:
        i = low.find(name.lower())
        if i == -1:
            continue
        # Must be followed (possibly with whitespace) by `(`
        j = i + len(name)
        while j < len(content) and content[j] in " \t":
            j += 1
        if j < len(content) and content[j] == "(":
            if best_pos == -1 or i < best_pos:
                best_pos = i
    if best_pos == -1:
        return None
    # Find the `[` inside the `(...)`
    paren = content.find("(", best_pos)
    if paren == -1:
        return None
    bracket = content.find("[", paren, paren + 200)
    if bracket == -1:
        # Maybe just a single dict: run_parallel({...})
        brace = content.find("{", paren, paren + 200)
        if brace == -1:
            return None
        close = find_balanced_close(content, brace)
        obj_text = content[brace : close + 1] if close != -1 else content[brace:]
        parsed = try_parse_json_relaxed(obj_text)
        call = parse_call_object(parsed)
        return (best_pos, [call]) if call else None
    close = find_balanced_close(content, bracket)
    body = content[bracket + 1 : close] if close != -1 else content[bracket + 1:]
    body = _strip_call_noise(body)
    calls = []
    for obj_text in _iter_json_objects(body):
        parsed = try_parse_json_relaxed(obj_text)
        call = parse_call_object(parsed)
        if call:
            calls.append(call)
    if calls:
        return (best_pos, calls)
    return None


def extract_bare_object(content: str) -> tuple[int, list[tuple[str, dict]]] | None:
    """Matches a bare ``{"name": "X", "arguments": {...}}`` with no wrapper.

    Last-resort extractor — only fires when stricter formats didn't
    match. Scans every ``{...}`` in the content for one that has a
    ``name`` + args shape.
    """
    for i, ch in enumerate(content):
        if ch != "{":
            continue
        close = find_balanced_close(content, i)
        if close == -1:
            continue
        obj_text = content[i : close + 1]
        parsed = try_parse_json_relaxed(obj_text)
        call = parse_call_object(parsed)
        if call:
            return (i, [call])
    return None


# ── Registry + master entry point ───────────────────────────────────

# Order matters: more specific formats first so their text_before is
# preserved, then fallbacks.
_EXTRACTORS: list[Callable[[str], tuple[int, list[tuple[str, dict]]] | None]] = [
    extract_python_call,
    extract_tool_calls_label,
    extract_tool_call_tags,
    extract_bare_object,
]


def extract_all_calls(
    content: str,
) -> tuple[str, list[tuple[str, dict]]] | None:
    """Run every registered extractor and return the earliest match.

    Returns (text_before_match, calls_list) or None.
    """
    best: tuple[str, int, list[tuple[str, dict]]] | None = None  # (label, pos, calls)
    for extractor in _EXTRACTORS:
        try:
            result = extractor(content)
        except Exception as exc:
            logger.debug("extractor %s crashed: %s", extractor.__name__, exc)
            continue
        if result is None:
            continue
        pos, calls = result
        if not calls:
            continue
        if best is None or pos < best[1]:
            best = (extractor.__name__, pos, calls)

    if best is None:
        return None

    _, pos, calls = best
    text_before = content[:pos].rstrip()
    # Strip leading `content: "..."` wrapper if the model used it
    if text_before.lower().lstrip().startswith('content:'):
        # Find the first opening quote and strip up through it
        quote_idx = text_before.find('"')
        if quote_idx != -1:
            text_before = text_before[quote_idx + 1:]
        # Drop trailing closing quote
        text_before = text_before.rstrip()
        if text_before.endswith('"'):
            text_before = text_before[:-1].rstrip()

    return text_before, calls

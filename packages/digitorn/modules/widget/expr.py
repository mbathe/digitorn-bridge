"""Widget expression evaluator — server-side substitution of ``{{...}}``.

Implements the closed-set binding language from the widgets spec
(§6 "Langage de binding"), evaluated against a scope dict
``{form: ..., state: ..., ctx: ..., item: ..., session: ..., app: ...}``.

Supports:

- Variable lookup with dotted paths and indexing: ``{{a.b.c}}``, ``{{l[0]}}``
- Filter pipeline: ``{{x | upper | truncate(40)}}``
- Comparisons: ``{{a == b}}``, ``{{a != b}}``, ``{{a > b}}``, ``{{a >= b}}``
- Logic: ``{{a && b}}``, ``{{a || b}}``, ``{{!a}}``
- Ternary: ``{{a ? "yes" : "no"}}``
- ``is empty`` / ``is not empty``
- Literals: ``"text"``, ``'text'``, ``42``, ``3.14``, ``true``, ``false``, ``null``

The 24 closed-set filters from the spec are implemented; unknown
filters raise ``ExprError`` with a clear message so the compiler
(or the runtime substitution) can surface an error.

The evaluator is **deliberately minimal** — no loops, no
assignments, no function calls beyond the closed filter set. That
keeps it sandboxed and reasoning-friendly.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from typing import Any, Callable


# ─────────────────────────── errors ────────────────────────────


class ExprError(Exception):
    """Raised when an expression cannot be evaluated."""


# ─────────────────────────── filters ────────────────────────────


def _f_upper(v, *args):  # noqa: D401, ANN001
    return str(v).upper() if v is not None else ""


def _f_lower(v, *args):
    return str(v).lower() if v is not None else ""


def _f_title(v, *args):
    return str(v).title() if v is not None else ""


def _f_truncate(v, n: Any = 40, *_):
    s = "" if v is None else str(v)
    n = int(n)
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def _f_default(v, fallback: Any = "", *_):
    if v is None or v == "":
        return fallback
    return v


def _f_length(v, *_):
    if v is None:
        return 0
    try:
        return len(v)
    except TypeError:
        return 0


def _f_date(v, fmt: Any = "YYYY-MM-DD", *_):
    if v is None:
        return ""
    try:
        if isinstance(v, (int, float)):
            dt = datetime.fromtimestamp(v)
        else:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return str(v)
    fmt = (
        str(fmt)
        .replace("YYYY", "%Y")
        .replace("MM", "%m")
        .replace("DD", "%d")
        .replace("HH", "%H")
        .replace("mm", "%M")
        .replace("ss", "%S")
    )
    return dt.strftime(fmt)


def _f_relative_time(v, *_):
    if v is None:
        return ""
    try:
        if isinstance(v, (int, float)):
            dt = datetime.fromtimestamp(v)
        else:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return str(v)
    delta = datetime.now() - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _f_money(v, currency: Any = "USD", *_):
    if v is None:
        return ""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    symbols = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}
    sym = symbols.get(str(currency).upper(), str(currency).upper() + " ")
    return f"{sym}{n:,.2f}"


def _f_number(v, decimals: Any = 0, *_):
    if v is None:
        return ""
    try:
        n = float(v)
        return f"{n:,.{int(decimals)}f}"
    except (TypeError, ValueError):
        return str(v)


def _f_percent(v, *_):
    if v is None:
        return ""
    try:
        return f"{float(v) * 100:.0f}%"
    except (TypeError, ValueError):
        return str(v)


def _f_json(v, *_):
    try:
        return json.dumps(v, default=str)
    except Exception:
        return str(v)


def _f_filter(v, key: Any, value: Any = None, *_):
    """Filter a list of dicts by ``item[key] == value``."""
    if not isinstance(v, list):
        return v
    return [it for it in v if isinstance(it, dict) and it.get(str(key)) == value]


def _f_map(v, key: Any, *_):
    if not isinstance(v, list):
        return v
    k = str(key)
    return [it.get(k) if isinstance(it, dict) else None for it in v]


def _f_pluck(v, key: Any, *_):
    return _f_map(v, key)


def _f_join(v, sep: Any = ", ", *_):
    if not isinstance(v, list):
        return str(v) if v is not None else ""
    return str(sep).join(str(x) for x in v)


def _f_first(v, *_):
    if isinstance(v, list) and v:
        return v[0]
    return None


def _f_last(v, *_):
    if isinstance(v, list) and v:
        return v[-1]
    return None


def _f_sort(v, key: Any = None, *_):
    if not isinstance(v, list):
        return v
    if key is None:
        try:
            return sorted(v)
        except TypeError:
            return v
    k = str(key)
    return sorted(
        v,
        key=lambda x: (x.get(k) if isinstance(x, dict) else x),
    )


def _f_reverse(v, *_):
    if isinstance(v, list):
        return list(reversed(v))
    if isinstance(v, str):
        return v[::-1]
    return v


def _f_slice(v, a: Any = 0, b: Any = None, *_):
    if not isinstance(v, (list, str)):
        return v
    return v[int(a) : int(b) if b is not None else None]


def _f_replace(v, old: Any, new: Any = "", *_):
    return str(v).replace(str(old), str(new)) if v is not None else ""


def _f_markdown(v, *_):
    # Markdown rendering happens client-side; this is a passthrough
    # so the evaluator doesn't error on the filter name.
    return v


def _f_plus_days(v, n: Any = 0, *_):
    if v is None:
        return ""
    try:
        if isinstance(v, (int, float)):
            dt = datetime.fromtimestamp(v)
        else:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return str(v)
    return (dt + timedelta(days=int(n))).date().isoformat()


def _f_minus_days(v, n: Any = 0, *_):
    return _f_plus_days(v, -int(n))


# Soft aliases used in the spec examples — passthrough so the
# evaluator doesn't error if the YAML references a custom semantic
# filter the client interprets locally.
def _f_passthrough(v, *_):
    return v


FILTERS: dict[str, Callable[..., Any]] = {
    "upper": _f_upper,
    "lower": _f_lower,
    "title": _f_title,
    "truncate": _f_truncate,
    "default": _f_default,
    "length": _f_length,
    "date": _f_date,
    "relative_time": _f_relative_time,
    "money": _f_money,
    "number": _f_number,
    "percent": _f_percent,
    "json": _f_json,
    "filter": _f_filter,
    "map": _f_map,
    "pluck": _f_pluck,
    "join": _f_join,
    "first": _f_first,
    "last": _f_last,
    "sort": _f_sort,
    "reverse": _f_reverse,
    "slice": _f_slice,
    "replace": _f_replace,
    "markdown": _f_markdown,
    "plus_days": _f_plus_days,
    "minus_days": _f_minus_days,
    # Aliases / soft passthrough for spec examples
    "filter_search": _f_passthrough,
    "source_icon": _f_passthrough,
    "tree_icon": _f_passthrough,
    "kind_color": _f_passthrough,
    "status_color": _f_passthrough,
    "sev_color": _f_passthrough,
}


# ─────────────────────────── lookup ────────────────────────────


_INDEX_RE = re.compile(r"\[(\d+)\]")


def _lookup(path: str, scopes: dict[str, Any]) -> Any:
    """Resolve ``a.b.c[0].d`` against the scope dict."""
    if not path:
        return None

    # Literal handling
    s = path.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if s in ("null", "None"):
        return None
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        pass

    # Walk the dotted path
    parts = re.split(r"\.", s)
    if not parts:
        return None
    head = parts[0]

    # Indexing on the head
    head_indexes: list[int] = []
    head_match = _INDEX_RE.findall(head)
    if head_match:
        head_indexes = [int(i) for i in head_match]
        head = _INDEX_RE.sub("", head)

    cursor: Any = scopes.get(head)
    for idx in head_indexes:
        if isinstance(cursor, (list, tuple)) and 0 <= idx < len(cursor):
            cursor = cursor[idx]
        else:
            return None

    for part in parts[1:]:
        idxs = [int(i) for i in _INDEX_RE.findall(part)]
        key = _INDEX_RE.sub("", part)
        if isinstance(cursor, dict):
            cursor = cursor.get(key)
        elif hasattr(cursor, key):
            cursor = getattr(cursor, key)
        else:
            return None
        for idx in idxs:
            if isinstance(cursor, (list, tuple)) and 0 <= idx < len(cursor):
                cursor = cursor[idx]
            else:
                return None
    return cursor


# ─────────────────────────── parser ────────────────────────────


def _parse_filter_args(args_text: str, scopes: dict[str, Any]) -> list[Any]:
    if not args_text:
        return []
    out: list[Any] = []
    depth = 0
    cur = ""
    in_str = False
    quote = ""
    for ch in args_text:
        if in_str:
            if ch == quote:
                in_str = False
            cur += ch
        elif ch in ('"', "'"):
            in_str = True
            quote = ch
            cur += ch
        elif ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            out.append(_lookup(cur.strip(), scopes))
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(_lookup(cur.strip(), scopes))
    return out


def _apply_filter(value: Any, filter_text: str, scopes: dict[str, Any]) -> Any:
    filter_text = filter_text.strip()
    if "(" in filter_text:
        name, rest = filter_text.split("(", 1)
        rest = rest.rsplit(")", 1)[0]
        args = _parse_filter_args(rest, scopes)
    else:
        name = filter_text
        args = []
    name = name.strip()
    fn = FILTERS.get(name)
    if fn is None:
        raise ExprError(f"unknown filter {name!r}")
    return fn(value, *args)


def _eval_term(text: str, scopes: dict[str, Any]) -> Any:
    """Evaluate one term (lookup + filter pipeline)."""
    parts = _split_top("|", text)
    head = parts[0].strip()
    if head.startswith("!"):
        rest = head[1:].strip()
        value = _eval_atom(rest, scopes)
        return not value
    value = _eval_atom(head, scopes)
    for f in parts[1:]:
        value = _apply_filter(value, f, scopes)
    return value


def _eval_atom(text: str, scopes: dict[str, Any]) -> Any:
    text = text.strip()
    if not text:
        return None
    if text.startswith("(") and text.endswith(")"):
        return _eval_expr(text[1:-1], scopes)
    return _lookup(text, scopes)


def _split_top(sep: str, text: str) -> list[str]:
    """Split ``text`` on ``sep`` only at the top parenthesis level."""
    out: list[str] = []
    cur = ""
    depth = 0
    in_str = False
    quote = ""
    i = 0
    L = len(sep)
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == quote:
                in_str = False
            cur += ch
            i += 1
            continue
        if ch in ('"', "'"):
            in_str = True
            quote = ch
            cur += ch
            i += 1
            continue
        if ch == "(":
            depth += 1
            cur += ch
            i += 1
            continue
        if ch == ")":
            depth -= 1
            cur += ch
            i += 1
            continue
        if depth == 0 and text[i : i + L] == sep:
            out.append(cur)
            cur = ""
            i += L
            continue
        cur += ch
        i += 1
    out.append(cur)
    return out


def _eval_expr(text: str, scopes: dict[str, Any]) -> Any:
    """Evaluate one full expression — handles ternary / logic / compare."""
    text = text.strip()
    if not text:
        return None

    # Ternary  a ? b : c
    if "?" in text:
        head, tail = _split_top("?", text)[0], _split_top("?", text)[1:]
        if tail:
            rest = "?".join(tail)
            true_branch, false_branch = _split_top(":", rest)[0], ":".join(_split_top(":", rest)[1:])
            cond = _eval_expr(head, scopes)
            return _eval_expr(true_branch, scopes) if cond else _eval_expr(false_branch, scopes)

    # Logic: ||
    parts = _split_top("||", text)
    if len(parts) > 1:
        for p in parts:
            v = _eval_expr(p, scopes)
            if v:
                return v
        return False

    parts = _split_top("&&", text)
    if len(parts) > 1:
        result: Any = True
        for p in parts:
            result = _eval_expr(p, scopes)
            if not result:
                return result
        return result

    # ``is empty`` / ``is not empty``
    if " is empty" in text:
        left = text.split(" is empty", 1)[0]
        v = _eval_expr(left, scopes)
        return v in (None, "", [], {}, 0)
    if " is not empty" in text:
        left = text.split(" is not empty", 1)[0]
        v = _eval_expr(left, scopes)
        return v not in (None, "", [], {}, 0)

    # Comparisons (single-pass; left-to-right)
    for op, py_op in (
        ("==", lambda a, b: a == b),
        ("!=", lambda a, b: a != b),
        (">=", lambda a, b: (a or 0) >= (b or 0)),
        ("<=", lambda a, b: (a or 0) <= (b or 0)),
        (">", lambda a, b: (a or 0) > (b or 0)),
        ("<", lambda a, b: (a or 0) < (b or 0)),
    ):
        sides = _split_top(op, text)
        if len(sides) == 2:
            l = _eval_expr(sides[0], scopes)
            r = _eval_expr(sides[1], scopes)
            return py_op(l, r)

    return _eval_term(text, scopes)


# ─────────────────────────── public API ─────────────────────────


_TOKEN_RE = re.compile(r"\{\{([^{}]+)\}\}")


def evaluate(text: str, scopes: dict[str, Any]) -> Any:
    """Evaluate one expression text (without the surrounding ``{{ }}``)."""
    return _eval_expr(text, scopes)


def substitute(text: str, scopes: dict[str, Any]) -> Any:
    """Substitute ``{{...}}`` tokens inside a string.

    Special case: when the entire string is a single ``{{...}}``
    token, return the resolved value verbatim (preserving its type).
    Otherwise interpolate as a string.
    """
    if not isinstance(text, str):
        return text
    matches = list(_TOKEN_RE.finditer(text))
    if not matches:
        return text

    if len(matches) == 1 and matches[0].group(0) == text.strip():
        try:
            return evaluate(matches[0].group(1), scopes)
        except ExprError:
            return text

    def _repl(m: re.Match) -> str:
        try:
            v = evaluate(m.group(1), scopes)
        except ExprError:
            return m.group(0)
        if v is None:
            return ""
        if isinstance(v, (dict, list)):
            return json.dumps(v, default=str)
        return str(v)

    return _TOKEN_RE.sub(_repl, text)


def substitute_tree(node: Any, scopes: dict[str, Any]) -> Any:
    """Recursively walk a dict/list tree and substitute every string leaf."""
    if isinstance(node, str):
        return substitute(node, scopes)
    if isinstance(node, list):
        return [substitute_tree(x, scopes) for x in node]
    if isinstance(node, dict):
        return {k: substitute_tree(v, scopes) for k, v in node.items()}
    return node

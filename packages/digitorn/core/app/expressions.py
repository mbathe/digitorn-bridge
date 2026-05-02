"""Compile-time expression linter for ``when:`` and ``expr:`` clauses.

The runtime evaluates these clauses with ``eval()`` against a runtime
namespace. Phase 4 adds a SYNTACTIC check at compile time so typos
surface before deploy:

  - Unbalanced parens
  - Operator typos (``coantains``, ``=>``)
  - Dangling operators (``a ==``)
  - Malformed identifier paths (``a..b``, ``.a``)
  - Unterminated strings

Phase 5 (after the flow runtime ships) will add identifier-vs-namespace
validation by passing in per-context allowed root names.

Public API::

    parse_expression(expr)          -> Node (AST root)
    validate_expression(expr, ctx)  -> list[str]   (error messages)
    collect_identifiers(node)       -> list[str]   (referenced paths)

The dialect is intentionally a strict subset of Python so the runtime
``eval()`` path keeps working unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional


# ─── Tokens ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Tok:
    kind: str
    value: str
    pos: int


_BOOL_OPS = {"and", "or"}
_UNARY_OPS = {"not"}
_COMPARATORS = {"==", "!=", "<", ">", "<=", ">="}
_MEMBERSHIP = {"contains", "in"}
_KEYWORDS = {"true", "false", "null", "default"} | _BOOL_OPS | _UNARY_OPS | _MEMBERSHIP


def _tokenize(expr: str) -> tuple[list[_Tok], list[str]]:
    """Tokenize the expression. Returns (tokens, errors)."""
    tokens: list[_Tok] = []
    errors: list[str] = []
    i = 0
    n = len(expr)

    while i < n:
        c = expr[i]
        # Whitespace
        if c.isspace():
            i += 1
            continue
        # Strings
        if c in ("'", '"'):
            quote = c
            j = i + 1
            while j < n and expr[j] != quote:
                # Allow escaped quotes
                if expr[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                j += 1
            if j >= n:
                errors.append(
                    f"unterminated string starting at column {i + 1}"
                )
                break
            tokens.append(_Tok("STRING", expr[i + 1 : j], i))
            i = j + 1
            continue
        # Numbers
        if c.isdigit() or (c == "-" and i + 1 < n and expr[i + 1].isdigit()):
            j = i + 1
            while j < n and (expr[j].isdigit() or expr[j] == "."):
                j += 1
            tokens.append(_Tok("NUMBER", expr[i:j], i))
            i = j
            continue
        # Identifiers (paths)
        if c.isalpha() or c == "_":
            j = i + 1
            while j < n and (expr[j].isalnum() or expr[j] in "._[]"):
                j += 1
            raw = expr[i:j]
            shape_err = _check_identifier_shape(raw, i)
            if shape_err:
                errors.append(shape_err)
            kind = "KEYWORD" if raw in _KEYWORDS else "IDENT"
            tokens.append(_Tok(kind, raw, i))
            i = j
            continue
        # Two-char operators
        if i + 1 < n and expr[i : i + 2] in _COMPARATORS:
            tokens.append(_Tok("OP", expr[i : i + 2], i))
            i += 2
            continue
        # Single-char operators
        if c in "<>":
            tokens.append(_Tok("OP", c, i))
            i += 1
            continue
        if c == "(":
            tokens.append(_Tok("LPAREN", c, i))
            i += 1
            continue
        if c == ")":
            tokens.append(_Tok("RPAREN", c, i))
            i += 1
            continue
        if c == ",":
            tokens.append(_Tok("COMMA", c, i))
            i += 1
            continue
        # Standalone =, ! - common typos that need clear errors
        if c == "=" or c == "!":
            errors.append(
                f"unexpected '{c}' at column {i + 1}. Did you mean '==' or '!='?"
            )
            i += 1
            continue

        errors.append(f"unexpected character {c!r} at column {i + 1}")
        i += 1

    return tokens, errors


def _check_identifier_shape(raw: str, col: int) -> Optional[str]:
    """Reject malformed identifier paths like ``a..b``, ``.a``, ``a.``."""
    if raw.startswith("."):
        return f"identifier '{raw}' at column {col + 1} starts with a dot"
    if raw.endswith("."):
        return f"identifier '{raw}' at column {col + 1} ends with a dot"
    if ".." in raw:
        return f"identifier '{raw}' at column {col + 1} contains '..'"
    return None


# ─── AST ───────────────────────────────────────────────────────────


@dataclass
class Node:
    kind: str  # 'binop', 'unop', 'literal', 'ident', 'keyword'
    value: object | None = None
    children: list["Node"] = field(default_factory=list)


# ─── Parser (recursive descent) ────────────────────────────────────


class _Parser:
    """Pratt-ish recursive descent. Precedence:

         or  <  and  <  not  <  comparators / membership  <  primary
    """

    def __init__(self, tokens: list[_Tok]) -> None:
        self.tokens = tokens
        self.i = 0
        self.errors: list[str] = []

    def _peek(self) -> Optional[_Tok]:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _eat(self) -> _Tok:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def _expect(self, kind: str) -> Optional[_Tok]:
        tok = self._peek()
        if tok is None or tok.kind != kind:
            return None
        return self._eat()

    def parse(self) -> Optional[Node]:
        node = self._parse_or()
        if self.i < len(self.tokens):
            tok = self.tokens[self.i]
            self.errors.append(
                f"unexpected token {tok.value!r} at column {tok.pos + 1}"
            )
        return node

    # or
    def _parse_or(self) -> Optional[Node]:
        left = self._parse_and()
        while True:
            tok = self._peek()
            if tok is None or not (tok.kind == "KEYWORD" and tok.value == "or"):
                return left
            self._eat()
            right = self._parse_and()
            if right is None:
                return left
            left = Node(kind="binop", value="or", children=[left, right]) if left else right

    # and
    def _parse_and(self) -> Optional[Node]:
        left = self._parse_not()
        while True:
            tok = self._peek()
            if tok is None or not (tok.kind == "KEYWORD" and tok.value == "and"):
                return left
            self._eat()
            right = self._parse_not()
            if right is None:
                return left
            left = Node(kind="binop", value="and", children=[left, right]) if left else right

    # not
    def _parse_not(self) -> Optional[Node]:
        tok = self._peek()
        if tok is not None and tok.kind == "KEYWORD" and tok.value == "not":
            self._eat()
            inner = self._parse_not()
            if inner is None:
                self.errors.append("dangling 'not' with no operand")
                return None
            return Node(kind="unop", value="not", children=[inner])
        return self._parse_comparison()

    # ident OP literal | ident contains literal | primary alone
    def _parse_comparison(self) -> Optional[Node]:
        left = self._parse_primary()
        if left is None:
            return None
        tok = self._peek()
        if tok is None:
            return left
        if tok.kind == "OP" and tok.value in _COMPARATORS:
            op = self._eat()
            right = self._parse_primary()
            if right is None:
                self.errors.append(
                    f"missing right operand after '{op.value}' at column {op.pos + 1}"
                )
                return left
            return Node(kind="binop", value=op.value, children=[left, right])
        if tok.kind == "KEYWORD" and tok.value in _MEMBERSHIP:
            op = self._eat()
            right = self._parse_primary()
            if right is None:
                self.errors.append(
                    f"missing right operand after '{op.value}' at column {op.pos + 1}"
                )
                return left
            return Node(kind="binop", value=op.value, children=[left, right])
        return left

    def _parse_primary(self) -> Optional[Node]:
        tok = self._peek()
        if tok is None:
            self.errors.append("expression ends mid-clause")
            return None
        if tok.kind == "LPAREN":
            self._eat()
            inner = self._parse_or()
            close = self._peek()
            if close is None or close.kind != "RPAREN":
                self.errors.append(
                    f"unbalanced parenthesis: missing ')' for '(' at column {tok.pos + 1}"
                )
                return inner
            self._eat()
            return inner
        if tok.kind == "RPAREN":
            self.errors.append(
                f"unbalanced parenthesis: stray ')' at column {tok.pos + 1}"
            )
            self._eat()
            return None
        if tok.kind == "STRING":
            self._eat()
            return Node(kind="literal", value=tok.value)
        if tok.kind == "NUMBER":
            self._eat()
            return Node(kind="literal", value=tok.value)
        if tok.kind == "KEYWORD" and tok.value in {"true", "false", "null", "default"}:
            self._eat()
            return Node(kind="keyword", value=tok.value)
        if tok.kind == "KEYWORD" and tok.value in _BOOL_OPS:
            self.errors.append(
                f"unexpected boolean operator '{tok.value}' with no left operand at column {tok.pos + 1}"
            )
            self._eat()
            return None
        if tok.kind == "KEYWORD" and tok.value in _MEMBERSHIP:
            self.errors.append(
                f"unexpected membership operator '{tok.value}' with no left operand at column {tok.pos + 1}"
            )
            self._eat()
            return None
        if tok.kind == "IDENT":
            self._eat()
            return Node(kind="ident", value=tok.value)
        if tok.kind == "KEYWORD":
            self.errors.append(
                f"unknown keyword '{tok.value}' at column {tok.pos + 1}"
            )
            self._eat()
            return None
        # Unrecognised
        self.errors.append(
            f"unexpected token {tok.value!r} at column {tok.pos + 1}"
        )
        self._eat()
        return None


# ─── Public API ────────────────────────────────────────────────────


def parse_expression(expr: str) -> Optional[Node]:
    """Parse and return the AST root, or None when the expression is empty.

    Errors are silently swallowed - call :func:`validate_expression` for
    a list of human-readable error messages.
    """
    if not expr or not expr.strip():
        return None
    tokens, _ = _tokenize(expr)
    return _Parser(tokens).parse()


def validate_expression(expr: str, *, ctx: str = "expression") -> list[str]:
    """Return the list of error messages for ``expr``. Empty list on success.

    ``ctx`` is a short label prepended to each message so the caller can
    pin the error to a specific YAML location (e.g. ``"flow.routes[0].when"``).
    """
    if not expr or not expr.strip():
        return [f"{ctx}: empty expression"]
    tokens, lex_errors = _tokenize(expr)
    if not tokens and not lex_errors:
        return [f"{ctx}: empty expression"]
    parser = _Parser(tokens)
    parser.parse()
    all_errors = lex_errors + parser.errors
    return [f"{ctx}: {e}" for e in all_errors]


def collect_identifiers(node: Optional[Node]) -> list[str]:
    """Walk the AST and return every identifier path referenced.

    Useful for the future runtime-context validator (Phase 5) that
    will check ``input.x`` against the actual runtime namespace."""
    out: list[str] = []
    if node is None:
        return out
    if node.kind == "ident":
        if isinstance(node.value, str):
            out.append(node.value)
    for child in node.children:
        out.extend(collect_identifiers(child))
    return out


def expression_uses_identifiers(expr: str) -> Iterable[str]:
    """Convenience: parse and collect in one call."""
    return collect_identifiers(parse_expression(expr))


# ─── Context schemas ────────────────────────────────────────────


# Each expression site has a fixed set of root identifiers the runtime
# eval namespace exposes. Anything else is a typo and the compile must
# reject it - that is the Phase 9 strictness upgrade over Phase 4
# (which only validated syntax).

# HOOK condition.expr context: matches what
# `digitorn.core.runtime.hooks._eval_expression` puts in the eval()
# namespace. Adding a key here without adding it to the runtime
# namespace would break the contract - the linter and the runtime
# must stay in lockstep.
HOOK_CONTEXT_ROOTS: frozenset[str] = frozenset({
    "turn",        # int, current turn index
    "tools",       # int, total tool calls so far
    "messages",    # int, total messages so far
    "pressure",    # float, token pressure ratio (0..1)
    "tokens",      # int, total tokens used
    "max_turns",   # int, configured ceiling
    # Templated namespace also accessible via {{tool.*}} resolver but
    # supported in expressions for symmetry with hook actions:
    "tool",
    "session",
    "state",
    # Reserved scalars for future expansion - silently accepted so a
    # YAML that references them does not break when the runtime gains
    # support. Mirrors the runtime's permissive eval namespace.
    "True", "False", "None", "true", "false", "null",
})


# FLOW route.when AND flow.decision.expr context: what the flow runtime
# exposes when evaluating routing conditions. Designed to match what
# the user's runtime implementation will plumb in tomorrow.
FLOW_CONTEXT_ROOTS: frozenset[str] = frozenset({
    "input",       # the user input that triggered this flow
    "output",      # the current node's output (after agent / tool ran)
    "previous",    # the previous node's output payload
    "approvals",   # map: node_id -> user's choice (string)
    "state",       # mutable flow state (set by transform nodes)
    "session",     # session metadata (id, user_id, ...)
    "event",       # the trigger event (background mode)
    # Reserved scalars / literals:
    "True", "False", "None", "true", "false", "null",
})


def validate_expression_against_context(
    expr: str,
    *,
    ctx: str,
    allowed_roots: frozenset[str],
) -> list[str]:
    """Lint an expression for both syntactic correctness AND identifier
    references that resolve to ``allowed_roots``.

    Returns a list of error strings prefixed with ``ctx``. Empty when
    the expression is sound.

    This is the Phase 9 upgrade over :func:`validate_expression`: the
    function still catches every syntactic mistake (paren imbalance,
    typo in operator, ...) AND now also flags ``input.kndr`` (when the
    user meant ``input.kind``) because ``kndr`` won't be a valid path."""
    syntactic = validate_expression(expr, ctx=ctx)
    if syntactic:
        return syntactic
    if not expr or not expr.strip():
        return []
    # Empty expr already caught by validate_expression.
    ast = parse_expression(expr)
    paths = collect_identifiers(ast)
    errors: list[str] = []
    for path in paths:
        root = path.split(".", 1)[0].split("[", 1)[0]
        if root in allowed_roots:
            continue
        import difflib as _df
        sug = _df.get_close_matches(root, allowed_roots, n=3, cutoff=0.6)
        hint = f" Did you mean: {', '.join(sorted(sug))}?" if sug else ""
        errors.append(
            f"{ctx}: identifier '{path}' has unknown root '{root}'. "
            f"Allowed roots: {sorted(allowed_roots - {'True','False','None','true','false','null'})}.{hint}"
        )
    return errors

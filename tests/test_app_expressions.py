"""Phase 4 tests for the expression parser.

The parser lints any string that the runtime will later ``eval()`` or
walk as a logical condition. Three call sites:

  - ``hooks[].condition.expr`` (when condition.type == 'expression')
  - ``flow.nodes[].routes[].when``
  - ``flow.nodes[].decision.expr``

Scope at this phase: SYNTACTIC validation only.

Catches:
  - Empty / whitespace expression
  - Unbalanced parentheses
  - Operator typos (``coantains``, ``=>``, ``=!`` ...)
  - Dangling operators (``a == ``, ``and b``)
  - Invalid identifier shapes (``a..b``, ``.a``, ``a.``)
  - Unterminated strings

Out of scope (Phase 5, runtime context-aware):
  - Whether a referenced identifier exists in the runtime namespace
  - Type compatibility (``"abc" >= 5``)
  - Whether the operator makes sense for the operand types

The expression dialect supports:
  identifiers : ``a``, ``a.b``, ``a.b.c``, ``a.b[0].c``
  literals    : strings ``"x"`` ``'x'``, numbers ``1``, ``1.5``,
                booleans ``true`` ``false``, null ``null``
  comparators : ``==``, ``!=``, ``<``, ``>``, ``<=``, ``>=``
  membership  : ``contains``, ``in``
  boolean     : ``and``, ``or``, ``not``
  parens      : ``(``, ``)``
  sentinel    : the bare token ``default`` (matches when no other route does)
"""
from __future__ import annotations

import pytest

from digitorn.core.app.expressions import parse_expression, validate_expression


class TestSyntacticHappyPath:
    """Well-formed expressions parse without errors."""

    @pytest.mark.parametrize("expr", [
        "default",
        "a == 1",
        "a == 'x'",
        "a.b.c == 'x'",
        "a == 1 and b == 2",
        "a == 1 or b == 2",
        "not a",
        "(a == 1) and (b == 2 or c == 3)",
        "a contains 'x'",
        "a >= 3",
        "a <= 3.14",
        "a != null",
        "input.payload.email == 'x@y.com'",
        "approvals.gate == 'approve'",
        "session.consecutive_failures.web_fetch >= 3",
        "not (a == 1)",
        "true",
        "false",
    ])
    def test_valid_expr_parses(self, expr: str):
        errors = validate_expression(expr, ctx="test")
        assert errors == [], f"{expr!r} should parse cleanly, got: {errors}"


class TestSyntacticErrors:
    """Real typos and malformed inputs surface as errors."""

    def test_empty_expression(self):
        errors = validate_expression("", ctx="test")
        assert errors and "empty" in errors[0].lower()

    def test_whitespace_only(self):
        errors = validate_expression("   ", ctx="test")
        assert errors

    def test_unbalanced_open_paren(self):
        errors = validate_expression("(a == 1", ctx="test")
        assert errors and ("paren" in errors[0].lower() or "unbalanced" in errors[0].lower())

    def test_unbalanced_close_paren(self):
        errors = validate_expression("a == 1)", ctx="test")
        assert errors

    def test_dangling_operator(self):
        errors = validate_expression("a ==", ctx="test")
        assert errors

    def test_double_operator(self):
        errors = validate_expression("a == == b", ctx="test")
        assert errors

    def test_typo_contains(self):
        errors = validate_expression("a coantains 'x'", ctx="test")
        assert errors and ("coantains" in errors[0].lower() or "unknown" in errors[0].lower())

    def test_invalid_compound_operator(self):
        errors = validate_expression("a => 1", ctx="test")
        assert errors

    def test_unterminated_string(self):
        errors = validate_expression("a == 'x", ctx="test")
        assert errors and ("string" in errors[0].lower() or "unterminated" in errors[0].lower())

    def test_double_dot_in_path(self):
        errors = validate_expression("a..b == 1", ctx="test")
        assert errors

    def test_leading_dot(self):
        errors = validate_expression(".a == 1", ctx="test")
        assert errors

    def test_trailing_dot(self):
        errors = validate_expression("a. == 1", ctx="test")
        assert errors


class TestParseAst:
    """parse_expression returns a structured AST callers can walk."""

    def test_parses_simple_comparison(self):
        ast = parse_expression("a == 1")
        assert ast is not None

    def test_collects_referenced_identifiers(self):
        from digitorn.core.app.expressions import collect_identifiers
        ast = parse_expression("session.foo == 1 and tool.params.url contains '/x'")
        idents = collect_identifiers(ast)
        roots = {i.split(".", 1)[0] for i in idents}
        assert "session" in roots
        assert "tool" in roots

    def test_default_keyword_alone(self):
        """A bare ``default`` keyword is the wildcard route condition."""
        ast = parse_expression("default")
        assert ast is not None


class TestIntegrationWithCompiler:
    """End-to-end: invalid expressions in hooks/flow surface as compile errors."""

    def test_hook_expression_with_typo_caught(self):
        from unittest.mock import AsyncMock, MagicMock
        from digitorn.core.app.compiler import AppYAMLCompiler
        from digitorn.core.app.errors import AppCompilationError

        web = MagicMock()
        web.MODULE_ID = "web"
        manifest = MagicMock()
        manifest.action_names.return_value = ["search"]
        manifest.actions = []
        manifest.supported_constraints = []
        web.get_manifest.return_value = manifest
        web._action_registry = {}
        web.execute = AsyncMock()
        web.on_config_update = AsyncMock()
        web.on_start = AsyncMock()
        web.on_stop = AsyncMock()
        registry = MagicMock()
        registry.list_available.return_value = ["web"]
        registry.get = MagicMock(return_value=web)
        registry.create = registry.get
        registry.is_available = MagicMock(return_value=True)

        raw = {
            "app": {"app_id": "test", "name": "T"},
            "agents": [{"id": "main", "brain": {"provider": "ollama", "model": "llama3"}}],
            "modules": {"web": {}},
            "execution": {
                "hooks": [{
                    "id": "bad",
                    "on": "tool_end",
                    "condition": {
                        "type": "expression",
                        "expr": "a == == b",  # syntax error
                    },
                    "action": {"type": "log", "message": "x"},
                }],
            },
        }
        with pytest.raises(AppCompilationError) as exc_info:
            AppYAMLCompiler(registry).compile(raw)
        msg = "\n".join(str(e) for e in exc_info.value.errors)
        assert "expr" in msg.lower() or "expression" in msg.lower()

    def test_flow_when_with_unbalanced_paren_caught(self):
        from unittest.mock import AsyncMock, MagicMock
        from digitorn.core.app.compiler import AppYAMLCompiler
        from digitorn.core.app.errors import AppCompilationError

        registry = MagicMock()
        registry.list_available.return_value = []
        registry.get = MagicMock(side_effect=Exception("none"))
        registry.create = registry.get
        registry.is_available = MagicMock(return_value=False)

        raw = {
            "app": {"app_id": "test", "name": "T"},
            "agents": [{"id": "main", "brain": {"provider": "ollama", "model": "llama3"}}],
            "flow": {
                "id": "f",
                "entry": "step",
                "nodes": [
                    {
                        "id": "step",
                        "type": "agent",
                        "agent": "main",
                        "routes": [
                            {"when": "(a == 1", "to": "end"},  # unbalanced paren
                        ],
                    },
                ],
            },
        }
        with pytest.raises(AppCompilationError) as exc_info:
            AppYAMLCompiler(registry).compile(raw)
        msg = "\n".join(str(e) for e in exc_info.value.errors).lower()
        assert "when" in msg or "expression" in msg or "paren" in msg

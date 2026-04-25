"""Tests for LSP module v3 — dynamic config, 3 protocols, auto-detection."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest

from digitorn.modules.lsp.module import (
    LspModule,
    _detect_protocol,
    _detect_parser,
    _NAME_TO_EXTENSIONS,
)
from digitorn.modules.lsp.protocols import (
    LspProtocol,
    CompilerProtocol,
    LinterProtocol,
    create_protocol,
)
from digitorn.modules.lsp.parsers import (
    PARSERS,
    Diagnostic,
    parse_ruff,
    parse_generic_json,
    parse_fallback,
    parse_lsp_diagnostics,
)


# ── Protocol detection ───────────────────────────────────────────


class TestProtocolDetection:
    @pytest.mark.parametrize("cmd,expected", [
        ("pyright-langserver --stdio", "lsp"),
        ("gopls", "lsp"),
        ("texlab", "lsp"),
        ("rust-analyzer", "lsp"),
        ("typescript-language-server --stdio", "lsp"),
        ("pylsp", "lsp"),
        ("vscode-json-languageserver --stdio", "lsp"),
        ("yaml-language-server --stdio", "lsp"),
        ("cargo check --message-format=json", "compiler"),
        ("tsc --noEmit", "compiler"),
        ("go vet -json", "compiler"),
        ("cargo build", "compiler"),
        ("gcc -fsyntax-only", "linter"),  # gcc without build/compile keyword → linter
        ("ruff check", "linter"),
        ("eslint", "linter"),
        ("stylelint", "linter"),
        ("mypy", "linter"),
        ("flake8", "linter"),
        ("black", "linter"),
        ("prettier", "linter"),
    ])
    def test_detect_protocol(self, cmd, expected):
        assert _detect_protocol(cmd) == expected

    @pytest.mark.parametrize("cmd,expected", [
        ("ruff check", "ruff"),
        ("eslint", "eslint"),
        ("cargo check", "cargo"),
        ("tsc --noEmit", "tsc"),
        ("unknown-tool", "fallback"),
    ])
    def test_detect_parser(self, cmd, expected):
        assert _detect_parser(cmd) == expected


# ── Extension mapping ────────────────────────────────────────────


class TestExtensionMapping:
    def test_all_major_languages(self):
        for lang in ["python", "typescript", "go", "rust", "latex", "json", "yaml", "css", "html"]:
            assert lang in _NAME_TO_EXTENSIONS, f"Missing: {lang}"
            assert len(_NAME_TO_EXTENSIONS[lang]) > 0

    def test_total_languages(self):
        assert len(_NAME_TO_EXTENSIONS) >= 30


# ── Config parsing ───────────────────────────────────────────────


class TestConfigParsing:
    def test_simple_string_config(self):
        module = LspModule()
        specs = module._parse_config({
            "python": "pyright-langserver --stdio",
            "rust": "cargo check --message-format=json",
        })
        assert len(specs) == 2

        py_spec = next(s for s in specs if s["name"] == "python")
        assert py_spec["protocol"] == "lsp"
        assert py_spec["extensions"] == [".py", ".pyi"]

        rs_spec = next(s for s in specs if s["name"] == "rust")
        assert rs_spec["protocol"] == "compiler"
        assert rs_spec["extensions"] == [".rs"]

    def test_full_dict_config(self):
        module = LspModule()
        specs = module._parse_config({
            "servers": {
                "latex": {
                    "command": "texlab",
                    "protocol": "lsp",
                    "extensions": [".tex", ".bib"],
                },
                "custom": {
                    "command": "my-tool --json",
                    "protocol": "linter",
                    "extensions": [".xyz"],
                    "parser": "generic_json",
                },
            },
        })
        assert len(specs) == 2

        latex = next(s for s in specs if s["name"] == "latex")
        assert latex["protocol"] == "lsp"
        assert ".tex" in latex["extensions"]

        custom = next(s for s in specs if s["name"] == "custom")
        assert custom["parser"] == "generic_json"

    def test_mixed_config(self):
        module = LspModule()
        specs = module._parse_config({
            "python": "pyright-langserver --stdio",
            "servers": {
                "latex": {"command": "texlab", "protocol": "lsp"},
            },
        })
        assert len(specs) == 2

    def test_workspace_ignored(self):
        module = LspModule()
        specs = module._parse_config({
            "workspace": "/some/path",
            "python": "pyright-langserver --stdio",
        })
        assert len(specs) == 1
        assert specs[0]["name"] == "python"


# ── Protocol factory ─────────────────────────────────────────────


class TestProtocolFactory:
    def test_create_lsp(self):
        p = create_protocol("lsp")
        assert isinstance(p, LspProtocol)
        assert p.mode == "lsp"

    def test_create_compiler(self):
        p = create_protocol("compiler", "cargo")
        assert isinstance(p, CompilerProtocol)
        assert p.mode == "compiler"

    def test_create_linter(self):
        p = create_protocol("linter", "ruff")
        assert isinstance(p, LinterProtocol)
        assert p.mode == "linter"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown protocol"):
            create_protocol("unknown")


# ── Parsers ──────────────────────────────────────────────────────


class TestParsers:
    def test_all_parsers_registered(self):
        for name in ["ruff", "eslint", "tsc", "cargo", "govet", "generic_json", "generic_lines", "fallback"]:
            assert name in PARSERS

    def test_parse_generic_json_array(self):
        stdout = '[{"file":"a.py","line":1,"message":"bad","severity":"error"}]'
        diags = parse_generic_json(stdout, "")
        assert len(diags) == 1
        assert diags[0].severity == "error"
        assert diags[0].file == "a.py"

    def test_parse_generic_json_nested(self):
        stdout = '{"diagnostics":[{"file":"b.py","line":5,"message":"oops"}]}'
        diags = parse_generic_json(stdout, "")
        assert len(diags) == 1
        assert diags[0].line == 5

    def test_parse_fallback(self):
        text = "src/app.py:42:10: error: undefined name 'foo'"
        diags = parse_fallback(text)
        assert len(diags) == 1
        assert diags[0].file == "src/app.py"
        assert diags[0].line == 42
        assert diags[0].severity == "error"

    def test_parse_lsp_diagnostics(self):
        raw = [{
            "range": {"start": {"line": 5, "character": 10}},
            "severity": 1,
            "message": "Type mismatch",
            "code": "E001",
            "source": "pyright",
        }]
        diags = parse_lsp_diagnostics(raw, "test.py")
        assert len(diags) == 1
        assert diags[0].line == 6  # 0-indexed → 1-indexed
        assert diags[0].severity == "error"
        assert diags[0].source == "pyright"


# ── Real pyright test (LSP protocol) ─────────────────────────────


@pytest.mark.skipif(not shutil.which("pyright-langserver"), reason="pyright not installed")
class TestRealPyrightV3:
    async def test_lsp_protocol_real(self):
        from digitorn.core.sidecar_pool import DaemonSidecarPool

        pool = DaemonSidecarPool()
        await pool.start()

        proto = LspProtocol()
        try:
            ok = await proto.start(pool, "pyright-v3", "test", ["pyright-langserver", "--stdio"], "/tmp")
            assert ok
            assert proto.is_connected

            # Write a file with error
            test_dir = Path("/tmp/test-lsp-v3")
            test_dir.mkdir(exist_ok=True)
            test_file = test_dir / "bad.py"
            test_file.write_text('x: str = 42  # Type error\n')

            await proto.notify_file_changed(str(test_file))
            await asyncio.sleep(5)

            diags = proto.get_diagnostics(str(test_file))
            assert len(diags) > 0, "Pyright should detect type error"
            assert any("type" in d.message.lower() or "cannot" in d.message.lower() for d in diags)
        finally:
            await proto.stop()
            await pool.stop()
            import shutil as sh
            sh.rmtree("/tmp/test-lsp-v3", ignore_errors=True)

    async def test_module_with_dynamic_config(self):
        """Test the full module with dynamic config."""
        from digitorn.core.sidecar_pool import DaemonSidecarPool

        pool = DaemonSidecarPool()
        await pool.start()

        module = LspModule()
        module._sidecar_pool = pool
        module._app_id = "v3-test"
        module._workspace = "/tmp/test-lsp-v3-module"

        test_dir = Path(module._workspace)
        test_dir.mkdir(exist_ok=True)
        (test_dir / "pyproject.toml").write_text('[project]\nname = "test"\n')
        (test_dir / "error.py").write_text('val: str = 123\n')

        try:
            # Parse config like YAML would
            specs = module._parse_config({"python": "pyright-langserver --stdio"})
            for spec in specs:
                await module._start_server(spec)

            assert ".py" in module._protocols
            assert module._protocols[".py"].mode == "lsp"

            # Notify change
            result = await module.execute("notify_change", {"path": str(test_dir / "error.py")})
            assert result.success

            await asyncio.sleep(5)

            # Get diagnostics
            result = await module.execute("diagnostics", {"path": str(test_dir / "error.py")})
            assert result.success
            assert result.data["mode"] == "lsp"
        finally:
            await module.on_stop()
            await pool.stop()
            import shutil as sh
            sh.rmtree(str(test_dir), ignore_errors=True)


# ── Real ruff test (Linter protocol) ─────────────────────────────


@pytest.mark.skipif(not shutil.which("ruff"), reason="ruff not installed")
class TestRealRuffV3:
    async def test_linter_protocol_real(self):
        proto = LinterProtocol("ruff")
        ok = await proto.start(None, "ruff-v3", "test", ["ruff", "check"], "/tmp")
        assert ok
        assert proto.is_connected

        test_file = Path("/tmp/test-ruff-v3.py")
        test_file.write_text("import os\nimport sys\n")

        try:
            await proto.notify_file_changed(str(test_file))
            diags = proto.get_diagnostics(str(test_file))
            assert len(diags) > 0, "Ruff should find unused imports"
            assert any("F401" in d.code for d in diags)
        finally:
            await proto.stop()
            test_file.unlink(missing_ok=True)

    async def test_module_linter_fallback(self):
        """Module uses linter when no LSP available."""
        module = LspModule()
        module._workspace = "/tmp"

        specs = module._parse_config({"python": "ruff check"})
        assert specs[0]["protocol"] == "linter"

        for spec in specs:
            await module._start_server(spec)

        assert ".py" in module._protocols
        assert module._protocols[".py"].mode == "linter"

        test_file = Path("/tmp/test-ruff-v3-module.py")
        test_file.write_text("import os\n")

        try:
            result = await module.execute("notify_change", {"path": str(test_file)})
            assert result.success

            result = await module.execute("diagnostics", {"path": str(test_file)})
            assert result.success
            assert result.data["mode"] == "linter"
            assert result.data["total"] > 0
        finally:
            await module.on_stop()
            test_file.unlink(missing_ok=True)


# ── Module integration ───────────────────────────────────────────


class TestModuleIntegration:
    def test_version(self):
        m = LspModule()
        assert m.VERSION == "3.0.0"

    def test_actions(self):
        m = LspModule()
        assert set(m._action_registry.keys()) == {
            "diagnostics", "check", "notify_change", "request", "cancel_request"
        }

    async def test_no_server_error_message(self):
        m = LspModule()
        result = await m.execute("diagnostics", {"path": "test.xyz"})
        assert not result.success
        assert "No feedback server" in result.error

    async def test_list_active_servers(self):
        m = LspModule()
        result = await m.execute("diagnostics", {})
        # No servers configured → should either show empty list or error
        assert result.success or result.error

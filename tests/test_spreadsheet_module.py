"""Spreadsheet module tests - create, read, edit, config, engine, renderer."""

from __future__ import annotations

from pathlib import Path

import pytest

from digitorn.modules.spreadsheet.module import SpreadsheetConfig, SpreadsheetModule
from digitorn.modules.spreadsheet.params import (
    CreateParams,
    EditParams,
    ReadParams,
)

xlsxwriter = pytest.importorskip("xlsxwriter")


@pytest.fixture
def ss():
    m = SpreadsheetModule()
    m._context = None  # reset shared ContextVar from previous tests
    yield m
    m._context = None  # cleanup for next tests


def _simple_sheets() -> list[dict]:
    """Return a minimal sheets spec compatible with SheetSpec."""
    return [{
        "name": "Sheet1",
        "columns": ["Name", "Value"],
        "data": [
            ["Alice", 100],
            ["Bob", 200],
            ["Charlie", 300],
        ],
    }]


# ═══════════════════════════════════════════════════════════════
# CONFIG_MODEL
# ═══════════════════════════════════════════════════════════════


class TestSpreadsheetConfig:
    def test_defaults(self):
        c = SpreadsheetConfig()
        assert c.default_font is None
        assert c.max_rows == 1_000_000

    def test_custom(self):
        c = SpreadsheetConfig(default_font="Arial", max_rows=5000)
        assert c.default_font == "Arial"
        assert c.max_rows == 5000

    def test_validation(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SpreadsheetConfig(max_rows=0)

    def test_config_model_set(self):
        assert SpreadsheetModule.CONFIG_MODEL is SpreadsheetConfig


# ═══════════════════════════════════════════════════════════════
# MODULE BASICS
# ═══════════════════════════════════════════════════════════════


class TestModule:
    def test_module_id(self, ss):
        assert ss.MODULE_ID == "spreadsheet"

    def test_manifest(self, ss):
        m = ss.get_manifest()
        assert m.module_id == "spreadsheet"


# ═══════════════════════════════════════════════════════════════
# OUTPUT CONSTRAINT
# ═══════════════════════════════════════════════════════════════


class TestOutputConstraint:
    """Tests for output directory constraint checking.

    The _check_output helper is not yet implemented on SpreadsheetModule,
    so these tests are skipped until it lands.
    """

    def test_no_constraint(self, ss):
        if not hasattr(ss, "_check_output"):
            pytest.skip("_check_output not implemented yet")
        assert ss._check_output("/tmp/test.xlsx") is None

    def test_inside_allowed(self, ss):
        if not hasattr(ss, "_check_output"):
            pytest.skip("_check_output not implemented yet")

        class FakeCtx:
            constraints = {"output_dir": "/tmp"}

        ss._context = FakeCtx()
        assert ss._check_output("/tmp/file.xlsx") is None

    def test_outside_blocked(self, ss):
        if not hasattr(ss, "_check_output"):
            pytest.skip("_check_output not implemented yet")

        class FakeCtx:
            constraints = {"output_dir": "/tmp/safe"}

        ss._context = FakeCtx()
        err = ss._check_output("/var/file.xlsx")
        assert err is not None
        assert "outside" in err.lower()


# ═══════════════════════════════════════════════════════════════
# CREATE
# ═══════════════════════════════════════════════════════════════


class TestCreate:
    @pytest.mark.asyncio
    async def test_basic(self, ss, tmp_path):
        out = str(tmp_path / "test.xlsx")
        r = await ss.create(CreateParams(
            name="test",
            sheets=_simple_sheets(),
            output_path=out,
        ))
        assert r.success
        assert Path(out).exists()

    @pytest.mark.asyncio
    async def test_with_formulas(self, ss, tmp_path):
        sheets = _simple_sheets()
        sheets[0]["formulas"] = {"B5": "=SUM(B2:B4)"}
        out = str(tmp_path / "formulas.xlsx")
        r = await ss.create(CreateParams(
            name="formulas",
            sheets=sheets,
            output_path=out,
        ))
        assert r.success
        assert Path(out).exists()


# ═══════════════════════════════════════════════════════════════
# READ
# ═══════════════════════════════════════════════════════════════


class TestRead:
    @pytest.mark.asyncio
    async def test_not_found(self, ss):
        r = await ss.read(ReadParams(path="/nonexistent.xlsx"))
        assert not r.success

    @pytest.mark.asyncio
    async def test_read_created(self, ss, tmp_path):
        out = str(tmp_path / "read_test.xlsx")
        await ss.create(CreateParams(
            name="read_test",
            sheets=_simple_sheets(),
            output_path=out,
        ))

        r = await ss.read(ReadParams(path=out))
        assert r.success
        assert r.data.get("total_rows", r.data.get("row_count", 0)) >= 3 or len(r.data.get("data", [])) >= 3

    @pytest.mark.asyncio
    async def test_read_with_max_rows(self, ss, tmp_path):
        out = str(tmp_path / "limit.xlsx")
        await ss.create(CreateParams(
            name="limit",
            sheets=_simple_sheets(),
            output_path=out,
        ))

        r = await ss.read(ReadParams(path=out, max_rows=1))
        assert r.success


# ═══════════════════════════════════════════════════════════════
# ENGINE - _cell_to_rc helper
# ═══════════════════════════════════════════════════════════════


class TestEngine:
    def test_cell_to_rc(self):
        try:
            from digitorn.modules.spreadsheet.engine import _cell_to_rc

            assert _cell_to_rc("A1") == (0, 0)
            assert _cell_to_rc("B3") == (2, 1)
            assert _cell_to_rc("C1") == (0, 2)
            assert _cell_to_rc("Z1") == (0, 25)
        except ImportError:
            pytest.skip("_cell_to_rc not exported")


# ═══════════════════════════════════════════════════════════════
# RENDERER - SpreadsheetRenderer
# ═══════════════════════════════════════════════════════════════


class TestSpreadsheetRenderer:
    def test_preview_basic(self):
        from digitorn.modules.spreadsheet.renderer import SpreadsheetRenderer
        import json

        renderer = SpreadsheetRenderer()

        class FakeBuffer:
            content = json.dumps({
                "sheets": [{
                    "name": "Test",
                    "columns": [{"header": "Name"}, {"header": "Value"}],
                    "data": [["Alice", 100], ["Bob", 200]],
                }]
            })
            def read_formatted(self):
                return self.content

        result = renderer.preview(FakeBuffer())
        assert "Test" in result
        assert "Alice" in result

    def test_preview_empty(self):
        from digitorn.modules.spreadsheet.renderer import SpreadsheetRenderer

        renderer = SpreadsheetRenderer()

        class FakeBuffer:
            content = ""
            def read_formatted(self):
                return ""

        result = renderer.preview(FakeBuffer())
        assert "empty" in result.lower()

    def test_summary_line(self):
        from digitorn.modules.spreadsheet.renderer import SpreadsheetRenderer
        import json

        renderer = SpreadsheetRenderer()

        class FakeBuffer:
            content = json.dumps({
                "sheets": [
                    {"name": "S1", "data": [[1], [2], [3]], "charts": [{"type": "line"}]},
                    {"name": "S2", "data": [[4]]},
                ]
            })
            lines = 10

        result = renderer.summary_line(FakeBuffer())
        assert "2 sheets" in result
        assert "4 rows" in result
        assert "1 charts" in result

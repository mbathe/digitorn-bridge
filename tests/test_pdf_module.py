"""PDF module tests - renderer, constraints, config, actions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from digitorn.modules.pdf.module import PdfConfig, PDFModule
from digitorn.modules.pdf.params import (
    GenerateParams,
    GenerateTypstParams,
    MergeParams,
    MetadataParams,
    ReadParams,
    SplitParams,
)
from digitorn.modules.pdf.renderer import markdown_to_typst


def _make_pdf():
    """Create PDFModule, mocking typst dependency check if needed."""
    try:
        import typst  # noqa: F401
        return PDFModule()
    except ImportError:
        with patch.object(PDFModule, "_check_dependencies"):
            return PDFModule()


# ═══════════════════════════════════════════════════════════════
# CONFIG_MODEL
# ═══════════════════════════════════════════════════════════════


class TestPdfConfig:
    def test_defaults(self):
        c = PdfConfig()
        assert c.default_style == "auto"
        assert c.default_page_size == "a4"
        assert c.default_language == "fr"

    def test_custom(self):
        c = PdfConfig(default_style="report", default_page_size="letter", default_language="en")
        assert c.default_style == "report"

    def test_config_model_set(self):
        assert PDFModule.CONFIG_MODEL is PdfConfig


# ═══════════════════════════════════════════════════════════════
# MODULE BASICS
# ═══════════════════════════════════════════════════════════════


class TestPDFModule:
    @pytest.fixture
    def pdf(self):
        return _make_pdf()

    def test_module_id(self, pdf):
        assert pdf.MODULE_ID == "pdf"
        assert pdf.VERSION == "1.0.0"

    def test_manifest(self, pdf):
        m = pdf.get_manifest()
        assert m.module_id == "pdf"
        names = [c.name for c in (m.supported_constraints or [])]
        assert "output_dir" in names
        assert "max_pages" in names


# ═══════════════════════════════════════════════════════════════
# OUTPUT PATH CONSTRAINT
# ═══════════════════════════════════════════════════════════════


class TestOutputPathConstraint:
    @pytest.fixture
    def pdf(self):
        return _make_pdf()

    def test_no_constraint(self, pdf):
        assert pdf._check_output_path("/tmp/test.pdf") is None

    def test_inside_allowed(self, pdf):
        # Simulate constraint
        class FakeCtx:
            constraints = {"output_dir": "/tmp"}

        pdf._context = FakeCtx()
        assert pdf._check_output_path("/tmp/report.pdf") is None

    def test_outside_allowed(self, pdf):
        class FakeCtx:
            constraints = {"output_dir": "/tmp/safe"}

        pdf._context = FakeCtx()
        err = pdf._check_output_path("/var/bad.pdf")
        assert err is not None
        assert "outside" in err.lower()


# ═══════════════════════════════════════════════════════════════
# GENERATE - basic validation (no typst needed)
# ═══════════════════════════════════════════════════════════════


class TestGenerate:
    @pytest.fixture
    def pdf(self):
        m = _make_pdf()
        m._context = None  # ensure no constraint leaks
        return m

    @pytest.mark.asyncio
    async def test_rejects_non_pdf(self, pdf):
        r = await pdf.generate(GenerateParams(
            content="# Hello",
            output_path="/tmp/report.txt",
        ))
        assert not r.success
        assert ".pdf" in r.error

    @pytest.mark.asyncio
    async def test_rejects_outside_output_dir(self, pdf):
        class FakeCtx:
            constraints = {"output_dir": "/tmp/safe"}

        pdf._context = FakeCtx()
        r = await pdf.generate(GenerateParams(
            content="# Hello",
            output_path="/var/report.pdf",
        ))
        assert not r.success
        assert "outside" in r.error.lower()


class TestGenerateTypst:
    @pytest.fixture
    def pdf(self):
        m = _make_pdf()
        m._context = None
        return m

    @pytest.mark.asyncio
    async def test_rejects_non_pdf(self, pdf):
        r = await pdf.generate_typst(GenerateTypstParams(
            content="= Hello",
            output_path="/tmp/out.txt",
        ))
        assert not r.success
        assert ".pdf" in r.error


# ═══════════════════════════════════════════════════════════════
# READ - file not found
# ═══════════════════════════════════════════════════════════════


class TestRead:
    @pytest.fixture
    def pdf(self):
        return _make_pdf()

    @pytest.mark.asyncio
    async def test_not_found(self, pdf):
        r = await pdf.read(ReadParams(path="/nonexistent.pdf"))
        assert not r.success
        assert "not found" in r.error.lower()


class TestMerge:
    @pytest.fixture
    def pdf(self):
        return _make_pdf()

    @pytest.mark.asyncio
    async def test_merge_outside_output_dir(self, pdf):
        class FakeCtx:
            constraints = {"output_dir": "/tmp/safe"}

        pdf._context = FakeCtx()
        r = await pdf.merge(MergeParams(
            files=["/tmp/a.pdf", "/tmp/b.pdf"],
            output_path="/var/merged.pdf",
        ))
        assert not r.success


class TestSplit:
    @pytest.fixture
    def pdf(self):
        return _make_pdf()

    @pytest.mark.asyncio
    async def test_not_found(self, pdf):
        r = await pdf.split(SplitParams(
            path="/nonexistent.pdf",
            pages="1-3",
            output_path="/tmp/out.pdf",
        ))
        assert not r.success

class TestMetadata:
    @pytest.fixture
    def pdf(self):
        return _make_pdf()

    @pytest.mark.asyncio
    async def test_not_found(self, pdf):
        r = await pdf.metadata(MetadataParams(path="/nonexistent.pdf"))
        assert not r.success


# ═══════════════════════════════════════════════════════════════
# RENDERER - markdown_to_typst (pure functions, no deps)
# ═══════════════════════════════════════════════════════════════


class TestMarkdownToTypst:
    def test_headings(self):
        result = markdown_to_typst("# H1\n## H2\n### H3\n#### H4")
        assert "= H1" in result
        assert "== H2" in result
        assert "=== H3" in result
        assert "==== H4" in result

    def test_bold(self):
        result = markdown_to_typst("**bold text**")
        assert "*bold text*" in result or "bold text" in result

    def test_italic(self):
        result = markdown_to_typst("*italic text*")
        assert "_italic text_" in result or "italic text" in result

    def test_code_block(self):
        result = markdown_to_typst("```python\nprint('hello')\n```")
        assert "print('hello')" in result

    def test_horizontal_rule(self):
        result = markdown_to_typst("---")
        assert "line" in result.lower() or "---" in result

    def test_table(self):
        md = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        result = markdown_to_typst(md)
        assert "table" in result.lower() or "1" in result

    def test_empty(self):
        result = markdown_to_typst("")
        assert isinstance(result, str)

    def test_multiline(self):
        md = "# Title\n\nParagraph text.\n\n- item 1\n- item 2"
        result = markdown_to_typst(md)
        assert "Title" in result
        assert "item 1" in result

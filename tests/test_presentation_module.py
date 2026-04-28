"""Tests for the Presentation module - HTML/CSS → PPTX engine."""

import os
import tempfile

import pytest

from digitorn.modules.presentation.engine import PresentationEngine
from digitorn.modules.presentation.themes import THEMES, THEME_NAMES, SlideTheme
from digitorn.modules.presentation.module import PresentationModule


# ── Theme Tests ──


class TestThemes:
    def test_all_themes_exist(self):
        assert len(THEMES) == 7
        expected = {"corporate", "dark", "minimal", "ocean", "forest", "royal", "sunset"}
        assert set(THEMES.keys()) == expected

    def test_theme_names_sorted(self):
        assert THEME_NAMES == sorted(THEMES.keys())

    def test_theme_dataclass(self):
        t = THEMES["corporate"]
        assert isinstance(t, SlideTheme)
        assert t.name == "corporate"
        assert t.bg_color.startswith("#")
        assert t.font_family == "Calibri"


# ── Engine Tests ──


class TestPresentationEngine:
    def setup_method(self):
        self.engine = PresentationEngine()
        self.tmpdir = tempfile.mkdtemp()

    def test_new_presentation(self):
        op = self.engine.new_presentation(
            "test", os.path.join(self.tmpdir, "test.pptx"),
        )
        assert op.theme.name == "corporate"
        assert len(op.slides) == 0

    def test_new_presentation_duplicate_error(self):
        self.engine.new_presentation("dup", os.path.join(self.tmpdir, "dup.pptx"))
        with pytest.raises(ValueError, match="already open"):
            self.engine.new_presentation("dup", os.path.join(self.tmpdir, "dup2.pptx"))

    def test_add_slide_title(self):
        self.engine.new_presentation("p1", os.path.join(self.tmpdir, "p1.pptx"))
        result = self.engine.add_slide(
            "p1",
            "<h1>Company Overview</h1><p>Annual Report 2026</p>",
            layout="title",
        )
        assert result["slide_number"] == 1
        assert result["layout"] == "title"

    def test_add_slide_content(self):
        self.engine.new_presentation("p2", os.path.join(self.tmpdir, "p2.pptx"))
        result = self.engine.add_slide(
            "p2",
            "<h2>Revenue</h2><p>Revenue grew <strong>25%</strong> YoY.</p>"
            "<ul><li>Product A: $5.2M</li><li>Product B: $3.1M</li></ul>",
        )
        assert result["slide_number"] == 1

    def test_add_slide_with_table(self):
        self.engine.new_presentation("p3", os.path.join(self.tmpdir, "p3.pptx"))
        html = (
            "<h2>Financial Data</h2>"
            "<table>"
            "<thead><tr><th>Quarter</th><th>Revenue</th><th>Growth</th></tr></thead>"
            "<tbody>"
            "<tr><td>Q1</td><td>$10.7M</td><td>+12%</td></tr>"
            "<tr><td>Q2</td><td>$12.1M</td><td>+15%</td></tr>"
            "</tbody>"
            "</table>"
        )
        result = self.engine.add_slide("p3", html)
        assert result["slide_number"] == 1

    def test_add_slide_with_kpi(self):
        self.engine.new_presentation("p4", os.path.join(self.tmpdir, "p4.pptx"))
        html = (
            '<h2>Key Metrics</h2>'
            '<div class="columns">'
            '<div class="kpi"><span class="value">$10.7M</span>'
            '<span class="label">Revenue</span></div>'
            '<div class="kpi"><span class="value">92%</span>'
            '<span class="label">Satisfaction</span></div>'
            '</div>'
        )
        result = self.engine.add_slide("p4", html)
        assert result["slide_number"] == 1

    def test_add_slide_section(self):
        self.engine.new_presentation("p5", os.path.join(self.tmpdir, "p5.pptx"))
        result = self.engine.add_slide(
            "p5",
            "<h2>Part II: Strategy</h2><p>Our forward-looking plan</p>",
            layout="section",
        )
        assert result["layout"] == "section"

    def test_add_slide_two_column(self):
        self.engine.new_presentation("p6", os.path.join(self.tmpdir, "p6.pptx"))
        html = (
            "<h2>Comparison</h2>"
            "<div class='col-left'><h3>Plan A</h3><ul><li>Cost: $5M</li></ul></div>"
            "<hr>"
            "<div class='col-right'><h3>Plan B</h3><ul><li>Cost: $8M</li></ul></div>"
        )
        result = self.engine.add_slide("p6", html, layout="two_column")
        assert result["layout"] == "two_column"

    def test_add_slide_code_block(self):
        self.engine.new_presentation("p7", os.path.join(self.tmpdir, "p7.pptx"))
        html = (
            "<h2>Code Example</h2>"
            "<pre>def hello():\n    print('Hello World!')</pre>"
        )
        result = self.engine.add_slide("p7", html)
        assert result["slide_number"] == 1

    def test_add_slide_blockquote(self):
        self.engine.new_presentation("p8", os.path.join(self.tmpdir, "p8.pptx"))
        html = (
            "<h2>Vision</h2>"
            "<blockquote>The best way to predict the future is to invent it.</blockquote>"
            "<p>- Alan Kay</p>"
        )
        result = self.engine.add_slide("p8", html)
        assert result["slide_number"] == 1

    def test_edit_slide(self):
        self.engine.new_presentation("e1", os.path.join(self.tmpdir, "e1.pptx"))
        self.engine.add_slide("e1", "<h1>Draft</h1>", layout="title")
        result = self.engine.edit_slide(
            "e1", 1, "<h1>Final Title</h1><p>Updated subtitle</p>",
        )
        assert result["status"] == "updated"

    def test_edit_slide_out_of_range(self):
        self.engine.new_presentation("e2", os.path.join(self.tmpdir, "e2.pptx"))
        self.engine.add_slide("e2", "<h1>Only slide</h1>", layout="title")
        with pytest.raises(ValueError, match="out of range"):
            self.engine.edit_slide("e2", 5, "<h1>Nope</h1>")

    def test_preview_slide(self):
        self.engine.new_presentation("pv", os.path.join(self.tmpdir, "pv.pptx"))
        self.engine.add_slide(
            "pv",
            "<h2>Revenue</h2><p>Some detail here.</p><ul><li>Item 1</li></ul>",
        )
        preview = self.engine.preview_slide("pv", 1)
        assert "Revenue" in preview
        assert "Item 1" in preview

    def test_presentation_state(self):
        self.engine.new_presentation("st", os.path.join(self.tmpdir, "st.pptx"))
        self.engine.add_slide("st", "<h1>Title</h1>", layout="title")
        self.engine.add_slide("st", "<h2>Content</h2><p>Body</p>")
        state = self.engine.presentation_state("st")
        assert state["slide_count"] == 2
        assert state["theme"] == "corporate"
        assert len(state["slides"]) == 2

    def test_finalize(self):
        path = os.path.join(self.tmpdir, "final.pptx")
        self.engine.new_presentation("fin", path)
        self.engine.add_slide("fin", "<h1>Hello World</h1>", layout="title")
        self.engine.add_slide("fin", "<h2>Slide 2</h2><p>Content here</p>")
        result = self.engine.finalize("fin")
        assert result["slides"] == 2
        assert os.path.isfile(path)
        assert result["size_kb"] > 0

    def test_finalize_not_found(self):
        with pytest.raises(ValueError, match="not found"):
            self.engine.finalize("nonexistent")

    def test_multiple_themes(self):
        """Test creating presentations with different themes."""
        for theme_name in ("dark", "ocean", "forest", "royal", "sunset", "minimal"):
            pid = f"theme_{theme_name}"
            path = os.path.join(self.tmpdir, f"{pid}.pptx")
            self.engine.new_presentation(pid, path, theme_name=theme_name)
            self.engine.add_slide(pid, "<h1>Test</h1><p>Theme test</p>", layout="title")
            self.engine.add_slide(pid, "<h2>Content</h2><p>Body text</p>")
            result = self.engine.finalize(pid)
            assert result["theme"] == theme_name
            assert os.path.isfile(path)

    def test_speaker_notes(self):
        self.engine.new_presentation("notes", os.path.join(self.tmpdir, "notes.pptx"))
        self.engine.add_slide(
            "notes",
            "<h2>Key Point</h2><p>Important data</p>",
            speaker_notes="Remember to mention the Q2 numbers and the new product launch.",
        )
        preview = self.engine.preview_slide("notes", 1)
        assert "Notes:" in preview

    def test_full_presentation_10_slides(self):
        """Build a complete 10-slide business presentation."""
        path = os.path.join(self.tmpdir, "full.pptx")
        self.engine.new_presentation("full", path, theme_name="corporate")

        slides = [
            ("<h1>Annual Report 2026</h1><p>Company XYZ - Confidential</p>", "title"),
            ("<h2>Agenda</h2><ol><li>Financial Overview</li><li>Market Analysis</li>"
             "<li>Product Roadmap</li><li>Team Growth</li></ol>", "content"),
            ("<h2>Financial Overview</h2>", "section"),
            ("<h2>Revenue Performance</h2>"
             "<table><thead><tr><th>Quarter</th><th>Revenue</th><th>Growth</th></tr></thead>"
             "<tbody><tr><td>Q1</td><td>$10.7M</td><td>+12%</td></tr>"
             "<tr><td>Q2</td><td>$12.1M</td><td>+15%</td></tr>"
             "<tr><td>Q3</td><td>$14.3M</td><td>+18%</td></tr>"
             "<tr><td>Q4</td><td>$16.8M</td><td>+17%</td></tr></tbody></table>", "content"),
            ('<h2>Key Metrics</h2>'
             '<div class="columns">'
             '<div class="kpi"><span class="value">$53.9M</span>'
             '<span class="label">Total Revenue</span></div>'
             '<div class="kpi"><span class="value">+15.5%</span>'
             '<span class="label">Avg Growth</span></div>'
             '<div class="kpi"><span class="value">92%</span>'
             '<span class="label">NPS Score</span></div>'
             '</div>', "content"),
            ("<h2>Market Analysis</h2>", "section"),
            ("<h2>Competitive Landscape</h2>"
             "<ul><li>Market share increased to <strong>23%</strong></li>"
             "<li>New markets: APAC (+40% growth), LATAM (+25%)</li>"
             "<li>Customer retention at <strong>96%</strong></li></ul>"
             "<blockquote>We are positioned to lead the next wave of innovation.</blockquote>",
             "content"),
            ("<h2>Product Roadmap</h2>", "section"),
            ("<h2>2027 Initiatives</h2>"
             "<ul><li>AI-powered analytics platform (Q1)</li>"
             "<li>Enterprise API v3 (Q2)</li>"
             "<li>Mobile-first dashboard (Q3)</li>"
             "<li>Global expansion toolkit (Q4)</li></ul>", "content"),
            ("<h1>Thank You</h1><p>Questions?</p>", "title"),
        ]

        for html, layout in slides:
            self.engine.add_slide("full", html, layout=layout)

        state = self.engine.presentation_state("full")
        assert state["slide_count"] == 10

        result = self.engine.finalize("full")
        assert result["slides"] == 10
        assert os.path.isfile(path)
        assert result["size_kb"] > 10  # Should be a decent size


# ── Module Tests ──


class TestPresentationModule:
    def setup_method(self):
        self.module = PresentationModule()

    def test_module_id(self):
        assert self.module.MODULE_ID == "presentation"

    def test_get_prompt_sections(self):
        sections = self.module.get_prompt_sections()
        assert len(sections) == 1
        section = sections[0]
        assert "Presentation" in section["title"]
        assert "add_slide" in section["content"]
        assert "HTML" in section["content"]
        assert section["priority"] == 60

    def test_get_manifest(self):
        manifest = self.module.get_manifest()
        assert manifest.module_id == "presentation"
        action_names = [a.name for a in manifest.actions]
        expected = [
            "new_presentation", "add_slide", "edit_slide",
            "remove_slide", "reorder_slides", "preview_slide",
            "presentation_state", "finalize_presentation",
            "list_presentations", "list_themes",
        ]
        for name in expected:
            assert name in action_names, f"Missing action: {name}"

    @pytest.mark.asyncio
    async def test_list_themes_action(self):
        from digitorn.modules.presentation.params import ListThemesParams
        result = await self.module.list_themes(ListThemesParams())
        assert result.success
        assert "corporate" in result.data
        assert "dark" in result.data

    @pytest.mark.asyncio
    async def test_full_workflow(self):
        """Test the complete new → add × N → finalize workflow via module actions."""
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "workflow.pptx")

        from digitorn.modules.presentation.params import (
            NewPresentationParams, AddSlideParams,
            PresentationStateParams, PreviewSlideParams,
            FinalizePresentationParams,
        )

        # 1. New
        r = await self.module.new_presentation(NewPresentationParams(
            presentation_id="wf", output_path=path, theme="ocean",
        ))
        assert r.success

        # 2. Add title
        r = await self.module.add_slide(AddSlideParams(
            presentation_id="wf",
            html="<h1>Workflow Test</h1><p>Testing the module</p>",
            layout="title",
        ))
        assert r.success

        # 3. Add content
        r = await self.module.add_slide(AddSlideParams(
            presentation_id="wf",
            html="<h2>Data</h2><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>",
        ))
        assert r.success

        # 4. State
        r = await self.module.presentation_state(PresentationStateParams(
            presentation_id="wf",
        ))
        assert r.success
        assert r.data["slide_count"] == 2

        # 5. Preview
        r = await self.module.preview_slide(PreviewSlideParams(
            presentation_id="wf", slide_number=1,
        ))
        assert r.success
        assert "Workflow Test" in r.data

        # 6. Finalize
        r = await self.module.finalize_presentation(FinalizePresentationParams(
            presentation_id="wf",
        ))
        assert r.success
        assert os.path.isfile(path)

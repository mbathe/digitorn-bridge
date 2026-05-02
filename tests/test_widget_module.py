"""Tests for the universal widget module + schema + compiler integration."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from digitorn.core.app.compiler import AppYAMLCompiler
from digitorn.core.app.errors import AppCompilationError
from digitorn.core.app.schema import AppDefinition
from digitorn.core.loader import load_modules
from digitorn.modules.registry import ModuleRegistry
from digitorn.modules.widget.module import (
    CloseParams,
    ErrorParams,
    RenderParams,
    UpdateParams,
    WidgetModule,
)


# ── fixtures ─────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def compiler() -> AppYAMLCompiler:
    reg = ModuleRegistry()
    load_modules(reg, load_all=True)
    return AppYAMLCompiler(reg)


def _wrap(yml_widgets: str) -> str:
    """Wrap a widgets: snippet in a minimal valid app.yaml.

    Uses provider: ollama because cloud providers now require an
    explicit credential reference (strict gate). The widget tests
    don't care about the brain - they care about the widgets:
    block - so a local provider is the cleanest fixture.
    """
    return f"""
app:
  app_id: t
  name: T
agents:
  - id: a
    role: coordinator
    brain:
      provider: ollama
      model: llama3
    system_prompt: hi
{yml_widgets}
"""


# ── schema parsing ────────────────────────────────────────────────


def test_widgets_parses_full_chat_side(compiler):
    yml = _wrap("""
widgets:
  version: 1
  chat_side:
    title: Sources
    icon: library_books
    width: 300
    accent: blue
    tree:
      type: column
      gap: 12
      children:
        - type: text
          text: hello
        - type: list
          items: '{{sources}}'
          item:
            type: card
            title: '{{item.title}}'
""")
    compiled = compiler.compile_string(yml)
    assert compiled.widgets is not None
    assert compiled.widgets.chat_side.title == "Sources"
    assert compiled.widgets.chat_side.tree.type == "column"
    assert len(compiled.widgets.chat_side.tree.children) == 2


def test_widgets_workspace_tabs_and_modals(compiler):
    yml = _wrap("""
widgets:
  version: 1
  workspace_tabs:
    - id: ops
      title: Ops
      tree: { type: column, children: [{ type: text, text: hi }] }
  modals:
    booking:
      title: Book
      width: 640
      tree: { type: form, children: [{ type: text_input, name: name }] }
""")
    compiled = compiler.compile_string(yml)
    assert len(compiled.widgets.workspace_tabs) == 1
    assert "booking" in compiled.widgets.modals
    assert compiled.widgets.modals["booking"].width == 640


def test_widgets_inline_named(compiler):
    yml = _wrap("""
widgets:
  version: 1
  inline:
    confirm_delete:
      tree:
        type: confirm
        text: 'Delete?'
        confirm_label: Delete
        destructive: true
        confirm_action: { action: tool, tool: delete_thing }
""")
    compiled = compiler.compile_string(yml)
    assert "confirm_delete" in compiled.widgets.inline


# ── compile-time validation ──────────────────────────────────────


def test_widgets_unknown_primitive_rejected(compiler):
    yml = _wrap("""
widgets:
  version: 1
  chat_side:
    title: x
    width: 300
    tree: { type: butan }
""")
    with pytest.raises(AppCompilationError) as exc:
        compiler.compile_string(yml)
    assert "unknown primitive" in str(exc.value).lower()


def test_widgets_unknown_action_rejected(compiler):
    yml = _wrap("""
widgets:
  version: 1
  chat_side:
    title: x
    width: 300
    tree:
      type: button
      label: Go
      action: { action: chatt, template: hi }
""")
    with pytest.raises(AppCompilationError) as exc:
        compiler.compile_string(yml)
    assert "unknown action" in str(exc.value).lower()


def test_widgets_unknown_accent_rejected(compiler):
    yml = _wrap("""
widgets:
  version: 1
  chat_side:
    title: x
    width: 300
    tree: { type: column, accent: rainbow }
""")
    with pytest.raises(AppCompilationError) as exc:
        compiler.compile_string(yml)
    assert "unknown accent" in str(exc.value).lower()


def test_widgets_version_gate(compiler):
    yml = _wrap("""
widgets:
  version: 99
""")
    with pytest.raises(AppCompilationError) as exc:
        compiler.compile_string(yml)
    assert "unsupported version" in str(exc.value).lower()


# ── external ./widgets/*.yaml loading ────────────────────────────


def test_widgets_external_files_loaded(compiler, tmp_path):
    bundle = tmp_path / "myapp"
    (bundle / "widgets").mkdir(parents=True)
    (bundle / "widgets" / "confirm_delete.yaml").write_text(
        """tree:
  type: confirm
  text: 'Delete?'
  confirm_label: Delete
  destructive: true
  confirm_action: { action: tool, tool: delete_thing }
""",
        encoding="utf-8",
    )
    (bundle / "app.yaml").write_text(
        _wrap(""),  # no inline widgets: block
        encoding="utf-8",
    )
    compiled = compiler.compile_file(bundle / "app.yaml")
    assert "confirm_delete" in compiled.widgets.inline
    assert compiled.widgets.inline["confirm_delete"].tree.type == "confirm"


def test_widgets_external_collision_rejected(compiler, tmp_path):
    bundle = tmp_path / "collision"
    (bundle / "widgets").mkdir(parents=True)
    (bundle / "widgets" / "foo.yaml").write_text(
        "tree: { type: text, text: external }\n",
        encoding="utf-8",
    )
    (bundle / "app.yaml").write_text(
        _wrap("""
widgets:
  version: 1
  inline:
    foo:
      tree: { type: text, text: inline }
"""),
        encoding="utf-8",
    )
    with pytest.raises(AppCompilationError) as exc:
        compiler.compile_file(bundle / "app.yaml")
    assert "collides" in str(exc.value).lower()


# ── module actions ───────────────────────────────────────────────


def test_widget_module_render_close_lifecycle():
    m = WidgetModule()
    m.set_active_session("s1")

    async def go():
        r = await m.render(RenderParams(
            zone="inline",
            tree={"type": "confirm", "text": "Delete?"},
            ctx={"path": "/foo"},
        ))
        assert r.success
        wid = r.data["widget_id"]

        snap = m.snapshot_for("s1")
        assert len(snap["mounted"]) == 1
        assert snap["mounted"][0]["widget_id"] == wid
        assert snap["seq"] == 1

        r = await m.update(UpdateParams(widget_id=wid, patch={"state.confirmed": True}))
        assert r.success
        snap = m.snapshot_for("s1")
        assert snap["seq"] == 2

        r = await m.close(CloseParams(widget_id=wid))
        assert r.success
        assert r.data["was_mounted"] is True
        snap = m.snapshot_for("s1")
        assert snap["mounted"] == []

    asyncio.run(go())


def test_widget_module_per_session_isolation():
    m = WidgetModule()

    async def go():
        m.set_active_session("alice")
        await m.render(RenderParams(zone="inline", tree={"type": "text", "text": "alice"}))

        m.set_active_session("bob")
        await m.render(RenderParams(zone="inline", tree={"type": "text", "text": "bob"}))

        snap_a = m.snapshot_for("alice")
        snap_b = m.snapshot_for("bob")
        assert len(snap_a["mounted"]) == 1
        assert len(snap_b["mounted"]) == 1
        assert snap_a["mounted"][0]["tree"]["text"] == "alice"
        assert snap_b["mounted"][0]["tree"]["text"] == "bob"

    asyncio.run(go())


def test_widget_module_bus_receives_events():
    m = WidgetModule()
    m.set_active_session("s1")

    class _Bus:
        def __init__(self):
            self.published = []

        @staticmethod
        def session_key(app_id, session_id, user_id):
            return f"{app_id}:{session_id}:{user_id}"

        async def publish(self, key, data):
            self.published.append((key, data))

    bus = _Bus()
    m._event_bus = bus
    m._bus_app_id = "test"

    async def go():
        await m.render(RenderParams(zone="inline", tree={"type": "text", "text": "x"}))
        await asyncio.sleep(0.05)
        assert len(bus.published) == 1
        _, data = bus.published[0]
        assert data["type"] == "widget:render"
        assert data["data"]["widget_seq"] == 1

    asyncio.run(go())


def test_widget_module_render_requires_ref_or_tree():
    m = WidgetModule()
    m.set_active_session("s1")

    async def go():
        r = await m.render(RenderParams(zone="inline"))
        assert r.success is False
        assert "ref" in r.error.lower() or "tree" in r.error.lower()

    asyncio.run(go())


def test_widget_module_unknown_zone_rejected():
    m = WidgetModule()
    m.set_active_session("s1")

    async def go():
        r = await m.render(RenderParams(zone="banana", tree={"type": "text", "text": "x"}))
        assert r.success is False
        assert "zone" in r.error.lower()

    asyncio.run(go())


def test_widget_module_error_action_publishes_event():
    m = WidgetModule()
    m.set_active_session("s1")

    async def go():
        await m.render(RenderParams(zone="inline", tree={"type": "list", "items": "{{x}}"}, widget_id="w1"))
        r = await m.error(ErrorParams(widget_id="w1", binding="x", message="boom"))
        assert r.success
        snap = m.snapshot_for("s1")
        assert snap["events"][-1]["event_type"] == "widget:error"
        assert snap["events"][-1]["data"]["message"] == "boom"

    asyncio.run(go())


# ── data binding resolver ────────────────────────────────────────


def test_data_static_binding_via_compile(compiler):
    """Static data sources are resolved verbatim by the daemon route."""
    yml = _wrap("""
widgets:
  version: 1
  chat_side:
    title: x
    width: 300
    data:
      priorities:
        type: static
        value:
          - low
          - med
          - high
    tree:
      type: list
      items: '{{priorities}}'
      item: { type: text, text: '{{item}}' }
""")
    compiled = compiler.compile_string(yml)
    # The static value is preserved on CompiledApp.widgets so the
    # /widgets/data/{binding} route can return it.
    assert compiled.widgets.chat_side.data["priorities"]["value"] == ["low", "med", "high"]


def test_data_http_binding_compiles(compiler):
    yml = _wrap("""
widgets:
  version: 1
  chat_side:
    title: x
    width: 300
    data:
      sources:
        type: http
        url: /rag/sources
        poll: 10s
    tree:
      type: list
      items: '{{sources}}'
      item: { type: card, title: '{{item.title}}' }
""")
    compiled = compiler.compile_string(yml)
    assert compiled.widgets.chat_side.data["sources"]["type"] == "http"
    assert compiled.widgets.chat_side.data["sources"]["url"] == "/rag/sources"


def test_data_tool_binding_compiles(compiler):
    yml = _wrap("""
widgets:
  version: 1
  workspace_tabs:
    - id: ops
      title: Ops
      data:
        summary:
          type: tool
          tool: summarize_docs
          args: { ids: '{{state.selected}}' }
      tree:
        type: column
        children:
          - { type: text, text: '{{summary.text}}' }
""")
    compiled = compiler.compile_string(yml)
    assert compiled.widgets.workspace_tabs[0].data["summary"]["tool"] == "summarize_docs"


# ── form values flow into tool args ──────────────────────────────


def test_form_to_tool_round_trip(compiler):
    """Verify the YAML compiles when a form action references {{form.x}}.

    The actual substitution happens client-side; this test just
    proves the schema accepts the pattern AND validation passes.
    """
    yml = _wrap("""
widgets:
  version: 1
  modals:
    booking:
      title: New booking
      width: 560
      tree:
        type: form
        id: booking_form
        children:
          - { type: text_input, name: topic, label: Topic, required: true }
          - { type: date,       name: when,  label: When,  required: true }
        submit:
          label: Book
          action:
            action: tool
            tool: create_meeting
            args:
              topic: '{{form.topic}}'
              when:  '{{form.when}}'
""")
    compiled = compiler.compile_string(yml)
    modal = compiled.widgets.modals["booking"]
    assert modal.tree.type == "form"
    submit = modal.tree.submit
    assert submit["action"]["action"] == "tool"
    assert submit["action"]["tool"] == "create_meeting"
    assert submit["action"]["args"]["topic"] == "{{form.topic}}"


# ── State as variables (P10) ────────────────────────────────────


def test_widget_state_set_and_get_dotted_path():
    from digitorn.modules.widget.module import GetStateParams, SetStateParams
    m = WidgetModule()
    m.set_active_session("s1")

    async def go():
        await m.set_state(SetStateParams(set={
            "form": {"email": "a@b.c", "topic": "x"},
            "selected_sources": ["s1", "s2"],
        }))
        r = await m.get_state(GetStateParams(key="form.email"))
        assert r.data == {"value": "a@b.c", "found": True}

        r = await m.get_state(GetStateParams(key="selected_sources"))
        assert r.data["value"] == ["s1", "s2"]

        r = await m.get_state(GetStateParams(key="nope.nope"))
        assert r.data == {"value": None, "found": False}

        # Full snapshot
        r = await m.get_state(GetStateParams())
        assert "form" in r.data["state"]
        assert r.data["state"]["selected_sources"] == ["s1", "s2"]

    asyncio.run(go())


def test_widget_prompt_sections_render_state():
    """get_prompt_sections() exposes form / state / results to the agent."""
    from digitorn.modules.widget.module import SetStateParams
    m = WidgetModule()
    m.set_active_session("alice")

    async def go():
        # Empty state → no section
        assert m.get_prompt_sections() == []

        await m.set_state(SetStateParams(set={
            "form": {"email": "alice@example.com"},
            "selected_sources": ["a", "b", "c"],
        }))
        # Simulate a tool result (the API auto-promotes these)
        sess = m._store.get_or_create("alice")
        sess.state["last_result"] = {
            "tool": "rag.query",
            "value": {"hits": 12, "score": 0.94},
        }

        sections = m.get_prompt_sections()
        assert len(sections) == 1
        assert sections[0]["title"] == "WIDGET CONTEXT"
        content = sections[0]["content"]
        assert "alice@example.com" in content
        assert "selected_sources" in content
        assert "rag.query" in content
        assert "0.94" in content

    asyncio.run(go())


def test_widget_prompt_sections_per_session_isolation():
    m = WidgetModule()
    from digitorn.modules.widget.module import SetStateParams

    async def go():
        m.set_active_session("alice")
        await m.set_state(SetStateParams(set={"form": {"name": "Alice"}}))
        m.set_active_session("bob")
        await m.set_state(SetStateParams(set={"form": {"name": "Bob"}}))

        m.set_active_session("alice")
        sec_a = m.get_prompt_sections()[0]["content"]
        assert "Alice" in sec_a and "Bob" not in sec_a

        m.set_active_session("bob")
        sec_b = m.get_prompt_sections()[0]["content"]
        assert "Bob" in sec_b and "Alice" not in sec_b

    asyncio.run(go())


def test_widget_prompt_sections_empty_when_no_session():
    m = WidgetModule()
    # No active session
    assert m.get_prompt_sections() == []


# ── Expression evaluator ──────────────────────────────────────────


def test_expr_simple_lookup():
    from digitorn.modules.widget.expr import evaluate
    scopes = {"form": {"name": "Alice", "age": 30}}
    assert evaluate("form.name", scopes) == "Alice"
    assert evaluate("form.age", scopes) == 30
    assert evaluate("form.missing", scopes) is None


def test_expr_filters():
    from digitorn.modules.widget.expr import substitute
    scopes = {
        "x": "Hello world this is a long string",
        "items": [1, 2, 3, 4, 5],
        "name": "alice",
    }
    assert substitute("{{x | truncate(10)}}", scopes) == "Hello wor…"
    assert substitute("{{items | length}}", scopes) == 5
    assert substitute("{{name | upper}}", scopes) == "ALICE"
    assert substitute("{{name | title}}", scopes) == "Alice"


def test_expr_pipeline():
    from digitorn.modules.widget.expr import substitute
    scopes = {"items": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}
    assert substitute("{{items | length}}", scopes) == 3


def test_expr_comparison_and_ternary():
    from digitorn.modules.widget.expr import evaluate
    scopes = {"count": 5}
    assert evaluate("count > 0", scopes) is True
    assert evaluate("count > 100", scopes) is False
    assert evaluate("count > 0 ? 'yes' : 'no'", scopes) == "yes"


def test_expr_is_empty():
    from digitorn.modules.widget.expr import evaluate
    assert evaluate("x is empty", {"x": []}) is True
    assert evaluate("x is empty", {"x": [1, 2]}) is False
    assert evaluate("x is empty", {"x": ""}) is True
    assert evaluate("x is empty", {"x": None}) is True


def test_expr_string_interpolation():
    from digitorn.modules.widget.expr import substitute
    scopes = {"form": {"name": "Alice"}, "count": 12}
    assert substitute("Hello {{form.name}}, you have {{count}} items", scopes) == "Hello Alice, you have 12 items"


def test_expr_tree_substitution_preserves_structure():
    from digitorn.modules.widget.expr import substitute_tree
    scopes = {"form": {"name": "Bob"}, "count": 3}
    tree = {
        "type": "card",
        "title": "Hello {{form.name}}",
        "children": [
            {"type": "text", "text": "{{count}} items"},
        ],
    }
    out = substitute_tree(tree, scopes)
    assert out["title"] == "Hello Bob"
    assert out["children"][0]["text"] == "3 items"


# ── Server-side render substitution ───────────────────────────────


def test_render_substitutes_against_session_state():
    from digitorn.modules.widget.module import RenderParams, SetStateParams

    m = WidgetModule()
    m.set_active_session("u1")

    async def go():
        await m.set_state(SetStateParams(set={"form": {"name": "Alice"}, "count": 7}))
        await m.render(RenderParams(
            zone="inline",
            widget_id="w1",
            tree={
                "type": "card",
                "title": "Hello {{form.name}}",
                # State scalars are addressed as state.X per spec §6.1
                "subtitle": "{{state.count}} items",
            },
        ))
        snap = m.snapshot_for("u1")
        rendered = snap["mounted"][0]["tree"]
        assert rendered["title"] == "Hello Alice"
        assert rendered["subtitle"] == "7 items"

    asyncio.run(go())


def test_render_substitutes_ctx_variables():
    from digitorn.modules.widget.module import RenderParams

    m = WidgetModule()
    m.set_active_session("u1")

    async def go():
        await m.render(RenderParams(
            zone="inline",
            widget_id="w1",
            tree={"type": "text", "text": "Path: {{ctx.path}}"},
            ctx={"path": "/foo/bar.md"},
        ))
        snap = m.snapshot_for("u1")
        assert snap["mounted"][0]["tree"]["text"] == "Path: /foo/bar.md"

    asyncio.run(go())


# ── Form validation re-check ─────────────────────────────────────


def test_validate_form_required_field():
    from digitorn.modules.widget.validate import validate_form_values
    inputs = {
        "email": {"type": "text_input", "required": True, "type_hint": "email"},
        "age": {"type": "text_input", "required": True, "validation": {"min": 1, "max": 120}},
    }
    ok, errors = validate_form_values(inputs, {"email": "a@b.c", "age": 30})
    assert ok and errors == {}

    ok, errors = validate_form_values(inputs, {})
    assert not ok
    assert "email" in errors and "required" in errors["email"]


def test_validate_form_email_format():
    from digitorn.modules.widget.validate import validate_form_values
    inputs = {"email": {"type": "text_input", "type_hint": "email"}}
    ok, errors = validate_form_values(inputs, {"email": "alice@example.com"})
    assert ok
    ok, errors = validate_form_values(inputs, {"email": "notanemail"})
    assert not ok and "valid email" in errors["email"]


def test_validate_form_min_max_string_length():
    from digitorn.modules.widget.validate import validate_form_values
    inputs = {"topic": {"type": "text_input", "validation": {"min": 3, "max": 10}}}
    ok, _ = validate_form_values(inputs, {"topic": "abcd"})
    assert ok
    ok, errors = validate_form_values(inputs, {"topic": "ab"})
    assert not ok and "at least 3" in errors["topic"]
    ok, errors = validate_form_values(inputs, {"topic": "abcdefghijk"})
    assert not ok and "at most 10" in errors["topic"]


def test_validate_form_collects_inputs_from_compiled_widgets(compiler):
    from digitorn.modules.widget.validate import collect_form_inputs

    yml = _wrap("""
widgets:
  version: 1
  modals:
    booking:
      title: Book
      width: 560
      tree:
        type: form
        children:
          - { type: text_input, name: topic, required: true }
          - { type: select, name: priority, options: [{value: low, label: Low}] }
""")
    compiled = compiler.compile_string(yml)
    inputs = collect_form_inputs(compiled.widgets)
    assert "topic" in inputs
    assert "priority" in inputs
    assert inputs["topic"]["required"] is True


def test_form_input_name_uniqueness_enforced(compiler):
    """Two inputs with the same name in one form must fail compile."""
    yml = _wrap("""
widgets:
  version: 1
  modals:
    booking:
      title: x
      width: 560
      tree:
        type: form
        children:
          - { type: text_input, name: topic, label: A }
          - { type: text_input, name: topic, label: B }
""")
    with pytest.raises(AppCompilationError) as exc:
        compiler.compile_string(yml)
    assert "duplicate input name" in str(exc.value).lower()

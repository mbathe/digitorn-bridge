"""Tools view - searchable tool catalog with categories and details."""

from __future__ import annotations

from typing import Any, Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Static, ListView, ListItem, Input


class ToolItem(ListItem):
    """A single tool row."""

    def __init__(self, tool: dict[str, Any]) -> None:
        super().__init__()
        self.tool_data = tool

    def compose(self) -> ComposeResult:
        t = self.tool_data
        name = t.get("name", t.get("short_name", "?"))
        risk = t.get("risk_level", "low")
        desc = (t.get("description", "") or "")[:50]

        _RISK_COLORS = {"low": "#22c55e", "medium": "#f59e0b", "high": "#ef4444"}
        color = _RISK_COLORS.get(risk, "#94a3b8")

        line = Text()
        line.append(f"  {name:<20}", style="bold white")
        line.append(f" {risk:<7}", style=color)
        line.append(f" {desc}", style="dim")
        yield Static(line)


class ToolDetail(Static):
    """Right panel showing tool details."""

    def update_tool(self, tool: dict[str, Any]) -> None:
        t = Text()
        name = tool.get("name", tool.get("short_name", "?"))
        desc = tool.get("description", "") or ""
        risk = tool.get("risk_level", "low")
        module = tool.get("module_id", tool.get("category", ""))

        t.append(f"  {name}\n", style="bold white")
        t.append(f"  {module}\n\n", style="dim")

        t.append("  Risk      ", style="bold #94a3b8")
        _RISK_COLORS = {"low": "#22c55e", "medium": "#f59e0b", "high": "#ef4444"}
        t.append(f"{risk}\n", style=_RISK_COLORS.get(risk, "white"))

        if desc:
            t.append("\n  ", style="")
            # Word wrap description
            words = desc.split()
            line_len = 2
            for w in words:
                if line_len + len(w) > 36:
                    t.append(f"\n  ", style="")
                    line_len = 2
                t.append(f"{w} ", style="#e2e8f0")
                line_len += len(w) + 1

        # Parameters
        schema = tool.get("input_schema", tool.get("parameters", {}))
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = set(schema.get("required", [])) if isinstance(schema, dict) else set()

        if props:
            t.append("\n\n  Parameters:\n", style="bold #94a3b8")
            for pname, pinfo in props.items():
                ptype = pinfo.get("type", "any") if isinstance(pinfo, dict) else "any"
                req = " *" if pname in required else ""
                pdesc = pinfo.get("description", "")[:30] if isinstance(pinfo, dict) else ""
                t.append(f"    {pname}", style="bold cyan")
                t.append(f" {ptype}{req}", style="dim")
                if pdesc:
                    t.append(f"\n      {pdesc}", style="#94a3b8")
                t.append("\n")

        self.update(t)


class ToolsScreen(Screen):
    """Modal screen for browsing the tool catalog."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", priority=True),
    ]

    DEFAULT_CSS = """
    ToolsScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    #tools-modal {
        width: 90%;
        height: 80%;
        background: #0f172a;
        border: tall #334155;
    }

    #tools-header {
        height: 3;
        background: #111827;
        layout: horizontal;
        padding: 1 2;
    }

    #tools-search {
        width: 1fr;
        background: #1e293b;
        border: none;
        color: #f1f5f9;
    }

    #tools-search:focus {
        border: none;
    }

    #tools-body {
        height: 1fr;
        layout: horizontal;
    }

    #tools-list {
        width: 1fr;
        background: #0a0e1a;
        border-right: solid #1e293b;
    }

    #tools-list > ListItem {
        height: 1;
        padding: 0;
    }

    #tools-list > ListItem.--highlight {
        background: #1e293b;
    }

    #tools-detail {
        width: 40;
        background: #0f1320;
        padding: 1;
    }

    #tools-footer {
        height: 1;
        background: #111827;
        padding: 0 2;
    }

    #tools-count {
        width: auto;
        padding: 1 2 1 0;
    }
    """

    def __init__(self, fetch_tools: Callable) -> None:
        super().__init__()
        self._fetch_tools = fetch_tools
        self._all_tools: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="tools-modal"):
            with Horizontal(id="tools-header"):
                yield Static(Text("  🔧 Tools  ", style="bold white"), id="tools-count")
                yield Input(placeholder="Search tools...", id="tools-search")
            with Horizontal(id="tools-body"):
                yield ListView(id="tools-list")
                yield ToolDetail("  Loading...", id="tools-detail")
            yield Static(
                Text.assemble(
                    ("  ↑↓ ", "bold cyan"), ("Navigate  ", "dim"),
                    ("Enter ", "bold cyan"), ("Details  ", "dim"),
                    ("Type to search  ", "dim"),
                    ("Esc ", "bold"), ("Close", "dim"),
                ),
                id="tools-footer",
            )

    async def on_mount(self) -> None:
        await self._load_tools()

    async def _load_tools(self, query: str = "") -> None:
        try:
            self._all_tools = await self._fetch_tools(query)
        except Exception as e:
            self.query_one("#tools-detail", ToolDetail).update(
                Text(f"  Error: {e}", style="red")
            )
            return

        self._populate_list(self._all_tools)

    def _populate_list(self, tools: list[dict]) -> None:
        lv = self.query_one("#tools-list", ListView)
        lv.clear()
        for tool in tools:
            lv.append(ToolItem(tool))

        count = self.query_one("#tools-count", Static)
        count.update(Text(f"  🔧 {len(tools)} tools  ", style="bold white"))

        if tools:
            self.query_one("#tools-detail", ToolDetail).update_tool(tools[0])

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.lower()
        if not query:
            self._populate_list(self._all_tools)
            return
        filtered = [
            t for t in self._all_tools
            if query in (t.get("name", "") + t.get("description", "")).lower()
        ]
        self._populate_list(filtered)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item and isinstance(event.item, ToolItem):
            self.query_one("#tools-detail", ToolDetail).update_tool(event.item.tool_data)

    def action_cancel(self) -> None:
        self.app.pop_screen()

"""CodePreview - split pane showing file contents on the right."""

from __future__ import annotations


import logging

logger = logging.getLogger(__name__)
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from rich.syntax import Syntax
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static


_EXT_TO_LANG = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".json": "json", ".jsonl": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "scss",
    ".md": "markdown",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".sql": "sql",
    ".rs": "rust",
    ".go": "go",
    ".rb": "ruby",
    ".java": "java",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp",
    ".xml": "xml",
    ".txt": "text",
}


def _detect_lang(path: str) -> str:
    """Detect syntax language from file extension."""
    for ext, lang in _EXT_TO_LANG.items():
        if path.lower().endswith(ext):
            return lang
    return "text"


def _short_path(path: str) -> str:
    """Get just the filename from a path."""
    try:
        return PureWindowsPath(path).name or PurePosixPath(path).name or path
    except Exception:
        return path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


class CodePreview(VerticalScroll):
    """Scrollable code preview panel."""

    DEFAULT_CSS = """
    CodePreview {
        width: 50%;
        background: #0a0e1a;
        border-left: solid #1e293b;
        padding: 0;
        display: none;
    }

    CodePreview.visible {
        display: block;
    }

    CodePreview .preview-header {
        height: 1;
        background: #111827;
        padding: 0 2;
    }

    CodePreview .preview-content {
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._current_path = ""
        self._history: list[dict[str, str]] = []  # [{path, content, action}]
        self._MAX_HISTORY = 20

    def show_file(self, path: str, content: str, action: str = "read") -> None:
        """Display a file in the preview pane."""
        self._current_path = path
        self._history = [h for h in self._history if h["path"] != path]
        self._history.insert(0, {"path": path, "content": content, "action": action})
        self._history = self._history[:self._MAX_HISTORY]

        self._render_file(path, content, action)
        if not self.has_class("visible"):
            self.add_class("visible")

    def _render_file(self, path: str, content: str, action: str) -> None:
        """Render file with syntax highlighting."""
        # Clear current content
        for child in list(self.children):
            try:
                child.remove()
            except Exception as exc:
                logger.debug("code_preview best-effort block failed: %s", exc)

        # Header
        icon = {"read": "\u2193", "write": "\u270e", "edit": "\u270e"}.get(action, "\u2022")
        color = {"read": "#60a5fa", "write": "#f59e0b", "edit": "#f59e0b"}.get(action, "#94a3b8")
        header = Text()
        header.append(f"  {icon} ", style=f"bold {color}")
        header.append(_short_path(path), style="bold white")
        header.append(f"  ({len(content.splitlines())} lines)", style="dim")
        self.mount(Static(header, classes="preview-header"))

        # Content with syntax highlighting
        lang = _detect_lang(path)
        try:
            syntax = Syntax(
                content,
                lang,
                theme="monokai",
                line_numbers=True,
                word_wrap=False,
            )
            self.mount(Static(syntax, classes="preview-content"))
        except Exception:
            self.mount(Static(content, classes="preview-content"))

        self.scroll_home()

    def toggle(self) -> None:
        """Toggle visibility."""
        self.toggle_class("visible")

    def close(self) -> None:
        self.remove_class("visible")

    @property
    def is_visible(self) -> bool:
        return self.has_class("visible")

"""Filesystem module - file tree tracker."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

class _TrackedFile:
    __slots__ = ("path", "action", "size", "timestamp", "is_dir", "insertions", "deletions")

    def __init__(
        self, path: str, action: str, size: int = 0,
        is_dir: bool = False, insertions: int = 0, deletions: int = 0,
    ) -> None:
        self.path = path
        self.action = action
        self.size = size
        self.is_dir = is_dir
        self.insertions = insertions
        self.deletions = deletions
        self.timestamp = time.time()

# Icons for file types (sent as tree node icons)
_EXT_ICON: dict[str, str] = {
    ".py": "\U0001f40d",    # snake
    ".ts": "\u24c9",        # circled T
    ".tsx": "\u24c9",
    ".js": "\u24bf",        # circled J
    ".jsx": "\u24bf",
    ".json": "{}",
    ".yaml": "\u2699",      # gear
    ".yml": "\u2699",
    ".toml": "\u2699",
    ".md": "\u270e",        # pencil
    ".sql": "\u2316",       # target
    ".html": "\u2b50",
    ".css": "\U0001f3a8",   # palette
    ".xlsx": "\U0001f4ca",  # chart
    ".pptx": "\U0001f4ca",
    ".pdf": "\U0001f4c4",   # page
    ".png": "\U0001f5bc",   # image
    ".jpg": "\U0001f5bc",
    ".jpeg": "\U0001f5bc",
    ".svg": "\U0001f5bc",
}

def _icon_for(path: str, is_dir: bool) -> str:
    if is_dir:
        return "\U0001f4c1"  # folder
    ext = os.path.splitext(path)[1].lower()
    return _EXT_ICON.get(ext, "\U0001f4c4")  # default: page

def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.1f}MB"

# Action → (label, badge letter, CSS color variable)
# Mirrors VS Code git status: A=green, M=yellow, D=red, R=blue
_ACTION_META: dict[str, tuple[str, str, str]] = {
    "read":       ("read",        "",  "var(--text-muted)"),
    "write":      ("created",     "A", "var(--green-text)"),
    "edit":       ("edited",      "M", "var(--yellow-text)"),
    "rm":         ("deleted",     "D", "var(--red-text)"),
    "mv_src":     ("moved",       "R", "var(--purple-text)"),
    "mv_dst":     ("moved here",  "R", "var(--purple-text)"),
    "cp_dst":     ("copied here", "A", "var(--green-text)"),
}

class FilesystemRenderer:
    """Workbench renderer for the filesystem shadow buffer."""

    def __init__(self, workspace_root: str | None = None) -> None:
        self._workspace_root = workspace_root
        self._tracked: dict[str, _TrackedFile] = {}

    def track(
        self,
        path: str,
        action: str,
        size: int = 0,
        is_dir: bool = False,
        insertions: int = 0,
        deletions: int = 0,
    ) -> None:
        """Record that a file was touched by the agent."""
        existing = self._tracked.get(path)
        if existing and action in ("edit", "write", "insert"):
            # Accumulate line changes across multiple edits to the same file
            insertions = existing.insertions + insertions
            deletions = existing.deletions + deletions
        self._tracked[path] = _TrackedFile(
            path=path, action=action, size=size, is_dir=is_dir,
            insertions=insertions, deletions=deletions,
        )

    def untrack(self, path: str) -> None:
        """Remove a file from tracking (e.g. after rm)."""
        self._tracked.pop(path, None)

    def to_dict(self) -> dict[str, Any]:
        """Serialize tracked files for session persistence."""
        return {
            "workspace_root": self._workspace_root,
            "files": {
                path: {
                    "action": tf.action,
                    "size": tf.size,
                    "is_dir": tf.is_dir,
                    "insertions": tf.insertions,
                    "deletions": tf.deletions,
                    "timestamp": tf.timestamp,
                }
                for path, tf in self._tracked.items()
            },
        }

    def restore_from_dict(self, data: dict[str, Any]) -> None:
        """Restore tracked files from session persistence."""
        if not data:
            return
        if data.get("workspace_root"):
            self._workspace_root = data["workspace_root"]
        for path, info in data.get("files", {}).items():
            self._tracked[path] = _TrackedFile(
                path=path,
                action=info.get("action", "read"),
                size=info.get("size", 0),
                is_dir=info.get("is_dir", False),
                insertions=info.get("insertions", 0),
                deletions=info.get("deletions", 0),
            )
            self._tracked[path].timestamp = info.get("timestamp", time.time())

    def snapshot(
        self, buffer: Any, action: str = "write", added_content: str = "",
    ) -> str:
        n = len(self._tracked)
        dirs = sum(1 for f in self._tracked.values() if f.is_dir)
        files = n - dirs
        return f"Workspace - {files} files, {dirs} dirs tracked"

    def preview(self, buffer: Any) -> str:
        return self.snapshot(buffer)

    def validate(self, buffer: Any) -> list:
        return []

    def summary_line(self, buffer: Any) -> str:
        n = len(self._tracked)
        return f"{n} files tracked"

    def post_use_summary(
        self, buffer: Any, tool_name: str, result: Any,
    ) -> str:
        return ""

    @staticmethod
    def _section_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
        return {"type": "stats", "items": items}

    @staticmethod
    def _section_tree(nodes: list[dict[str, Any]], title: str = "") -> dict[str, Any]:
        d: dict[str, Any] = {"type": "tree", "nodes": nodes}
        if title:
            d["title"] = title
        return d

    def event_payload(self, buffer: Any) -> dict[str, Any]:
        """Build tree + stats sections from tracked files."""

        if not self._tracked:
            return {"sections": []}

        root = self._workspace_root
        # Fallback: if no workspace root configured, infer from common prefix
        if not root:
            abs_paths = [f.path for f in self._tracked.values() if os.path.isabs(f.path)]
            if abs_paths:
                root = os.path.commonpath(abs_paths)
                # If commonpath is a file, use its parent
                if root and not os.path.isdir(root):
                    root = os.path.dirname(root)
        tracked = sorted(
            self._tracked.values(),
            key=lambda f: f.path,
        )

        # Build tree nodes
        nodes: list[dict[str, Any]] = []
        seen_dirs: set[str] = set()

        for tf in tracked:
            rel = tf.path
            if root:
                try:
                    rel = str(Path(tf.path).relative_to(root))
                except ValueError:
                    pass

            # Insert parent directories as tree nodes
            parts = Path(rel).parts
            for depth in range(len(parts) - (0 if tf.is_dir else 1)):
                dir_path = str(Path(*parts[: depth + 1]))
                if dir_path not in seen_dirs:
                    seen_dirs.add(dir_path)
                    nodes.append({
                        "label": parts[depth] + "/",
                        "level": depth,
                        "icon": "\U0001f4c1",
                    })

            # Skip dir entries that were already added as parents
            if tf.is_dir:
                continue

            # The file itself
            level = len(parts) - 1
            is_recent = (time.time() - tf.timestamp) < 5
            meta = _ACTION_META.get(tf.action, ("", "", "var(--text-muted)"))
            label_text, badge, color = meta

            # Build detail string: "edited · 1.2KB · +12 -3"
            detail_parts: list[str] = []
            if label_text:
                detail_parts.append(label_text)
            if tf.size:
                detail_parts.append(_human_size(tf.size))

            node: dict[str, Any] = {
                "label": parts[-1],
                "level": max(level, 0),
                "icon": _icon_for(tf.path, False),
                "active": is_recent,
                "detail": " · ".join(detail_parts),
                "color": color,
            }
            if badge:
                node["badge"] = badge
            if tf.action != "read":
                node["status"] = tf.action
            # Line change counts (like VS Code git gutter)
            if tf.insertions or tf.deletions:
                node["insertions"] = tf.insertions
                node["deletions"] = tf.deletions

            nodes.append(node)

        # Stats
        files_count = sum(1 for f in self._tracked.values() if not f.is_dir)
        dirs_count = sum(1 for f in self._tracked.values() if f.is_dir)
        total_size = sum(f.size for f in self._tracked.values() if not f.is_dir)

        writes = sum(1 for f in self._tracked.values() if f.action in ("write", "edit", "insert"))
        reads = sum(1 for f in self._tracked.values() if f.action == "read")
        total_insertions = sum(f.insertions for f in self._tracked.values())
        total_deletions = sum(f.deletions for f in self._tracked.values())

        stat_items: list[dict[str, Any]] = [
            {"label": "files", "value": files_count},
        ]
        if dirs_count:
            stat_items.append({"label": "dirs", "value": dirs_count})
        if total_size:
            stat_items.append({"label": "total", "value": _human_size(total_size)})
        if writes:
            stat_items.append({"label": "modified", "value": writes, "color": "var(--yellow-text)"})
        if total_insertions:
            stat_items.append({"label": "insertions", "value": f"+{total_insertions}", "color": "var(--green-text)"})
        if total_deletions:
            stat_items.append({"label": "deletions", "value": f"-{total_deletions}", "color": "var(--red-text)"})
        if reads:
            stat_items.append({"label": "read", "value": reads})

        sections = [
            self._section_stats(stat_items),
            self._section_tree(nodes, title="Workspace"),
        ]
        return {"sections": sections}

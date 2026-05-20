"""Source location tracking for behavior YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from ruamel.yaml import YAML
    _YAML = YAML(typ="rt")
    _YAML.preserve_quotes = True
    _RUAMEL_AVAILABLE = True
except ImportError:
    _YAML = None
    _RUAMEL_AVAILABLE = False

class SourceMap:
    """Maps dotted path (rule_definitions[3].condition.counter_gte)."""

    def __init__(self) -> None:
        self._map: dict[str, tuple[str, int, int]] = {}
        self._default_file: str = ""

    def set_default_file(self, path: str) -> None:
        """The main app.yaml path, used when a key has no override."""
        self._default_file = path

    def set(self, path: str, file: str, line: int, col: int = 0) -> None:
        self._map[path] = (file, line, col)

    def get(self, path: str) -> tuple[str, int, int]:
        """Return (file, line, col). Falls back to ancestor path if exact not found."""
        if path in self._map:
            return self._map[path]
        # Walk up: "a.b.c" → "a.b" → "a"
        p = path
        while "." in p or "[" in p:
            p = _parent_path(p)
            if p in self._map:
                return self._map[p]
        if self._default_file:
            return (self._default_file, 0, 0)
        return ("", 0, 0)

    def format_location(self, path: str) -> str:
        file, line, col = self.get(path)
        if not file:
            return path
        if line:
            return f"{file}:{line}"
        return file

def _parent_path(path: str) -> str:
    last_dot = path.rfind(".")
    last_bracket = path.rfind("[")
    cut = max(last_dot, last_bracket)
    if cut == -1:
        return ""
    return path[:cut]

def load_with_source(
    yaml_path: str | Path,
) -> tuple[dict, SourceMap] | tuple[None, SourceMap]:
    """Load a YAML file and build a source map of line numbers."""
    src_map = SourceMap()
    p = Path(yaml_path).resolve()
    src_map.set_default_file(str(p))

    if not _RUAMEL_AVAILABLE:
        import yaml as _pyyaml
        with open(p, "r", encoding="utf-8") as f:
            data = _pyyaml.safe_load(f) or {}
        return data, src_map

    with open(p, "r", encoding="utf-8") as f:
        data = _YAML.load(f)
    if data is None:
        return {}, src_map

    # Walk the tree, populate the source map
    _walk(data, src_map, "", str(p))
    return _to_plain(data), src_map

def _walk(node: Any, src_map: SourceMap, path: str, file: str) -> None:
    lc = getattr(node, "lc", None)
    if lc is not None:
        # lc.line/col are 0-based; make them 1-based for user display
        line = (lc.line or 0) + 1
        col = (lc.col or 0) + 1
        if path:
            src_map.set(path, file, line, col)

    if isinstance(node, dict) or (hasattr(node, "items") and hasattr(node, "lc")):
        for k, v in node.items():
            child_path = f"{path}.{k}" if path else k
            # lc.data holds per-key positions in ruamel CommentedMap
            key_lc = lc.data.get(k) if lc and hasattr(lc, "data") and isinstance(lc.data, dict) else None
            if key_lc:
                # key_lc = (key_line, key_col, value_line, value_col)
                src_map.set(child_path, file, (key_lc[2] or 0) + 1, (key_lc[3] or 0) + 1)
            _walk(v, src_map, child_path, file)
    elif isinstance(node, list) or (hasattr(node, "lc") and hasattr(node, "__iter__") and not isinstance(node, str)):
        for i, item in enumerate(node):
            child_path = f"{path}[{i}]"
            item_lc = lc.data.get(i) if lc and hasattr(lc, "data") and isinstance(lc.data, dict) else None
            if item_lc:
                src_map.set(child_path, file, (item_lc[0] or 0) + 1, (item_lc[1] or 0) + 1)
            _walk(item, src_map, child_path, file)

def _to_plain(node: Any) -> Any:
    if isinstance(node, dict) or hasattr(node, "items"):
        return {k: _to_plain(v) for k, v in node.items()}
    if isinstance(node, list) or (hasattr(node, "__iter__") and not isinstance(node, (str, bytes))):
        try:
            return [_to_plain(x) for x in node]
        except TypeError:
            pass
    return node

def overlay_included_file(
    src_map: SourceMap,
    included_yaml_path: str | Path,
    at_parent_path: str,
) -> None:
    """When `{{behavior.X}}` or `{{include:Y}}` inlines another."""
    if not _RUAMEL_AVAILABLE:
        return

    p = Path(included_yaml_path).resolve()
    if not p.exists():
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = _YAML.load(f)
    except Exception:
        return
    if data is None:
        return

    # Walk and record inner paths RELATIVE to at_parent_path
    _walk(data, src_map, at_parent_path, str(p))

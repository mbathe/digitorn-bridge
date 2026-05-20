"""YAML loader that preserves line/column info for compiler error reports."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml


@dataclass(frozen=True)
class Position:
    source: str
    line: int
    column: int

    def __str__(self) -> str:
        return f"{self.source}:{self.line}:{self.column}"


PositionMap = dict[tuple, Position]


class _PositionedLoader(yaml.SafeLoader):
    pass


_BOOL_TAG = "tag:yaml.org,2002:bool"
_STRICT_BOOL_RE = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")


def _install_strict_bool_resolver(loader_cls: type) -> None:
    cls_resolvers = dict(loader_cls.yaml_implicit_resolvers)
    for first_char, resolvers in list(cls_resolvers.items()):
        filtered = [
            (tag, pat) for (tag, pat) in resolvers if tag != _BOOL_TAG
        ]
        if filtered:
            cls_resolvers[first_char] = filtered
        else:
            cls_resolvers.pop(first_char, None)
    loader_cls.yaml_implicit_resolvers = cls_resolvers
    for first_char in "tTfF":
        loader_cls.add_implicit_resolver(
            _BOOL_TAG, _STRICT_BOOL_RE, [first_char],
        )


_install_strict_bool_resolver(_PositionedLoader)


def _mapping_node_to_dict(loader, node):
    loader.flatten_mapping(node)
    result = {}
    meta_keys = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        value = loader.construct_object(value_node, deep=True)
        result[key] = value
        meta_keys[key] = (
            key_node.start_mark.line + 1,
            key_node.start_mark.column + 1,
        )
    result["__positions__"] = meta_keys
    result["__node_pos__"] = (
        node.start_mark.line + 1,
        node.start_mark.column + 1,
    )
    return result


def _sequence_node_to_list(loader, node):
    items = [loader.construct_object(c, deep=True) for c in node.value]
    child_pos = [
        (c.start_mark.line + 1, c.start_mark.column + 1) for c in node.value
    ]
    return {"__seq__": items, "__child_pos__": child_pos, "__node_pos__": (
        node.start_mark.line + 1, node.start_mark.column + 1,
    )}


_PositionedLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_node_to_dict,
)
_PositionedLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_SEQUENCE_TAG, _sequence_node_to_list,
)


def _strip_meta(node: Any, path: tuple, source: str, positions: PositionMap) -> Any:
    if isinstance(node, dict) and "__seq__" in node:
        child_pos = node.get("__child_pos__", [])
        items = node["__seq__"]
        clean: list = []
        for i, item in enumerate(items):
            new_path = path + (i,)
            if i < len(child_pos):
                line, col = child_pos[i]
                positions[new_path] = Position(source, line, col)
            clean.append(_strip_meta(item, new_path, source, positions))
        return clean
    if isinstance(node, dict):
        meta_keys = node.pop("__positions__", {})
        node.pop("__node_pos__", None)
        for k, (line, col) in meta_keys.items():
            positions[path + (k,)] = Position(source, line, col)
        return {
            k: _strip_meta(v, path + (k,), source, positions)
            for k, v in node.items()
        }
    return node


def load_with_positions(
    text: str, source: str = "<string>",
) -> tuple[Any, PositionMap]:
    """Parse YAML and return `(data, positions)`."""
    raw = yaml.load(text, Loader=_PositionedLoader)
    positions: PositionMap = {}
    if raw is None:
        return None, positions
    clean = _strip_meta(raw, (), source, positions)
    return clean, positions


def format_location(positions: PositionMap, path: tuple, source: str = "") -> str:
    """Return `"app.yaml:42:7"` for `path`, with fallback walks."""
    cur = path
    while cur:
        pos = positions.get(cur)
        if pos is not None:
            return str(pos)
        cur = cur[:-1]
    return source or "<unknown>"


def pydantic_loc_to_path(loc: tuple) -> tuple:
    """Normalize a Pydantic error `loc` to a position-map key tuple."""
    out: list = []
    for part in loc:
        if isinstance(part, int):
            out.append(part)
        elif isinstance(part, str):
            out.append(part)
    return tuple(out)


def merge_positions(
    main: PositionMap,
    sub: PositionMap,
    prefix: tuple,
) -> None:
    """Merge positions from a sub-file's map into the main map under `prefix`."""
    for sub_path, pos in sub.items():
        main[prefix + sub_path] = pos
    if prefix and prefix not in main and sub:
        root_pos = sub.get(())
        if root_pos is not None:
            main[prefix] = root_pos


class _StrictBoolLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 strict bool semantics - no position tracking."""


_install_strict_bool_resolver(_StrictBoolLoader)


def safe_load_strict(text: str) -> Any:
    """Drop-in replacement for `yaml.safe_load` with YAML 1.2 bool rules."""
    return yaml.load(text, Loader=_StrictBoolLoader)


def load_frontmatter_with_positions(
    text: str, source: str,
) -> tuple[dict[str, Any], str, PositionMap]:
    """Parse a Markdown file with YAML frontmatter, returning"""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text, {}
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, text, {}
    fm_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:])
    fm_data, fm_positions = load_with_positions(fm_text, source=source)
    offset = 1
    shifted: PositionMap = {}
    for path, pos in fm_positions.items():
        shifted[path] = Position(pos.source, pos.line + offset, pos.column)
    return (fm_data or {}), body, shifted

"""Fragmentation loader - compose an AppDefinition from a directory tree."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from digitorn.core.app.yaml_loader import safe_load_strict


CONVENTION_DIRS: dict[str, str] = {
    "agents": "agents",       # list of agent definitions
    "hooks": "hooks",         # list of hook configs (under execution.hooks)
}

CONVENTION_FILES: dict[str, str] = {
    "templates": "templates.yaml",
}


ReadFn = Callable[[str], Optional[str]]


def _make_filesystem_reader(source_dir: Path) -> ReadFn:
    def _read(rel_path: str) -> Optional[str]:
        path = (source_dir / rel_path).resolve()
        try:
            path.relative_to(source_dir.resolve())
        except ValueError:
            return None  # path escaped the source dir
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    return _read


def _list_yaml_files_filesystem(source_dir: Path, rel_dir: str) -> list[str]:
    """Discover .yaml/.yml files in `source_dir / rel_dir`."""
    target = source_dir / rel_dir
    if not target.is_dir():
        return []
    files = sorted(target.glob("*.yaml")) + sorted(target.glob("*.yml"))
    out: list[str] = []
    for p in files:
        if not p.is_file():
            continue
        try:
            out.append(p.resolve().relative_to(source_dir.resolve()).as_posix())
        except ValueError:
            continue
    return out


def _parse(content: str, label: str) -> Any:
    try:
        return safe_load_strict(content)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse error in {label}: {exc}") from exc


def _coerce_to_list(parsed: Any, label: str) -> list[Any]:
    """A fragment file may hold either a single mapping (one agent) or"""
    if parsed is None:
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    raise ValueError(
        f"{label}: expected a mapping or list of mappings, "
        f"got {type(parsed).__name__}"
    )


def _merge_list_section(
    inline: list[Any],
    rel_paths: list[str],
    read: ReadFn,
    errors: list[str],
    *,
    source_label: str,
) -> list[Any]:
    out = list(inline or [])
    for rel in rel_paths:
        content = read(rel)
        if content is None:
            errors.append(f"include.{source_label}: cannot read '{rel}'")
            continue
        try:
            parsed = _parse(content, rel)
            out.extend(_coerce_to_list(parsed, rel))
        except ValueError as exc:
            errors.append(f"include.{source_label}: {exc}")
    return out


def _resolve_include_paths(
    spec: Any,
    source_dir: Path | None,
    label: str,
    errors: list[str],
    list_dir: Callable[[str], list[str]],
) -> list[str]:
    """Turn an `include[section]` spec into a list of relative YAML paths."""
    if spec is None:
        return []
    if isinstance(spec, str):
        spec = [spec]
    if not isinstance(spec, list):
        errors.append(
            f"include.{label}: expected a string or list of paths, "
            f"got {type(spec).__name__}."
        )
        return []
    out: list[str] = []
    for entry in spec:
        if not isinstance(entry, str):
            errors.append(f"include.{label}: list entries must be strings.")
            continue
        rel = entry.replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        # Heuristic: ends with .yaml/.yml = file, otherwise = directory.
        if rel.endswith(".yaml") or rel.endswith(".yml"):
            out.append(rel)
            continue
        # Strip trailing slashes for directory enumeration.
        rel_dir = rel.rstrip("/")
        children = list_dir(rel_dir)
        if not children:
            errors.append(
                f"include.{label}: '{entry}' did not resolve to a file or "
                f"directory containing YAML."
            )
            continue
        out.extend(children)
    return out


def apply_includes(
    raw: dict[str, Any],
    source_dir: Path | None,
    *,
    asset_loader: ReadFn | None = None,
    list_dir: Callable[[str], list[str]] | None = None,
    collected_assets: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Compose the raw YAML dict with auto-loaded conventions and any"""
    if not isinstance(raw, dict):
        return raw, []

    # Build the reader + lister callables based on which mode we're in.
    read: ReadFn | None = None
    enumerate_dir: Callable[[str], list[str]] | None = None
    if source_dir is not None:
        source_dir = source_dir.resolve()
        read = _make_filesystem_reader(source_dir)
        enumerate_dir = lambda rel: _list_yaml_files_filesystem(source_dir, rel)
    elif asset_loader is not None and list_dir is not None:
        read = asset_loader
        enumerate_dir = list_dir

    if read is None or enumerate_dir is None:
        # No reader available - just strip the include block (it's no-op
        # data at this layer) and return the raw dict unchanged.
        merged = dict(raw)
        merged.pop("include", None)
        # The canonical home for include is `dev.include` since v2.
        dev = merged.get("dev")
        if isinstance(dev, dict):
            dev = dict(dev)
            dev.pop("include", None)
            merged["dev"] = dev
        return merged, []

    errors: list[str] = []
    merged = dict(raw)
    # Read include spec from either the canonical home (`dev.include`,
    # set by schema_aliases) or the legacy top-level path.
    include_spec = merged.pop("include", None)
    if include_spec is None:
        dev_block = merged.get("dev")
        if isinstance(dev_block, dict) and "include" in dev_block:
            new_dev = dict(dev_block)
            include_spec = new_dev.pop("include")
            merged["dev"] = new_dev

    def _record(rel_paths: list[str]) -> None:
        """Persist the fragment content into the bundle's asset map so"""
        if collected_assets is None or read is None:
            return
        for rel in rel_paths:
            if rel in collected_assets:
                continue
            content = read(rel)
            if content is not None:
                collected_assets[rel] = content

    # 1. Convention auto-load (skipped per-section when overridden by include).
    overridden: set[str] = set()
    if isinstance(include_spec, dict):
        overridden = set(include_spec.keys())

    if "agents" not in overridden:
        rels = enumerate_dir(CONVENTION_DIRS["agents"])
        if rels:
            merged["agents"] = _merge_list_section(
                merged.get("agents"), rels, read, errors, source_label="agents",
            )
            _record(rels)

    if "hooks" not in overridden:
        rels = enumerate_dir(CONVENTION_DIRS["hooks"])
        if rels:
            rt = dict(merged.get("runtime") or {})
            rt["hooks"] = _merge_list_section(
                rt.get("hooks"), rels, read, errors, source_label="hooks",
            )
            merged["runtime"] = rt
            _record(rels)

    for field_name, filename in CONVENTION_FILES.items():
        if field_name in overridden:
            continue
        content = read(filename)
        if content is None:
            continue
        parsed = _parse(content, filename)
        if isinstance(parsed, dict) and field_name in parsed:
            items = parsed[field_name]
        else:
            items = parsed
        if items is None:
            items = []
        if not isinstance(items, list):
            errors.append(
                f"convention '{filename}': expected a list (or a "
                f"mapping with '{field_name}:' key), got "
                f"{type(items).__name__}."
            )
            continue
        inline = merged.get(field_name) or []
        if not isinstance(inline, list):
            inline = []
        merged[field_name] = list(inline) + list(items)
        _record([filename])

    # 2. Explicit include block - overrides convention for listed sections.
    if include_spec is None:
        return merged, errors
    if not isinstance(include_spec, dict):
        errors.append(
            f"include: expected a mapping section -> path(s), "
            f"got {type(include_spec).__name__}."
        )
        return merged, errors

    for section, spec in include_spec.items():
        rels = _resolve_include_paths(
            spec, source_dir, section, errors, enumerate_dir,
        )
        if section == "agents":
            merged["agents"] = _merge_list_section(
                raw.get("agents"), rels, read, errors, source_label="agents",
            )
            _record(rels)
        elif section == "hooks":
            # Same v2 contract as the convention auto-load above:
            # hooks live under `runtime.hooks` post-aliases.
            rt = dict(merged.get("runtime") or {})
            rt["hooks"] = _merge_list_section(
                rt.get("hooks"), rels, read, errors, source_label="hooks",
            )
            merged["runtime"] = rt
            _record(rels)
        else:
            errors.append(
                f"include.{section}: unknown include section. "
                f"Supported: agents, hooks."
            )

    return merged, errors

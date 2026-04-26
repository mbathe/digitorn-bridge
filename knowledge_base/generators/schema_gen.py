"""SchemaGenerator — one doc per Pydantic model in ``AppDefinition``'s tree.

Walks ``AppDefinition`` and every BaseModel it reaches transitively. For
each model we emit a reference card listing its fields (name, type,
required, default, description). The top-level blocks (``app``,
``modules``, ``agents``, …) get listed in ``_index.md`` so readers can
navigate them as the root of the YAML schema.
"""

from __future__ import annotations

import logging
import sys
import types
import typing
from pathlib import Path
from typing import Any

from .base import DocGenerator

logging.getLogger("digitorn").setLevel(logging.ERROR)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PACKAGES_DIR = REPO_ROOT / "packages"


def _ensure_packages_on_path() -> None:
    p = str(PACKAGES_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)


# ── Type rendering ──────────────────────────────────────────────────


_PRIMITIVE_MAP = {
    int: "int",
    float: "float",
    str: "str",
    bool: "bool",
    dict: "dict",
    list: "list",
    type(None): "null",
    Any: "any",
}


def _render_type(tp: Any, model_index: dict[type, str]) -> str:
    """Render a Python typing annotation to a human-friendly string.

    Known Pydantic models render as ``[ModelName](ModelName.md)`` so
    the emitted Markdown cross-links properly. Unknown primitives fall
    back to their ``str(tp)``.
    """
    # Direct primitive
    if tp in _PRIMITIVE_MAP:
        return _PRIMITIVE_MAP[tp]

    # A Pydantic model we know about
    if isinstance(tp, type) and tp in model_index:
        name = model_index[tp]
        return f"[{name}]({name}.md)"

    # typing constructs
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)

    # Literal[...] → '"a" | "b"'
    if origin is typing.Literal:
        return " | ".join(repr(a) for a in args)

    # Handle both typing.Union AND PEP-604 ``X | Y`` (types.UnionType)
    if origin is typing.Union or origin is types.UnionType:
        rendered = [_render_type(a, model_index) for a in args if a is not type(None)]
        nullable = type(None) in args
        text = " | ".join(rendered)
        if nullable:
            text = f"{text} | null"
        return text

    if origin is list:
        inner = _render_type(args[0], model_index) if args else "any"
        return f"list[{inner}]"

    if origin is dict:
        k = _render_type(args[0], model_index) if args else "str"
        v = _render_type(args[1], model_index) if len(args) > 1 else "any"
        return f"dict[{k}, {v}]"

    if origin is tuple:
        inner = ", ".join(_render_type(a, model_index) for a in args) if args else "..."
        return f"tuple[{inner}]"

    # Fallback — keep the Pydantic-serialized form
    if isinstance(tp, type):
        return tp.__name__
    return str(tp)


# ── Model walker ────────────────────────────────────────────────────


def _collect_referenced_models(root: type) -> list[type]:
    """BFS over every Pydantic model reachable from ``root`` (inclusive)."""
    try:
        from pydantic import BaseModel
    except ImportError as exc:
        raise SystemExit(f"[schema_gen] pydantic import failed → {exc}")

    seen: list[type] = []
    queue: list[type] = [root]
    seen_set: set[type] = set()

    while queue:
        current = queue.pop(0)
        if current in seen_set:
            continue
        seen_set.add(current)
        seen.append(current)

        fields = getattr(current, "model_fields", None) or {}
        for f in fields.values():
            for ref in _iter_contained_models(f.annotation):
                if ref not in seen_set and isinstance(ref, type) and issubclass(ref, BaseModel):
                    queue.append(ref)
    return seen


def _iter_contained_models(annotation: Any):
    """Yield every class inside an annotation (unwrapping Optional/Union/list/dict)."""
    if isinstance(annotation, type):
        yield annotation
        return
    for arg in typing.get_args(annotation) or ():
        yield from _iter_contained_models(arg)


# ── Doc rendering ───────────────────────────────────────────────────


_TOP_LEVEL_ORDER = [
    "app",
    "variables",
    "modules",
    "agents",
    "execution",
    "capabilities",
    "behavior",
    "channels",
    "workspace",
    "preview",
    "widgets",
    "middleware",
    "skills",
    "pipeline",
    "features",
    "theme",
    "slash_commands",
]


def _escape_pipes(text: str) -> str:
    """Escape pipe characters so they don't break Markdown tables."""
    return text.replace("|", "\\|")


def _render_field_row(name: str, field: Any, model_index: dict[type, str]) -> str:
    tp = _escape_pipes(_render_type(field.annotation, model_index))
    required = "✓" if field.is_required() else ""

    # Pydantic uses a PydanticUndefined sentinel when no literal default
    # is set (i.e. when a default_factory is used). Detect it and prefer
    # the factory's actual product.
    try:
        from pydantic_core import PydanticUndefined  # type: ignore
    except ImportError:
        PydanticUndefined = None  # type: ignore[assignment]

    if field.is_required():
        default_str = "—"
    else:
        raw_default = field.default
        factory = getattr(field, "default_factory", None)
        has_default = PydanticUndefined is None or raw_default is not PydanticUndefined

        if factory is not None:
            try:
                sample = factory()
                default_str = f"`{sample!r}`"
            except Exception:
                fname = getattr(factory, "__name__", str(factory))
                default_str = f"factory: `{fname}`"
        elif not has_default or raw_default is None:
            default_str = "`None`"
        else:
            default_str = f"`{raw_default!r}`"

    desc = (field.description or "").replace("|", "\\|").replace("\n", " ")
    if len(desc) > 400:
        desc = desc[:397] + "..."
    return f"| `{name}` | {tp} | {required} | {default_str} | {desc} |"


def render_model_card(
    model: type,
    model_index: dict[type, str],
    is_root: bool = False,
) -> str:
    name = model_index[model]
    docstring = (model.__doc__ or "").strip()
    # Drop indentation from triple-quoted docstrings
    if docstring:
        lines = [line.lstrip() for line in docstring.splitlines()]
        # Keep blank lines to preserve paragraph breaks
        docstring = "\n".join(lines)

    fields = getattr(model, "model_fields", None) or {}

    keywords = [name.lower()]
    if is_root:
        keywords.append("app-definition")
        keywords.append("yaml-root")
    # Include ordered field names as additional keywords for retrieval
    keywords.extend(sorted(fields.keys())[:10])
    # Dedupe keeping order
    seen_kw: set[str] = set()
    keywords = [k for k in keywords if not (k in seen_kw or seen_kw.add(k))]

    fm = [
        "---",
        f"id: yaml-schema-{name.lower()}",
        f'title: "{name} — YAML schema reference"',
        "type: schema-reference",
        f"model: {name}",
        f"is_root: {str(is_root).lower()}",
        f"keywords: [{', '.join(keywords)}]",
        "---",
        "",
    ]

    body: list[str] = [f"# {name}", ""]

    if is_root:
        body.append("**This is the root block** — top-level in `app.yaml`.")
        body.append("")
    if docstring:
        body.append("## Description")
        body.append(docstring)
        body.append("")

    body.append("## Fields")
    body.append("")
    if fields:
        body.append("| Name | Type | Required | Default | Description |")
        body.append("|------|------|:--------:|---------|-------------|")
        order = _TOP_LEVEL_ORDER if is_root and model.__name__ == "AppDefinition" else None
        if order:
            keys = [k for k in order if k in fields] + [k for k in fields if k not in order]
        else:
            keys = list(fields.keys())
        for fname in keys:
            body.append(_render_field_row(fname, fields[fname], model_index))
    else:
        body.append("_(no declared fields)_")
    body.append("")

    # Referenced sub-models
    referenced: set[type] = set()
    for f in fields.values():
        for ref in _iter_contained_models(f.annotation):
            if isinstance(ref, type) and ref in model_index and ref is not model:
                referenced.add(ref)
    if referenced:
        body.append("## Linked models")
        for ref in sorted(referenced, key=lambda m: model_index[m]):
            body.append(f"- [{model_index[ref]}]({model_index[ref]}.md)")
        body.append("")

    # Config: extra = forbid / allow
    cfg = getattr(model, "model_config", None)
    if cfg and isinstance(cfg, dict) and "extra" in cfg:
        body.append("## Strictness")
        body.append(f"- `extra: {cfg['extra']}` — unknown keys {'cause a validation error' if cfg['extra'] == 'forbid' else 'are tolerated'}")
        body.append("")

    return "\n".join(fm + body).rstrip() + "\n"


def render_index(root_model: type, models: list[type], model_index: dict[type, str]) -> str:
    root_name = model_index[root_model]
    fields = getattr(root_model, "model_fields", None) or {}

    body = [
        "---",
        "id: yaml-schema-index",
        'title: "YAML Schema Reference — Index"',
        "type: schema-index",
        "keywords: [schema, yaml, reference, index, app-definition]",
        "---",
        "",
        f"# YAML Schema Reference",
        "",
        f"Every block in a Digitorn `app.yaml` is derived from the `{root_name}` Pydantic model. "
        f"This index lists every model in that tree with a one-line summary. "
        f"Click through for the full field list of each block.",
        "",
        "## Top-level blocks",
        "",
        "| Block | Required | Type | Summary |",
        "|-------|:--------:|------|---------|",
    ]

    order = _TOP_LEVEL_ORDER
    top_keys = [k for k in order if k in fields] + [k for k in fields if k not in order]

    for fname in top_keys:
        f = fields[fname]
        tp = _escape_pipes(_render_type(f.annotation, model_index))
        required = "✓" if f.is_required() else ""
        summary = (f.description or "").split("\n")[0].split(".")[0][:120]
        summary = summary.replace("|", "\\|")
        body.append(f"| `{fname}` | {required} | {tp} | {summary} |")
    body.append("")

    body.append("## All models in the schema tree")
    body.append("")
    body.append("| Model | Summary |")
    body.append("|-------|---------|")
    for m in sorted(models, key=lambda x: model_index[x]):
        name = model_index[m]
        doc = (m.__doc__ or "").strip().split("\n")[0][:120].replace("|", "\\|")
        body.append(f"| [{name}]({name}.md) | {doc} |")
    body.append("")

    return "\n".join(body).rstrip() + "\n"


# ── Generator ───────────────────────────────────────────────────────


class SchemaGenerator(DocGenerator):
    name = "schema"

    @property
    def output_dir(self) -> Path:
        return REPO_ROOT / "knowledge_base" / "reference" / "yaml-schema"

    def generate(self) -> dict[Path, str]:
        _ensure_packages_on_path()
        try:
            from digitorn.core.app.schema import AppDefinition
        except ImportError as exc:
            raise SystemExit(f"[schema_gen] cannot import AppDefinition → {exc}")

        models = _collect_referenced_models(AppDefinition)
        model_index: dict[type, str] = {m: m.__name__ for m in models}

        out: dict[Path, str] = {}
        out[self.output_dir / "_index.md"] = render_index(AppDefinition, models, model_index)
        for m in models:
            is_root = (m is AppDefinition)
            out[self.output_dir / f"{model_index[m]}.md"] = render_model_card(m, model_index, is_root=is_root)
        return out

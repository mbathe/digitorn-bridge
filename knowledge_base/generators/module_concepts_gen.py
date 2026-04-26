"""ModuleConceptsGenerator — one concept doc per registered module.

Unlike ``modules_gen.py`` which emits one card PER ACTION, this generator
emits ONE doc PER MODULE: a high-level orientation page that lists
config shape, action catalogue, isolation mode, and the module's own
class docstring. Dedicated to helping the LLM architect decide
*which module* to use, before drilling into individual action cards.

Everything here is derived from code — class docstring, ConfigModel,
manifest, isolation mode. No hand-written facts.
"""

from __future__ import annotations

import inspect
import logging
import sys
import textwrap
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


def _short_name_for(module_id: str, action_name: str) -> str:
    try:
        from digitorn.core.runtime.tool_names import to_short
        fqn = f"{module_id}.{action_name}"
        short = to_short(fqn)
        if short and short != fqn:
            return short
    except Exception:
        pass
    return ""


def _clean_docstring(doc: str | None) -> str:
    if not doc:
        return ""
    return textwrap.dedent(doc).strip()


def _collect_module_documentation(module: Any) -> str:
    """Combine the module FILE docstring + the module CLASS docstring.

    Modules tend to put the rich orientation doc at file level (the
    one with sections like Design / Actions / Removed), and a
    one-line summary on the class itself. Merge both for maximum
    signal, preferring the file-level text when both exist because
    the class docstring is often redundant.
    """
    mod = inspect.getmodule(module.__class__)
    file_doc = _clean_docstring(getattr(mod, "__doc__", None)) if mod else ""
    class_doc = _clean_docstring(module.__class__.__doc__)

    if file_doc and class_doc:
        # Dedupe when the class doc is just the first line of the file doc.
        file_first_line = file_doc.split("\n", 1)[0].strip()
        if class_doc.strip() in (file_first_line, file_doc.strip()):
            return file_doc
        return f"{file_doc}\n\n> Class-level summary: {class_doc}"
    return file_doc or class_doc


def _render_config_fields(config_model: type | None) -> tuple[str, list[str]]:
    """Return (markdown_table, field_names). Empty if no config model."""
    if config_model is None:
        return "", []

    try:
        from pydantic_core import PydanticUndefined  # type: ignore
    except ImportError:
        PydanticUndefined = None  # type: ignore[assignment]

    fields = getattr(config_model, "model_fields", None) or {}
    if not fields:
        return "", []

    rows = [
        "| Field | Type | Required | Default | Description |",
        "|-------|------|:--------:|---------|-------------|",
    ]
    field_names: list[str] = []
    for fname, f in fields.items():
        field_names.append(fname)
        # Type: prefer a compact string rep
        ann = getattr(f, "annotation", None)
        tp = getattr(ann, "__name__", None) or str(ann)
        tp = tp.replace("typing.", "").replace("|", "\\|")

        required = "✓" if f.is_required() else ""
        if f.is_required():
            default_str = "—"
        else:
            try:
                from pydantic import BaseModel as _PydBaseModel  # type: ignore
            except ImportError:
                _PydBaseModel = None  # type: ignore[assignment]

            factory = getattr(f, "default_factory", None)
            raw = f.default
            has_default = PydanticUndefined is None or raw is not PydanticUndefined

            def _compact(val: Any) -> str:
                if _PydBaseModel is not None and isinstance(val, _PydBaseModel):
                    # Don't dump a multi-field Pydantic repr — name the model only.
                    # We don't emit a link because the linked doc isn't
                    # guaranteed to exist (schema-gen only walks AppDefinition).
                    return f"`{type(val).__name__}` (nested — see module code)"
                r = repr(val)
                if len(r) > 80:
                    r = r[:77] + "..."
                return f"`{r}`"

            if factory is not None:
                try:
                    sample = factory()
                    default_str = _compact(sample)
                except Exception:
                    fname2 = getattr(factory, "__name__", "factory")
                    default_str = f"factory: `{fname2}`"
            elif not has_default or raw is None:
                default_str = "`None`"
            else:
                default_str = _compact(raw)

        desc = (f.description or "").replace("|", "\\|").replace("\n", " ")
        if len(desc) > 200:
            desc = desc[:197] + "..."
        rows.append(f"| `{fname}` | {tp} | {required} | {default_str} | {desc} |")

    return "\n".join(rows), field_names


def _find_config_model(module: Any) -> type | None:
    """Discover the Pydantic config model used by a module instance, if any.

    Heuristic: look at the module class's module-level classes whose name
    ends in ``Config`` and is a Pydantic BaseModel. Falls back to None
    if nothing matches (e.g. modules without explicit config).
    """
    try:
        from pydantic import BaseModel
    except ImportError:
        return None

    # The module instance's class defines CONFIG_MODEL sometimes
    cfg = getattr(module.__class__, "CONFIG_MODEL", None)
    if cfg is not None and inspect.isclass(cfg) and issubclass(cfg, BaseModel):
        return cfg

    # Otherwise walk the module's Python file for a *Config class.
    mod = inspect.getmodule(module.__class__)
    if mod is None:
        return None
    for name, obj in inspect.getmembers(mod, inspect.isclass):
        if (
            issubclass(obj, BaseModel)
            and obj is not BaseModel
            and name.lower().endswith("config")
            and "config" in name.lower()
            # Skip obvious action param classes
            and not name.endswith("Params")
        ):
            # Prefer one whose name starts with the module id
            mid = getattr(module.__class__, "MODULE_ID", "").replace("_", "")
            if mid and mid.lower() in name.lower():
                return obj

    return None


def _render_actions_table(manifest: Any, module_id: str) -> tuple[str, int, int]:
    """Return (markdown_table, visible_count, internal_count)."""
    actions = getattr(manifest, "actions", None) or []
    if isinstance(actions, dict):
        items = list(actions.items())
    else:
        items = [(getattr(a, "name", "?"), a) for a in actions]

    if not items:
        return "_(no actions registered)_", 0, 0

    rows = [
        "| Action | Short name | Internal | Risk | One-liner |",
        "|--------|-----------|:--------:|------|-----------|",
    ]
    visible = 0
    internal = 0
    for name, spec in items:
        is_internal = bool(getattr(spec, "internal", False))
        if is_internal:
            internal += 1
        else:
            visible += 1
        short = _short_name_for(module_id, name) or "—"
        risk = getattr(spec, "risk_level", "") or ""
        desc = (getattr(spec, "description", "") or "").replace("|", "\\|").split("\n")[0]
        if len(desc) > 120:
            desc = desc[:117] + "..."
        internal_marker = "✓" if is_internal else ""
        rows.append(f"| `{name}` | `{short}` | {internal_marker} | {risk} | {desc} |")

    return "\n".join(rows), visible, internal


def _render_grant_snippet(module_id: str, visible_action_names: list[str]) -> str:
    """Render a minimal ``capabilities.grant`` block showing ALL visible actions."""
    if not visible_action_names:
        return ""
    actions_str = ", ".join(visible_action_names)
    return (
        "```yaml\n"
        "capabilities:\n"
        "  grant:\n"
        f"    - module: {module_id}\n"
        f"      actions: [{actions_str}]\n"
        "```"
    )


def _render_per_agent_grant(module_id: str, visible_action_names: list[str]) -> str:
    if not visible_action_names:
        return ""
    # Keep to at most 5 actions in the example to stay scannable
    sample = visible_action_names[:5]
    actions_str = ", ".join(sample)
    return (
        "```yaml\n"
        "agents:\n"
        "  - id: my-agent\n"
        "    modules:\n"
        f"      - {{{module_id}: [{actions_str}]}}\n"
        "```"
    )


def _read_isolation_from_toml(module_id: str) -> str:
    """Read the declared isolation from the module's digitorn-module.toml.

    Returns the raw value from the TOML (``shared`` | ``session`` |
    ``isolated``) or an empty string when the file is unreadable.
    This is the authoritative source — the registry wraps ``shared``
    modules in per-session proxies at runtime.
    """
    toml_path = PACKAGES_DIR / "digitorn" / "modules" / module_id / "digitorn-module.toml"
    if not toml_path.exists():
        return ""
    try:
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
        # digitorn-module.toml nests the fields under a ``[module]`` section.
        module_section = data.get("module", {}) if isinstance(data.get("module"), dict) else {}
        return str(module_section.get("isolation", data.get("isolation", "")) or "")
    except Exception:
        return ""


def render_module_concept(module_id: str, module: Any, manifest: Any) -> str:
    cls = module.__class__
    version = getattr(cls, "VERSION", "") or getattr(manifest, "version", "") or ""
    isolation = _read_isolation_from_toml(module_id) or "session"

    docstring = _collect_module_documentation(module)

    config_model = _find_config_model(module)
    config_table, config_fields = _render_config_fields(config_model)

    actions_table, visible_count, internal_count = _render_actions_table(manifest, module_id)

    # Collect visible action names for grant snippets
    actions = getattr(manifest, "actions", None) or []
    if isinstance(actions, dict):
        pairs = actions.items()
    else:
        pairs = ((getattr(a, "name", "?"), a) for a in actions)
    visible_names = [n for n, s in pairs if not bool(getattr(s, "internal", False))]

    grant_snippet = _render_grant_snippet(module_id, visible_names)
    per_agent_snippet = _render_per_agent_grant(module_id, visible_names)

    # Frontmatter keywords: module + action names
    keywords = [module_id, f"{module_id}-module"] + visible_names[:15]
    seen: set[str] = set()
    keywords = [k for k in keywords if not (k in seen or seen.add(k))]

    fm = [
        "---",
        f"id: module-concept-{module_id}",
        f'title: "{module_id} module — overview"',
        "type: module-concept",
        f"module: {module_id}",
        f"isolation: {isolation}",
        f"keywords: [{', '.join(keywords)}]",
        f"version: {version}",
        "---",
        "",
    ]

    body: list[str] = [f"# `{module_id}` module", ""]

    body.append(
        f"- **Isolation**: `{isolation}`"
        + (" (one instance shared across apps)" if isolation == "shared" else " (per-session state)")
    )
    if version:
        body.append(f"- **Version**: `{version}`")
    body.append(f"- **Actions**: {visible_count} visible, {internal_count} internal")
    body.append("")

    if docstring:
        body.append("## Description (from class docstring)")
        body.append("")
        body.append(docstring)
        body.append("")

    if config_table:
        body.append("## Configuration")
        body.append("")
        body.append(f"Set under `modules.{module_id}.config` in `app.yaml`. All fields derive from the module's Pydantic config model.")
        body.append("")
        body.append(config_table)
        body.append("")

    body.append("## Actions")
    body.append("")
    body.append(actions_table)
    body.append("")

    if grant_snippet:
        body.append("## Grant (in `capabilities.grant`)")
        body.append("")
        body.append("Full-app grant (every visible action):")
        body.append("")
        body.append(grant_snippet)
        body.append("")

        body.append("Per-specialist grant (under `agents[].modules`):")
        body.append("")
        body.append(per_agent_snippet)
        body.append("")

    # Cross-links
    body.append("## Per-action cards")
    body.append("")
    body.append(
        f"For the full parameter spec of each action, see the auto-generated "
        f"cards in `knowledge_base/modules/{module_id}-*.md`."
    )
    body.append("")

    return "\n".join(fm + body).rstrip() + "\n"


def _build_registry() -> Any:
    _ensure_packages_on_path()
    try:
        from digitorn.modules.registry import ModuleRegistry
        from digitorn.core.loader import load_modules
    except ImportError as exc:
        raise SystemExit(f"[module_concepts_gen] cannot import digitorn → {exc}")
    registry = ModuleRegistry()
    load_modules(registry, load_all=True)
    return registry


class ModuleConceptsGenerator(DocGenerator):
    name = "module_concepts"

    @property
    def output_dir(self) -> Path:
        return REPO_ROOT / "knowledge_base" / "concepts" / "modules"

    def generate(self) -> dict[Path, str]:
        registry = _build_registry()
        list_available = getattr(registry, "list_available", None) or registry.list_modules

        out: dict[Path, str] = {}
        for module_id in sorted(list_available()):
            try:
                # The registry exposes a factory; we need an instance for
                # introspection (and to locate the ConfigModel via its class).
                instance = registry.create(module_id)
            except Exception as exc:
                print(f"[module_concepts_gen] skip {module_id}: create failed → {exc}", file=sys.stderr)
                continue

            try:
                manifest = registry.get_manifest(module_id)
            except Exception as exc:
                print(f"[module_concepts_gen] skip {module_id}: get_manifest failed → {exc}", file=sys.stderr)
                continue

            if manifest is None:
                continue

            try:
                card = render_module_concept(module_id, instance, manifest)
            except Exception as exc:
                print(f"[module_concepts_gen] FAILED on {module_id}: {exc}", file=sys.stderr)
                continue

            out[self.output_dir / f"{module_id}.md"] = card

        return out

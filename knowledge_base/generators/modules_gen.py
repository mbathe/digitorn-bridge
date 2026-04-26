"""ModulesGenerator — one card per @action in the live module registry.

Contract is defined in ``generators.base.DocGenerator``. Output directory
is ``knowledge_base/modules/``. Internal actions are skipped (they are
hidden from LLM tool discovery; documenting them would bloat the KB
with entries the architect cannot grant anyway).
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

from .base import DocGenerator

# Quiet the daemon logger so generator output stays readable.
logging.getLogger("digitorn").setLevel(logging.ERROR)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PACKAGES_DIR = REPO_ROOT / "packages"


def _ensure_packages_on_path() -> None:
    p = str(PACKAGES_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)


def _slugify(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "untitled"


def _short_name_for(module_id: str, action_name: str) -> str:
    try:
        from digitorn.core.runtime.tool_names import to_short
        fqn = f"{module_id}.{action_name}"
        short = to_short(fqn)
        if short and short != fqn:
            return short
    except Exception:
        pass
    return "".join(p.capitalize() for p in re.split(r"[._]+", action_name))


def _render_param_table(params: list[Any]) -> str:
    if not params:
        return "_(no parameters)_"
    rows = [
        "| Name | Type | Required | Default | Description |",
        "|------|------|:--------:|---------|-------------|",
    ]
    for p in params:
        name = getattr(p, "name", "?")
        ptype = getattr(p, "type", "?") or "?"
        required = "✓" if getattr(p, "required", False) else ""
        default = getattr(p, "default", None)
        default_str = "—" if default is None else f"`{default}`"
        desc = (getattr(p, "description", "") or "").replace("|", "\\|").replace("\n", " ")
        if len(desc) > 200:
            desc = desc[:197] + "..."
        rows.append(f"| `{name}` | {ptype} | {required} | {default_str} | {desc} |")
    return "\n".join(rows)


def _render_grant_snippet(module_id: str, action_name: str) -> str:
    return (
        "```yaml\n"
        "capabilities:\n"
        "  grant:\n"
        f"    - module: {module_id}\n"
        f"      actions: [{action_name}]\n"
        "```"
    )


def render_action_card(module_id: str, action_name: str, spec: Any) -> str:
    short = _short_name_for(module_id, action_name)
    fqn = f"{module_id}.{action_name}"
    description = (getattr(spec, "description", "") or "").strip()
    tool_prompt = (getattr(spec, "tool_prompt", "") or "").strip()
    tags = list(getattr(spec, "tags", []) or [])
    aliases = list(getattr(spec, "aliases", []) or [])
    permissions = list(getattr(spec, "permissions", []) or [])
    risk_level = getattr(spec, "risk_level", "") or ""
    irreversible = getattr(spec, "irreversible", False)
    require_approval = getattr(spec, "require_approval", False)
    params = list(getattr(spec, "params", []) or [])

    keywords: list[str] = [module_id, action_name]
    if short and short.lower() not in keywords:
        keywords.append(short.lower())
    keywords += [t.lower() for t in tags if t]
    keywords += [a.lower() for a in aliases if a]
    seen: set[str] = set()
    keywords = [k for k in keywords if not (k in seen or seen.add(k))]

    fm_lines = [
        "---",
        f"id: {module_id}-{_slugify(action_name)}",
        f'title: "{fqn} ({short})"',
        "type: module-action",
        f"module: {module_id}",
        f"action: {action_name}",
        f"fqn: {fqn}",
        f"short_name: {short}",
        f"keywords: [{', '.join(keywords)}]",
        f"permissions: [{', '.join(permissions)}]",
        f"risk_level: {risk_level or 'unknown'}",
        f"irreversible: {str(irreversible).lower()}",
        f"require_approval: {str(require_approval).lower()}",
        "---",
        "",
    ]

    body: list[str] = [f"# {fqn} ({short})", ""]
    if description:
        body.extend(["## Description", description, ""])

    body.extend(["## Parameters", _render_param_table(params), ""])
    body.extend(["## Capability grant (in app YAML)", _render_grant_snippet(module_id, action_name), ""])

    if tool_prompt:
        body.extend(["## Tool usage instructions", "```", tool_prompt, "```", ""])

    if aliases:
        body.extend(["## Aliases", ", ".join(f"`{a}`" for a in aliases), ""])

    if permissions or risk_level or irreversible or require_approval:
        body.append("## Safety")
        if permissions:
            body.append(f"- Required permissions: {', '.join(f'`{p}`' for p in permissions)}")
        if risk_level:
            body.append(f"- Risk level: **{risk_level}**")
        if irreversible:
            body.append("- ⚠️ **Irreversible** — cannot be undone once executed")
        if require_approval:
            body.append("- ⛔ **Requires user approval** before execution")
        body.append("")

    return "\n".join(fm_lines + body).rstrip() + "\n"


def _build_registry() -> Any:
    _ensure_packages_on_path()
    try:
        from digitorn.modules.registry import ModuleRegistry
        from digitorn.core.loader import load_modules
    except ImportError as exc:
        raise SystemExit(
            f"[modules_gen] cannot import digitorn from {PACKAGES_DIR}\n  → {exc}"
        )
    registry = ModuleRegistry()
    load_modules(registry, load_all=True)
    return registry


def _iter_actions(registry: Any, include_internal: bool):
    list_available = getattr(registry, "list_available", None) or registry.list_modules
    for module_id in sorted(list_available()):
        try:
            manifest = registry.get_manifest(module_id)
        except Exception as exc:
            print(
                f"[modules_gen] skip {module_id}: get_manifest failed → {exc}",
                file=sys.stderr,
            )
            continue
        if manifest is None:
            continue

        actions = getattr(manifest, "actions", None) or []
        if isinstance(actions, dict):
            iterable = actions.items()
        else:
            iterable = ((getattr(a, "name", "?"), a) for a in actions)

        for action_name, spec in iterable:
            if bool(getattr(spec, "internal", False)) and not include_internal:
                continue
            yield module_id, action_name, spec


class ModulesGenerator(DocGenerator):
    name = "modules"

    def __init__(self, include_internal: bool = False, only_module: str | None = None) -> None:
        self.include_internal = include_internal
        self.only_module = only_module

    @property
    def output_dir(self) -> Path:
        return REPO_ROOT / "knowledge_base" / "modules"

    def generate(self) -> dict[Path, str]:
        registry = _build_registry()
        out: dict[Path, str] = {}
        for module_id, action_name, spec in _iter_actions(registry, self.include_internal):
            if self.only_module and module_id != self.only_module:
                continue
            try:
                card = render_action_card(module_id, action_name, spec)
            except Exception as exc:
                print(
                    f"[modules_gen] FAILED on {module_id}.{action_name}: {exc}",
                    file=sys.stderr,
                )
                continue
            out_path = self.output_dir / f"{module_id}-{_slugify(action_name)}.md"
            out[out_path] = card
        return out

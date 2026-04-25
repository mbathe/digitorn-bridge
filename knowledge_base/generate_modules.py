"""generate_modules.py — auto-generate one card per Digitorn @action.

Walks the live ModuleRegistry, iterates every non-internal ``@action``
exposed by every module, and writes a structured markdown card to
``knowledge_base/modules/<module_id>-<action_name>.md``.

These cards are the second of the three RAG collections used by the
App Builder agent (the others being ``concepts/`` distilled by hand
and ``examples/`` curated templates). Unlike the concept cards, these
are **fully auto-generated** and should be regenerated every time the
module surface changes — they are *never* edited by hand.

Usage::

    # Regenerate every action card
    python knowledge_base/generate_modules.py

    # Dry-run (print, don't write)
    python knowledge_base/generate_modules.py --dry-run

    # Limit to a single module (useful while iterating)
    python knowledge_base/generate_modules.py --module filesystem

    # Include actions marked ``internal=True`` (debug only — these are
    # normally hidden from the builder because they are also hidden
    # from the LLM in production)
    python knowledge_base/generate_modules.py --include-internal
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import Any

# Force UTF-8 on stdout/stderr — the Windows default cp1252 codepage
# trips over the unicode glyphs we put in the rendered cards
# (✓, ⚠️, ⛔, etc.) which would otherwise crash a --dry-run print.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

# Quiet the daemon's structured logger — we don't want module_loaded
# noise drowning out the actual generation output.
logging.basicConfig(level=logging.ERROR)
logging.getLogger("digitorn").setLevel(logging.ERROR)

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULES_OUT_DIR = REPO_ROOT / "knowledge_base" / "modules"
PACKAGES_DIR = REPO_ROOT / "packages"


# ────────────────────────────────────────────────────────────────────
# Bootstrap a populated ModuleRegistry from this standalone script
# ────────────────────────────────────────────────────────────────────


def build_registry() -> Any:
    """Spin up a populated ModuleRegistry without starting the daemon.

    Returns the registry instance. Raises if the import fails — usually
    because the script wasn't run from the repo root.
    """
    sys.path.insert(0, str(PACKAGES_DIR))
    try:
        from digitorn.modules.registry import ModuleRegistry
        from digitorn.core.loader import load_modules
    except ImportError as exc:
        raise SystemExit(
            f"[generate_modules] cannot import digitorn from {PACKAGES_DIR}\n"
            f"  → {exc}\n"
            f"Run this script from the repo root with `py -3.12 knowledge_base/generate_modules.py`"
        )

    registry = ModuleRegistry()
    load_modules(registry, load_all=True)
    return registry


# ────────────────────────────────────────────────────────────────────
# Card rendering
# ────────────────────────────────────────────────────────────────────


def _slugify(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or "untitled"


def _short_name_for(module_id: str, action_name: str) -> str:
    """Return the LLM-facing short name (Read, Write, Edit, ...) for an action.

    Uses the project's central tool_names map when available so we
    stay consistent with what the agent actually sees in its tool
    list. Falls back to a PascalCase derivation when the map doesn't
    cover the action.
    """
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
    """Render the action's ParamSpec list as a Markdown table.

    Falls back to "(no parameters)" when the action takes nothing.
    """
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
    """Build the full markdown card for one action."""
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

    keywords: list[str] = []
    keywords.append(module_id)
    keywords.append(action_name)
    if short and short.lower() not in keywords:
        keywords.append(short.lower())
    keywords += [t.lower() for t in tags if t]
    keywords += [a.lower() for a in aliases if a]
    # Dedupe while preserving order
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

    body: list[str] = []
    body.append(f"# {fqn} ({short})")
    body.append("")
    if description:
        body.append("## Description")
        body.append(description)
        body.append("")

    body.append("## Parameters")
    body.append(_render_param_table(params))
    body.append("")

    body.append("## Capability grant (in app YAML)")
    body.append(_render_grant_snippet(module_id, action_name))
    body.append("")

    if tool_prompt:
        body.append("## Tool usage instructions")
        body.append("```")
        body.append(tool_prompt)
        body.append("```")
        body.append("")

    if aliases:
        body.append("## Aliases")
        body.append(", ".join(f"`{a}`" for a in aliases))
        body.append("")

    if permissions or risk_level or irreversible or require_approval:
        body.append("## Safety")
        flag_lines = []
        if permissions:
            flag_lines.append(f"- Required permissions: {', '.join(f'`{p}`' for p in permissions)}")
        if risk_level:
            flag_lines.append(f"- Risk level: **{risk_level}**")
        if irreversible:
            flag_lines.append("- ⚠️ **Irreversible** — cannot be undone once executed")
        if require_approval:
            flag_lines.append("- ⛔ **Requires user approval** before execution")
        body.extend(flag_lines)
        body.append("")

    return "\n".join(fm_lines + body).rstrip() + "\n"


# ────────────────────────────────────────────────────────────────────
# Driver
# ────────────────────────────────────────────────────────────────────


def iterate_actions(
    registry: Any,
    only_module: str | None,
    include_internal: bool,
):
    """Yield ``(module_id, action_name, spec)`` triples for every visible action."""
    list_available = getattr(registry, "list_available", None) or registry.list_modules
    for module_id in sorted(list_available()):
        if only_module and module_id != only_module:
            continue
        try:
            manifest = registry.get_manifest(module_id)
        except Exception as exc:
            print(
                f"[generate_modules] skip {module_id}: get_manifest failed → {exc}",
                file=sys.stderr,
            )
            continue
        if manifest is None:
            continue

        actions = getattr(manifest, "actions", None) or []
        # ``actions`` may be a list of ActionSpec OR a dict keyed by name.
        if isinstance(actions, dict):
            iterable = actions.items()
        else:
            iterable = ((getattr(a, "name", "?"), a) for a in actions)

        for action_name, spec in iterable:
            internal = bool(getattr(spec, "internal", False))
            if internal and not include_internal:
                continue
            yield module_id, action_name, spec


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the cards instead of writing them to disk",
    )
    parser.add_argument(
        "--module",
        default=None,
        help="Limit generation to a single module id (e.g. 'filesystem')",
    )
    parser.add_argument(
        "--include-internal",
        action="store_true",
        help="Include actions flagged internal=True (normally hidden from the LLM)",
    )
    args = parser.parse_args()

    print("[generate_modules] bootstrapping ModuleRegistry...", file=sys.stderr)
    registry = build_registry()

    available_count = len(list(getattr(registry, "list_available", lambda: [])())) or len(
        list(registry.list_modules())
    )
    print(
        f"[generate_modules] {available_count} module(s) available",
        file=sys.stderr,
    )

    MODULES_OUT_DIR.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped_no_action = 0

    for module_id, action_name, spec in iterate_actions(
        registry,
        only_module=args.module,
        include_internal=args.include_internal,
    ):
        try:
            card = render_action_card(module_id, action_name, spec)
        except Exception as exc:
            print(
                f"[generate_modules] FAILED on {module_id}.{action_name}: {exc}",
                file=sys.stderr,
            )
            continue

        out_path = MODULES_OUT_DIR / f"{module_id}-{_slugify(action_name)}.md"

        if args.dry_run:
            print(f"\n--- {out_path.relative_to(REPO_ROOT)} ---")
            print(card)
        else:
            out_path.write_text(card, encoding="utf-8")
            written += 1
            print(f"  ✓ {out_path.relative_to(REPO_ROOT)}", file=sys.stderr)

    if not written and not args.dry_run:
        print(
            "[generate_modules] no cards written — no @action method discovered. "
            "Check that load_modules(load_all=True) actually populated the registry.",
            file=sys.stderr,
        )
        sys.exit(1)

    verb = "would write" if args.dry_run else "wrote"
    print(f"\n[generate_modules] {verb} {written or 'multiple'} card(s)", file=sys.stderr)


if __name__ == "__main__":
    main()

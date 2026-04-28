"""HooksGenerator - one doc per hook condition + one per hook action.

The hooks subsystem uses two registries in ``runtime/hooks.py``:
  - ``_CONDITION_REGISTRY``  - name → evaluator fn
  - ``_ACTION_REGISTRY``     - name → executor fn

Both are populated by the ``@register_condition`` / ``@register_action``
decorators, which also store a param schema in ``_CONDITION_PARAMS`` /
``_ACTION_PARAMS``. We walk both registries, extract the fn docstring
and the param schema, and emit one card per entry.

We also emit a single ``events.md`` listing the 15 hook events with
their purpose, because events are semantic (not registered via a
decorator) and worth grouping in one doc.
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


# The 15 events listed in the hooks.py file banner. Each tuple is
# (event_name, aliases, short_description). Aliases are alternate
# YAML names the compiler accepts for the same event - they're
# declared in the hooks.py alias map (_EVENT_ALIASES).
_HOOK_EVENTS: list[tuple[str, list[str], str]] = [
    ("turn_start", ["user_prompt"], "Fires at the beginning of each agent turn. Also triggered by the ``user_prompt`` alias."),
    ("turn_end", [], "Fires at the end of each agent turn (after the final tool or reply for that turn)."),
    ("tool_start", ["pre_tool_use"], "Fires right before a tool executes. Can gate, transform params, or inject messages."),
    ("tool_end", ["post_tool_use"], "Fires right after a tool returns. Can transform the result or inject a follow-up."),
    ("session_start", [], "Fires at session creation (turn == 0)."),
    ("session_end", [], "Fires when ``manager.end_session`` closes the session."),
    ("pre_compact", [], "Fires before the context-compaction step - ideal for custom compaction strategies."),
    ("error", [], "Fires when the agent loop catches an exception (provider error, tool crash, etc.)."),
    ("approval_request", [], "Fires whenever ``ApprovalQueue.enqueue`` adds a new pending approval."),
    ("agent_spawn", [], "Fires from the agent_spawn module when a sub-agent is launched."),
    ("agent_complete", [], "Fires from the agent_spawn module when a sub-agent finishes."),
    ("activation", [], "Declared-only event for background-trigger routing. Not fired by the runtime - the activation router consumes it."),
]


def _clean_doc(obj: Any) -> str:
    doc = inspect.getdoc(obj) or ""
    return textwrap.dedent(doc).strip()


def _render_params_table(params: dict[str, str] | None) -> str:
    if not params:
        return "_(no params)_"
    rows = ["| Param | Requirement |", "|-------|-------------|"]
    for name, req in sorted(params.items()):
        rows.append(f"| `{name}` | {req} |")
    return "\n".join(rows)


def render_condition_card(name: str, fn: Any, params: dict[str, str] | None) -> str:
    doc = _clean_doc(fn)
    keywords = [name, "condition", "hook"]
    # Pull additional keywords from any param names
    if params:
        keywords += list(params.keys())
    seen: set[str] = set()
    keywords = [k for k in keywords if not (k in seen or seen.add(k))]

    fm = [
        "---",
        f"id: hook-condition-{name}",
        f'title: "Hook condition: {name}"',
        "type: hook-condition",
        f"condition: {name}",
        f"keywords: [{', '.join(keywords)}]",
        "---",
        "",
    ]

    body: list[str] = [f"# Hook condition: `{name}`", ""]
    body.append(f"Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_condition(\"{name}\")`.")
    body.append("")

    body.append("## Params")
    body.append(_render_params_table(params))
    body.append("")

    if doc:
        body.append("## Behavior")
        body.append(doc)
        body.append("")

    body.append("## YAML")
    body.append("")
    body.append(
        f"```yaml compile=skip\n"
        f"hooks:\n"
        f"  - id: my-hook\n"
        f"    \"on\": tool_start     # any hook event\n"
        f"    condition:\n"
        f"      type: {name}\n"
        + (f"      # params: {', '.join(params.keys())}\n" if params else "")
        + f"    action:\n"
        f"      type: log\n"
        f"      message: \"{name} fired\"\n"
        f"```"
    )
    body.append("")

    return "\n".join(fm + body).rstrip() + "\n"


def render_action_card(name: str, fn: Any, params: dict[str, str] | None) -> str:
    doc = _clean_doc(fn)
    keywords = [name, "action", "hook"]
    if params:
        keywords += list(params.keys())
    seen: set[str] = set()
    keywords = [k for k in keywords if not (k in seen or seen.add(k))]

    fm = [
        "---",
        f"id: hook-action-{name}",
        f'title: "Hook action: {name}"',
        "type: hook-action",
        f"action: {name}",
        f"keywords: [{', '.join(keywords)}]",
        "---",
        "",
    ]

    body: list[str] = [f"# Hook action: `{name}`", ""]
    body.append(f"Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action(\"{name}\")`.")
    body.append("")

    body.append("## Params")
    body.append(_render_params_table(params))
    body.append("")

    if doc:
        body.append("## Behavior")
        body.append(doc)
        body.append("")

    body.append("## YAML")
    body.append("")
    body.append(
        f"```yaml compile=skip\n"
        f"hooks:\n"
        f"  - id: my-hook\n"
        f"    \"on\": turn_end\n"
        f"    condition:\n"
        f"      type: always\n"
        f"    action:\n"
        f"      type: {name}\n"
        + (f"      # params: {', '.join(params.keys())}\n" if params else "")
        + f"```"
    )
    body.append("")

    return "\n".join(fm + body).rstrip() + "\n"


def render_events_doc() -> str:
    fm = [
        "---",
        "id: hook-events-reference",
        'title: "Hook events reference"',
        "type: hook-events",
        "keywords: [hooks, events, turn_start, turn_end, tool_start, tool_end, session_start, session_end, pre_compact, error, approval_request, agent_spawn, agent_complete, activation]",
        "---",
        "",
    ]
    body: list[str] = ["# Hook events - reference", ""]
    body.append(
        "Every hook declares an `on:` event. When that event fires in the agent "
        "loop, the hook's condition is evaluated and, if it passes, the action "
        "executes. Events are semantic (not registered via a decorator); this "
        "page is the canonical list."
    )
    body.append("")
    body.append("| Event | Aliases | Purpose |")
    body.append("|-------|---------|---------|")
    for name, aliases, desc in _HOOK_EVENTS:
        aliases_str = ", ".join(f"`{a}`" for a in aliases) if aliases else "-"
        body.append(f"| `{name}` | {aliases_str} | {desc} |")
    body.append("")
    body.append("## YAML wiring")
    body.append("")
    body.append("`on:` must be quoted as a string - YAML 1.1 treats `on`/`yes`/`no` as booleans.")
    body.append("")
    body.append(
        "```yaml compile=skip\n"
        "execution:\n"
        "  hooks:\n"
        "    - id: my-hook\n"
        "      \"on\": tool_start    # quote 'on' - YAML 1.1 truthiness\n"
        "      condition:\n"
        "        type: always\n"
        "      action:\n"
        "        type: log\n"
        "        message: \"Starting a tool\"\n"
        "```"
    )
    body.append("")

    return "\n".join(fm + body).rstrip() + "\n"


def render_index(conditions: list[str], actions: list[str]) -> str:
    fm = [
        "---",
        "id: hooks-reference-index",
        'title: "Hooks reference - index"',
        "type: hooks-index",
        "keywords: [hooks, events, conditions, actions, registry, reference, index]",
        "---",
        "",
    ]
    body: list[str] = ["# Hooks reference - index", ""]
    body.append(f"Derived from the hooks registry in `packages/digitorn/core/runtime/hooks.py`. **{len(_HOOK_EVENTS)} events**, **{len(conditions)} conditions**, **{len(actions)} actions**.")
    body.append("")

    body.append("## Events")
    body.append("See [events.md](events.md) for the full catalogue.")
    body.append("")

    body.append("## Conditions (evaluate whether a hook should fire)")
    body.append("")
    for c in conditions:
        body.append(f"- [`{c}`](conditions/{c}.md)")
    body.append("")

    body.append("## Actions (what the hook does when it fires)")
    body.append("")
    for a in actions:
        body.append(f"- [`{a}`](actions/{a}.md)")
    body.append("")

    return "\n".join(fm + body).rstrip() + "\n"


class HooksGenerator(DocGenerator):
    name = "hooks"

    @property
    def output_dir(self) -> Path:
        return REPO_ROOT / "knowledge_base" / "reference" / "hooks"

    #: Recurse into subdirs for phantom-detection
    output_glob = "**/*.md"

    def generate(self) -> dict[Path, str]:
        _ensure_packages_on_path()
        try:
            from digitorn.core.runtime import hooks as hooks_mod
        except ImportError as exc:
            raise SystemExit(f"[hooks_gen] cannot import hooks module → {exc}")

        conditions = hooks_mod._CONDITION_REGISTRY
        cond_params = hooks_mod._CONDITION_PARAMS
        actions = hooks_mod._ACTION_REGISTRY
        act_params = hooks_mod._ACTION_PARAMS

        out: dict[Path, str] = {}

        out[self.output_dir / "events.md"] = render_events_doc()
        for name, fn in sorted(conditions.items()):
            out[self.output_dir / "conditions" / f"{name}.md"] = render_condition_card(
                name, fn, cond_params.get(name)
            )
        for name, fn in sorted(actions.items()):
            out[self.output_dir / "actions" / f"{name}.md"] = render_action_card(
                name, fn, act_params.get(name)
            )

        out[self.output_dir / "_index.md"] = render_index(
            sorted(conditions.keys()),
            sorted(actions.keys()),
        )
        return out

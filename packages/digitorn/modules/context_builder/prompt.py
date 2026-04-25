"""System prompt assembly for agents.

Enriches the user-defined system prompt with tool discovery
instructions and context metadata.

The context builder is the SINGLE SOURCE OF TRUTH for tool awareness.
The user's YAML system_prompt should focus on personality and behavior —
never on listing tools or explaining how to call them.
"""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from digitorn.modules.context_builder.types import ToolIndex


def _params_signature(schema: dict[str, Any]) -> str:
    """Build a compact params signature from a JSON Schema.

    Example output: ``"command, cwd?, timeout?"``
    """
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    if not props:
        return ""

    parts: list[str] = []
    for name, prop in props.items():
        if name.startswith("_"):
            continue
        suffix = "" if name in required else "?"
        parts.append(f"{name}{suffix}")
    return ", ".join(parts)


def _is_json_string_param(name: str, prop: dict[str, Any]) -> bool:
    """Detect if a parameter expects a JSON-encoded string value.

    Works with any MCP server naming convention:
    - Explicit ``_json`` suffix (e.g. ``properties_json``, ``children_json``)
    - Description keywords (e.g. "JSON string", "serialized JSON", "JSON-encoded")
    - Schema hints: type=string + description mentions JSON
    """
    if name.endswith("_json"):
        return True
    desc = (prop.get("description") or "").lower()
    if not desc:
        return False
    ptype = prop.get("type", "")
    if "anyOf" in prop:
        for option in prop["anyOf"]:
            if option.get("type") != "null":
                ptype = option.get("type", "")
                break
    if ptype and ptype != "string":
        return False
    json_keywords = (
        "json string", "json object", "json array",
        "serialized json", "json-encoded", "json encoded",
        "stringify", "stringified",
    )
    return any(kw in desc for kw in json_keywords)


def _parse_args_section(description: str) -> dict[str, str]:
    """Parse ``Args:`` section from a docstring-style description.

    Returns ``{param_name: description_text}``.
    """
    import re

    result: dict[str, str] = {}
    lines = description.split("\n")
    in_args = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("args:"):
            in_args = True
            continue
        if in_args:
            if not stripped or (stripped and not line.startswith((" ", "\t")) and ":" in stripped):
                if not stripped:
                    continue
                if not line.startswith((" ", "\t")):
                    break
            m = re.match(r"^\s+(\w+)(?:\s*\([^)]*\))?\s*:\s*(.+)", line)
            if m:
                result[m.group(1)] = m.group(2).strip()
    return result


def _build_param_details(
    params: dict[str, Any],
    description: str = "",
) -> list[str]:
    """Build compact parameter detail lines for MCP tools.

    Only emits details when there are 2+ non-internal parameters.
    Detects ``_json`` suffix to warn about JSON string vs object confusion.
    """
    props = params.get("properties", {})
    required = set(params.get("required", []))

    visible = {k: v for k, v in props.items() if not k.startswith("_")}
    if len(visible) < 2:
        return []

    arg_descs = _parse_args_section(description) if description else {}

    lines: list[str] = []
    for name, prop in visible.items():
        ptype = prop.get("type", "string")
        if "anyOf" in prop:
            for option in prop["anyOf"]:
                if option.get("type") != "null":
                    ptype = option.get("type", "string")
                    break

        is_json_str = _is_json_string_param(name, prop)
        if is_json_str:
            ptype = "JSON string"

        default = prop.get("default")
        if name in required:
            req_str = "required"
        elif default is not None and default != "null":
            req_str = f'default: "{default}"' if isinstance(default, str) else f"default: {default}"
        else:
            req_str = "optional"

        desc = prop.get("description", "") or arg_descs.get(name, "")

        line = f"    {name} ({ptype}, {req_str})"
        if desc:
            line += f": {desc}"
        if is_json_str:
            line += " — pass as STRING not object"
        lines.append(line)

    return lines


def _group_tools_by_module(
    tools: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group OpenAI-format tool schemas by module prefix."""
    by_module: dict[str, list[dict[str, Any]]] = {}
    for t in tools:
        fn = t.get("function", {})
        fn_name = fn.get("name", "")
        if "__" in fn_name:
            mod = fn_name.split("__", 1)[0]
        else:
            mod = "_other"
        by_module.setdefault(mod, []).append(t)
    return by_module


def _build_direct_instructions(
    total_tools: int,
    *,
    native_tool_use: bool,
    tools: list[dict[str, Any]] | None,
    index: "ToolIndex | None" = None,
) -> str:
    """Build rich, grouped tool instructions for direct injection mode.

    Generates everything the LLM needs to know about available tools:
    - Tools grouped by module with descriptions
    - Key parameters shown inline (required + optional)
    - One-line description per tool

    The user's YAML system_prompt never needs to list tools.
    """
    if not tools:
        return f"You have {total_tools} tools available. Call them directly by name."

    # Prefer the actual count of tools being listed over `index.total_tools`
    # — the latter counts only indexed module actions while `tools` also
    # includes primitive meta-tools (run_parallel, background, etc).
    # System prompt said "9 tools" then listed 11 because of this mismatch.
    actual_total = len(tools) or total_tools

    if not native_tool_use:
        header = (
            f"You have {actual_total} tools available.\n"
            "Call them directly — no discovery step needed.\n\n"
            "To call a tool, output EXACTLY this XML format:\n\n"
            '<tool_call>{"name": "tool_name", "arguments": {"param": "value"}}</tool_call>\n\n'
            "## Tools"
        )
        tool_text = _render_tools_as_text(tools)
        return header + tool_text

    by_module = _group_tools_by_module(tools)

    parts: list[str] = [
        f"You have {actual_total} tools available across {len(by_module)} modules.",
        "Call them directly by their function name — no discovery step needed.",
        "",
    ]

    for mod, mod_tools in sorted(by_module.items()):
        cat_summary = ""
        if index:
            cat = index.categories.get(mod)
            if cat:
                cat_summary = f" — {cat.summary}"

        parts.append(f"## {mod} ({len(mod_tools)} tools){cat_summary}")
        if mod.startswith("mcp_"):
            parts.append(
                "**Status: CONNECTED** — call these tools directly, "
                "do NOT try to connect or configure anything."
            )
        parts.append("")

        for t in mod_tools:
            fn = t.get("function", {})
            fn_name = fn.get("name", "")
            desc = fn.get("description", "")
            params = fn.get("parameters", {})

            short = desc.split(".")[0].strip() if desc else ""
            sig = _params_signature(params)
            sig_str = f"({sig})" if sig else "()"

            # Add safety badges from index metadata
            badges = ""
            if index:
                sep_pos = fn_name.rfind("__")
                fqn = (f"{fn_name[:sep_pos]}.{fn_name[sep_pos + 2:]}"
                       if sep_pos > 0 else fn_name)
                indexed = index.tools.get(fqn)
                if indexed:
                    if indexed.irreversible:
                        badges += " **IRREVERSIBLE**"
                    if indexed.side_effects:
                        badges += f" [side-effects: {', '.join(indexed.side_effects)}]"
            else:
                indexed = None
                fqn = fn_name

            parts.append(f"- **{fn_name}**{sig_str}: {short}{badges}")

            if index:
                if indexed and "mcp" in indexed.tags:
                    param_lines = _build_param_details(params, desc)
                    if param_lines:
                        parts.extend(param_lines)

                if indexed and getattr(indexed, "output_schema", None):
                    out = indexed.output_schema
                    out_type = out.get("type", "object")
                    out_props = out.get("properties", {})
                    if out_props:
                        keys = ", ".join(list(out_props.keys())[:6])
                        parts.append(f"  → returns {out_type} with: {keys}")

                if indexed and indexed.examples:
                    for ex in indexed.examples[:2]:
                        ex_val = ex.get("value", ex) if isinstance(ex, dict) else ex
                        ex_name = ex.get("name", "") if isinstance(ex, dict) else ""
                        label = f" ({ex_name})" if ex_name else ""
                        parts.append(
                            f"  Example{label}: `{json.dumps(ex_val, ensure_ascii=False)}`"
                        )

        if index:
            hints = _build_mcp_workflow_hints(mod, mod_tools, index)
            if hints:
                parts.extend(hints)

        if index:
            struct_hints = _build_structural_hints(mod, index)
            if struct_hints:
                parts.extend(struct_hints)

        parts.append("")

    return "\n".join(parts)


def _build_compact_direct_instructions(
    total_tools: int,
    *,
    tools: list[dict[str, Any]] | None,
    index: "ToolIndex | None" = None,
) -> str:
    """Build compact tool instructions — names + one-liners, no full schemas.

    Used when full tool schemas don't fit in context but tools can still be
    called directly by name (no meta-tools needed). The LLM gets enough info
    to pick the right tool, then calls it with its best guess at parameters.
    """
    if not tools:
        return f"You have {total_tools} tools available. Call them directly by name."

    by_module = _group_tools_by_module(tools)

    parts: list[str] = [
        f"You have {total_tools} tools across {len(by_module)} modules.",
        "Call them directly by function name. Key parameters shown inline.",
        "",
    ]

    for mod, mod_tools in sorted(by_module.items()):
        parts.append(f"**{mod}** ({len(mod_tools)} tools):")
        for t in mod_tools:
            fn = t.get("function", {})
            fn_name = fn.get("name", "")
            desc = fn.get("description", "")
            params = fn.get("parameters", {})
            short = desc.split(".")[0].strip() if desc else ""
            sig = _params_signature(params)
            parts.append(f"  {fn_name}({sig}): {short}")
        parts.append("")

    return "\n".join(parts)


def _build_mcp_workflow_hints(
    mod: str,
    mod_tools: list[dict[str, Any]],
    index: "ToolIndex",
) -> list[str]:
    """Auto-detect workflow patterns for MCP tools.

    Detects:
    - "getter before setter" — when a write tool has *_id params and a
      corresponding get_* tool exists, suggest calling the getter first.
    - JSON-string params that reference structured data (properties, blocks)
      where inspecting an existing resource reveals the expected format.
    """
    if not mod.startswith("mcp_"):
        return []

    tool_actions: dict[str, str] = {}
    getter_actions: set[str] = set()
    writer_actions: dict[str, dict[str, Any]] = {}

    for t in mod_tools:
        fn = t.get("function", {})
        fn_name = fn.get("name", "")
        params = fn.get("parameters", {})
        sep_pos = fn_name.rfind("__")
        action = fn_name[sep_pos + 2:] if sep_pos > 0 else fn_name
        tool_actions[action] = fn_name

        if action.startswith("get_") or action.startswith("list_"):
            getter_actions.add(action)

        props = params.get("properties", {})
        json_params = [p for p in props if _is_json_string_param(p, props.get(p, {}))]
        if json_params:
            writer_actions[action] = params

    if not writer_actions:
        return []

    hints: list[str] = []
    all_json_params: set[str] = set()

    for writer_action, params in writer_actions.items():
        props = params.get("properties", {})

        for param_name in props:
            if not param_name.endswith("_id"):
                continue
            entity = param_name.removesuffix("_id")
            getter = f"get_{entity}"
            if getter in getter_actions:
                getter_fn = tool_actions.get(getter, getter)
                writer_fn = tool_actions.get(writer_action, writer_action)
                hints.append(
                    f"  Tip: call **{getter_fn}**({param_name}) before "
                    f"**{writer_fn}** to discover the exact schema/properties."
                )

        for param_name in props:
            if _is_json_string_param(param_name, props.get(param_name, {})):
                all_json_params.add(param_name)

    if all_json_params and getter_actions:
        read_tools = sorted(
            getter_actions,
            key=lambda a: (0 if a.startswith("get_") else 1, a),
        )
        read_fns = [tool_actions[a] for a in read_tools[:3]]
        params_str = ", ".join(f"`{p}`" for p in sorted(all_json_params))
        hints.append(
            f"  **MANDATORY**: {params_str} expect complex nested JSON strings. "
            f"If no copy-paste template is shown below, you MUST call "
            f"{' or '.join(f'**{fn}**' for fn in read_fns)} first to see "
            f"the real structure, then copy that EXACT format — do NOT "
            f"invent fields, do NOT add fields like 'object', 'annotations', "
            f"'link', 'plain_text' unless they appear in the response."
        )

    return hints


def _build_structural_hints(
    mod: str,
    index: "ToolIndex",
) -> list[str]:
    """Inject copy-paste-ready templates for writer tool JSON params.

    100% dynamic — no hardcoded API-specific mappings.  Works with any
    MCP server (Notion, Google, GitHub, Slack, etc.).

    Strategy:
    1. Collect all writer tools (tools with JSON string params) for this module
    2. For each probed getter response, detect what kind of structure it is
       (array of typed objects = content/blocks, dict with typed values = properties)
    3. Match getter structures to writer params by structural shape analysis
    4. Present as copy-paste-ready templates labeled by param name
    """
    hints_map = getattr(index, "mcp_structural_hints", {})
    server_hints = hints_map.get(mod)
    if not server_hints:
        return []

    writer_info: dict[str, dict[str, Any]] = {}
    for fqn, tool in index.tools.items():
        if tool.module_id != mod:
            continue
        props = tool.params_schema.get("properties", {})
        json_params = {
            p: props[p] for p in props
            if _is_json_string_param(p, props[p])
        }
        if json_params:
            writer_info[tool.action_name] = json_params

    if not writer_info:
        return _build_raw_structural_hints(mod, server_hints)

    all_json_params: set[str] = set()
    for params in writer_info.values():
        all_json_params.update(params.keys())

    param_templates = _match_templates_to_params(
        server_hints, all_json_params,
    )

    parts: list[str] = [
        "",
        f"### {mod} — COPY-PASTE Templates for JSON Parameters",
        "",
        "**MANDATORY**: Use EXACTLY the structures below for JSON string "
        "parameters. Do NOT invent fields — only use what is shown.",
        "",
    ]

    for param_name, template in param_templates.items():
        users = [
            action for action, params in writer_info.items()
            if param_name in params
        ]
        users_str = ", ".join(f"`{u}`" for u in users) if users else ""

        parts.append(
            f"#### `{param_name}` parameter"
            f"{f' (used by {users_str})' if users_str else ''}"
        )
        parts.append("Pass this as a **JSON string** (not an object). Copy this template:")
        parts.append("```json")
        parts.append(template)
        parts.append("```")
        parts.append("")

    matched_getters = set()
    for getter_name in server_hints:
        for param_name in param_templates:
            if param_templates[param_name] == server_hints[getter_name]:
                matched_getters.add(getter_name)
    for getter_name, template in server_hints.items():
        if getter_name not in matched_getters:
            parts.append(f"**{getter_name}** response (reference):")
            parts.append("```json")
            parts.append(template)
            parts.append("```")
            parts.append("")

    if param_templates:
        first_param = next(iter(param_templates))
        parts.append("#### CORRECT vs WRONG")
        parts.append("")
        parts.append("WRONG (invented fields — will fail):")
        parts.append('```json')
        parts.append('[{"object": "block", "type": "paragraph", '
                     '"paragraph": {"text": [{"text": {"content": "..."}}]}}]')
        parts.append('```')
        parts.append("")
        parts.append(f"CORRECT (copy from `{first_param}` template above):")
        first_template = param_templates[first_param]
        compact = first_template.replace("\n", "").replace("  ", "")
        if len(compact) > 200:
            compact = compact[:200] + "..."
        parts.append(f'```json')
        parts.append(compact)
        parts.append('```')
        parts.append("")

    parts.append(
        "**RULES**: (1) Do NOT add fields not shown in the template. "
        "(2) Replace `\"...\"` with your actual content. "
        "(3) You can add more items to arrays following the same structure. "
        "(4) Do NOT add metadata fields (id, created_time, object, annotations, "
        "link, plain_text, href) — they are read-only."
    )

    return parts


def _build_raw_structural_hints(
    mod: str,
    server_hints: dict[str, str],
) -> list[str]:
    """Fallback: show raw getter responses when no writer tools are found."""
    parts: list[str] = [
        "",
        f"### {mod} — API Response Formats (auto-discovered)",
        "",
    ]
    for tool_name, template in server_hints.items():
        parts.append(f"**{tool_name}** response:")
        parts.append("```json")
        parts.append(template)
        parts.append("```")
        parts.append("")
    return parts


def _match_templates_to_params(
    server_hints: dict[str, str],
    json_params: set[str],
) -> dict[str, str]:
    """Match getter response templates to writer param names.

    100% dynamic — no hardcoded API-specific mappings.

    Uses three strategies in priority order:

    1. **Entity name overlap**: getter ``get_blocks`` → param ``blocks_json``
       (strips get_/list_ prefix and _json suffix, checks substring match)

    2. **Structural shape**: array of typed objects → params with
       "content"/"children"/"blocks" in name; dict with typed values →
       params with "properties"/"config"/"schema" in name

    3. **Single param fallback**: if there's only one JSON param and one
       template, match them regardless of names

    Returns ``{param_name: template_json_string}``.
    """
    if not json_params or not server_hints:
        return {}

    result: dict[str, str] = {}
    used_getters: set[str] = set()

    for getter_name, template in server_hints.items():
        getter_entity = (
            getter_name
            .removeprefix("get_")
            .removeprefix("list_")
            .removeprefix("fetch_")
            .removeprefix("read_")
        )
        for param_name in json_params:
            if param_name in result:
                continue
            param_entity = param_name.removesuffix("_json")
            if (
                param_entity and getter_entity
                and (param_entity in getter_entity or getter_entity in param_entity)
            ):
                result[param_name] = template
                used_getters.add(getter_name)
                break

    unmatched_params = json_params - set(result.keys())
    unmatched_getters = {
        g: t for g, t in server_hints.items() if g not in used_getters
    }

    if unmatched_params and unmatched_getters:
        array_templates: list[tuple[str, str]] = []
        dict_templates: list[tuple[str, str]] = []

        for getter_name, template in unmatched_getters.items():
            shape = _detect_template_shape(template)
            if shape == "array":
                array_templates.append((getter_name, template))
            elif shape == "dict":
                dict_templates.append((getter_name, template))

        _ARRAY_KEYWORDS = {"content", "children", "blocks", "items", "elements", "body", "rows"}
        _DICT_KEYWORDS = {"properties", "config", "schema", "metadata", "attributes", "fields"}

        for param_name in list(unmatched_params):
            param_entity = param_name.removesuffix("_json")
            if param_entity in _ARRAY_KEYWORDS and array_templates:
                getter_name, template = array_templates.pop(0)
                result[param_name] = template
                used_getters.add(getter_name)
                unmatched_params.discard(param_name)
            elif param_entity in _DICT_KEYWORDS and dict_templates:
                getter_name, template = dict_templates.pop(0)
                result[param_name] = template
                used_getters.add(getter_name)
                unmatched_params.discard(param_name)

    remaining_params = json_params - set(result.keys())
    remaining_getters = {
        g: t for g, t in server_hints.items() if g not in used_getters
    }
    if len(remaining_params) == 1 and len(remaining_getters) == 1:
        param = next(iter(remaining_params))
        getter_name, template = next(iter(remaining_getters.items()))
        result[param] = template

    return result


def _detect_template_shape(template: str) -> str:
    """Detect the structural shape of a JSON template.

    Returns:
        "array" — top-level is a JSON array (list of items/blocks)
        "dict"  — top-level is a JSON object (properties/config)
        "unknown" — could not determine
    """
    stripped = template.strip()
    if stripped.startswith("["):
        return "array"
    if stripped.startswith("{"):
        return "dict"
    return "unknown"


def _build_discovery_instructions(
    total_tools: int,
    n_categories: int,
    tools: list[dict[str, Any]] | None = None,
    index: "ToolIndex | None" = None,
) -> str:
    """Build discovery instructions for large toolsets.

    Adapts to scale with a 3-tier strategy:

    - **Small** (≤ 20 categories): full detail — tools + examples inline
    - **Medium** (21–100 categories): compact — tools listed, no examples
    - **Large** (> 100 categories): minimal — one-line per category, no tools

    Examples are always available via ``get_tool()`` regardless of tier.
    This keeps the prompt under ~2K tokens even with 1000+ servers.
    """
    direct_names = []
    if tools:
        for t in tools:
            fn = t.get("function", {})
            name = fn.get("name", "")
            if name and "." not in name and "__" not in name:
                direct_names.append(name)

    parts: list[str] = [
        f"You have access to {total_tools} tools across {n_categories} domains.",
        "",
    ]

    if direct_names:
        parts.append("# DIRECT TOOLS (call these directly, no discovery needed)")
        parts.append("")
        parts.append(f"These tools are always available: {', '.join(direct_names)}")
        parts.append("Call them directly by name. Do NOT use search_tools or execute_tool for these.")
        parts.append("")

    if n_categories <= 20:
        tier = "small"
        max_tools_per_cat = 5
        show_examples = True
    elif n_categories <= 100:
        tier = "medium"
        max_tools_per_cat = 3
        show_examples = False
    else:
        tier = "large"
        max_tools_per_cat = 0
        show_examples = False

    if index and index.categories:
        parts.append("# AVAILABLE DOMAINS")
        parts.append("")

        sorted_cats = sorted(index.categories.items())

        if tier == "large":
            for cat_id, cat in sorted_cats:
                tool_list = ", ".join(cat.tool_names[:8])
                suffix = f" (+{cat.tool_count - 8} more)" if cat.tool_count > 8 else ""
                parts.append(f"- **{cat_id}** ({cat.tool_count}): {tool_list}{suffix}")
            parts.append("")

        for cat_id, cat in sorted_cats:
            if tier == "large":
                continue

            parts.append(f"## {cat_id} ({cat.tool_count} tools)")
            parts.append(cat.summary)

            shown = 0
            for fqn in cat.tool_names:
                if shown >= max_tools_per_cat:
                    remaining = cat.tool_count - shown
                    if remaining > 0:
                        parts.append(f"  … and {remaining} more")
                    break
                tool = index.tools.get(fqn)
                if tool:
                    short = tool.description.split(".")[0].strip()
                    parts.append(f"- {tool.action_name}: {short}")
                    if "mcp" in tool.tags:
                        props = tool.params_schema.get("properties", {})
                        json_params = [
                            p for p in props
                            if _is_json_string_param(p, props.get(p, {}))
                        ]
                        if json_params:
                            parts.append(
                                f"  Note: {', '.join(json_params)} — pass as JSON STRING, not object"
                            )
                    if show_examples and tool.examples:
                        ex = tool.examples[0]
                        ex_val = ex.get("value", ex) if isinstance(ex, dict) else ex
                        ex_name = ex.get("name", "") if isinstance(ex, dict) else ""
                        label = f" ({ex_name})" if ex_name else ""
                        parts.append(
                            f"  Example{label}: `{json.dumps(ex_val, ensure_ascii=False)}`"
                        )
                    shown += 1
            parts.append("")

        if tier != "large":
            from collections import Counter
            tag_counts: Counter[str] = Counter()
            for tool in index.tools.values():
                tag_counts.update(tool.tags)
            skip = {"core", "system", "discovery", "tools", "read", "write"}
            useful_tags = sorted(
                t for t, c in tag_counts.items()
                if c >= 2 and t not in skip
            )
            if useful_tags:
                parts.append(f"Capabilities: {', '.join(useful_tags)}")
                parts.append("")

    if index:
        hints_map = getattr(index, "mcp_structural_hints", {})
        if hints_map:
            for mod_id, server_hints in sorted(hints_map.items()):
                struct_lines = _build_structural_hints(mod_id, index)
                if struct_lines:
                    parts.extend(struct_lines)
                    parts.append("")

    parts.append("# HOW TO USE TOOLS")
    parts.append("")
    if tools:
        parts.append(f"You have {len(tools)} meta-tools to discover and execute any tool:")
        for tool in tools:
            fn = tool.get("function", {})
            name = fn.get("name", "")
            desc = fn.get("description", "")
            sig = _params_signature(fn.get("parameters", {}))
            short_desc = desc.split(".")[0].strip() if desc else ""
            parts.append(f"- **{name}**({sig}): {short_desc}")
    else:
        parts.append(
            "You have meta-tools to discover and execute any tool: "
            "search_tools, get_tool, execute_tool, list_categories, browse_category."
        )

    parts.append("")
    parts.append("Workflow:")
    parts.append("1. **search_tools**(query) — find tools by natural language")
    parts.append("2. **get_tool**(name) — get full schema + usage examples")
    parts.append("3. **execute_tool**(name, params) — run it")
    parts.append("")
    parts.append("You can also browse_category(category) to see all tools in a domain.")
    parts.append("IMPORTANT: Always call get_tool() before calling execute_tool() — "
                 "it returns the exact parameter schema and usage examples.")
    parts.append("")
    parts.append("**CRITICAL: You can ONLY call these functions directly: "
                 "search_tools, get_tool, execute_tool, list_categories, browse_category. "
                 "ALL other tools MUST be called via execute_tool(name, params). "
                 "NEVER call a tool like `agent_status(...)` directly — "
                 "use `execute_tool(name=\"agent_spawn.agent_status\", params={...})` instead.**")

    return "\n".join(parts)


_TEXT_TOOL_USE_HEADER = """\
You have access to {total_tools} tools across {n_categories} domains.


You have the following tools. To call a tool, output EXACTLY this XML format:

<tool_call>{{"name": "tool_name", "arguments": {{"param": "value"}}}}</tool_call>

You can call multiple tools in one response. Each tool call must be on its own line.
After all tool calls, the system will execute them and return the results.
Then you can respond based on the results.

IMPORTANT RULES:
- Output the <tool_call> tag EXACTLY as shown — the system parses it literally
- Arguments must be valid JSON inside the tag
- Do NOT wrap tool calls in markdown code blocks
- You can include text before/after tool calls
- Wait for results before making conclusions

## Tools\
"""

_TEXT_TOOL_ENTRY = """
### {name}
{description}
Parameters:
```json
{params_json}
```"""

_TEXT_TOOL_USE_FOOTER = """
## Workflow
1. Discover what's available (list or search)
2. Get the exact parameter schema before calling
3. Execute the tool with the correct parameters

Never guess tool names — always search first."""


def _render_tools_as_text(tools: list[dict[str, Any]]) -> str:
    """Render OpenAI-format tool schemas as text for the system prompt."""
    parts: list[str] = []
    for tool in tools:
        fn = tool.get("function", {})
        name = fn.get("name", "")
        desc = fn.get("description", "")
        params = fn.get("parameters", {})
        params_json = json.dumps(params, indent=2, ensure_ascii=False)
        parts.append(_TEXT_TOOL_ENTRY.format(
            name=name,
            description=desc,
            params_json=params_json,
        ))
    return "".join(parts)



# _build_primitive_section and its constants (_BASE_PRIMITIVE_NAMES,
# _WATCHER_PRIMITIVE_NAMES, _SCHEDULER_PRIMITIVE_NAMES) have been moved to
# each action mixin's _prompt_sections_*() method. The instructions are now
# injected via get_prompt_sections() on the ContextBuilderModule.


def _build_channels_section(
    channels_info: list[dict[str, Any]] | None,
    default_channel: str = "llm_notification",
) -> str:
    """Build dynamic instructions about available output channels.

    Fully data-driven — works with any channel type (email, SMS, Slack,
    Telegram, webhook, etc.) without hardcoded examples. Instructions and
    examples are generated from each channel's ``per_delivery_config_schema``.

    Args:
        channels_info: List of dicts with keys: name, type, per_delivery_config,
            has_user_resolver.
        default_channel: The default channel from execution.default_channel.
    """
    if not channels_info:
        return ""

    parts: list[str] = [
        "# OUTPUT CHANNELS",
        "",
        "Two ways to deliver through external channels:",
        "",
        "- **Send NOW**: `send_notification(channel, message, output_config)` "
        "— delivers immediately.",
        "- **Send LATER**: `cron_native.schedule(when, action=\"channels.send_message\", "
        "args={...})` "
        "— delivers at the scheduled time. Do NOT also call send_notification.",
        "",
        "IMPORTANT: When the user says \"in X minutes send...\", use ONLY "
        "`cron_native.schedule` with the `channels.send_message` action. "
        "The scheduler handles the delivery — do NOT call send_notification separately.",
        "",
        f"Default channel: **{default_channel}**",
        "",
        "Available channels:",
        "",
    ]

    has_resolver = False
    example_channel: str | None = None
    example_config: dict[str, str] = {}

    for ch in channels_info:
        name = ch["name"]
        ch_type = ch["type"]
        per_delivery = ch.get("per_delivery_config", {})
        resolver = ch.get("has_user_resolver", False)
        if resolver:
            has_resolver = True

        label = f"- **{name}** (type: `{ch_type}`)"
        if resolver:
            label += " — *auto-resolves user targets*"
        parts.append(label)

        required = per_delivery.get("required", {})
        optional = per_delivery.get("optional", {})
        if required:
            for field_name, desc in required.items():
                parts.append(f"  - `{field_name}` (required): {desc}")
        if optional:
            for field_name, desc in optional.items():
                parts.append(f"  - `{field_name}` (optional): {desc}")

        if example_channel is None and required:
            example_channel = name
            example_config = {k: f"<{k}>" for k in required}

    parts.append("")

    parts.append("## Targeting rules")
    parts.append("")

    if has_resolver:
        parts.append(
            "Channels marked *auto-resolves user targets* automatically look up "
            "the recipient's address from a data source using the current session. "
            "You do NOT need to specify `output_config` for these — just use the "
            "channel name and the system resolves the target."
        )
        parts.append("")

    parts.append(
        "**When the user provides an explicit address** (email, phone, URL, chat_id, etc.), "
        "you MUST pass it in `output_config` — this overrides the auto-resolver "
        "and ensures delivery. Check the channel's required fields above."
    )
    parts.append("")

    parts.append("## Examples")
    parts.append("")

    ch0 = channels_info[0]["name"]
    if example_channel and example_config:
        config_str = ", ".join(
            f'"{k}": "{v}"' for k, v in example_config.items()
        )

        parts.append("Send NOW (immediate delivery):")
        parts.append(
            f'  send_notification(channel="{example_channel}", '
            f'message="...", title="...", output_config={{{config_str}}})'
        )
        parts.append("")

        parts.append("Send LATER (delayed delivery — ONE call, no send_notification):")
        parts.append(
            f'  cron_native.schedule(when="in 5m", action="channels.send_message", '
            f'args={{"channel": "{example_channel}", "message": "Message content here", '
            f"\"output_config\": {{{config_str}}}}})"
        )
        parts.append("")

    return "\n".join(parts)


def build_system_prompt(
    agent_id: str,
    role: str,
    user_prompt: str,
    index: "ToolIndex",
    *,
    native_tool_use: bool = True,
    tool_injection: str = "discovery",
    tools: list[dict[str, Any]] | None = None,
    plan_first: bool = True,
    setup_summary: list[str] | None = None,
    channels_info: list[dict[str, Any]] | None = None,
    default_channel: str = "llm_notification",
    skills: list[dict[str, str]] | None = None,
    modules: dict[str, Any] | None = None,
) -> str:
    """Build the full system prompt for an agent.

    The context builder generates ALL tool-related instructions dynamically.
    The user's YAML system_prompt should only define personality and behavior.

    Combines:
    1. Agent identity header
    2. Tool instructions (auto-generated, varies by injection mode)
    3. Behavioral guidelines
    4. Pre-configured resources
    5. User-defined system prompt (personality only)
    """
    parts: list[str] = []

    # Module prompt sections (memory, spawn, etc.) are now
    # injected via get_prompt_sections() on each module — see collector below.

    parts.append(f'You are agent "{agent_id}" (role: {role}).')

    if tool_injection == "direct":
        parts.append(_build_direct_instructions(
            total_tools=index.total_tools,
            native_tool_use=native_tool_use,
            tools=tools,
            index=index,
        ))
    elif tool_injection == "compact_direct":
        parts.append(_build_compact_direct_instructions(
            total_tools=index.total_tools,
            tools=tools,
            index=index,
        ))
    elif native_tool_use:
        parts.append(_build_discovery_instructions(
            total_tools=index.total_tools,
            n_categories=index.total_categories,
            tools=tools,
            index=index,
        ))
    else:
        header = _TEXT_TOOL_USE_HEADER.format(
            total_tools=index.total_tools,
            n_categories=index.total_categories,
        )
        tool_text = _render_tools_as_text(tools or [])
        parts.append(header + tool_text + _TEXT_TOOL_USE_FOOTER)

    # Primitive sections (parallel, background, watchers, scheduler)
    # are now provided by each mixin's _prompt_sections_*() method, collected
    # via get_prompt_sections() on each module — see module sections collector below.

    # Core behavioral rules — same as fine-tuning system prompts
    parts.append(
        "# How to think\n"
        "\n"
        "## Understand the goal BEFORE acting\n"
        "When you receive a request, STOP and ask yourself:\n"
        "- What is the user actually trying to achieve? (not just what they literally said)\n"
        "- What information do I need to accomplish this?\n"
        "- What is the simplest approach that works?\n"
        "Do NOT start calling tools until you have a clear mental plan.\n"
        "\n"
        "## Plan, then execute\n"
        "For any non-trivial task:\n"
        "1. State your plan in 2-3 sentences (the user needs to see what you're doing)\n"
        "2. Execute the plan with precise tool calls\n"
        "3. Verify the result\n"
        "4. Report what you did and what you found\n"
        "Never discover your plan by trial and error. Think first.\n"
        "\n"
        "## Try the simplest approach first\n"
        "- One Grep often answers the question. Don't read 10 files when 1 search works.\n"
        "- A 3-line fix is better than a 50-line refactor.\n"
        "- Ask yourself: can I answer this in 2-3 tool calls? If yes, do that.\n"
        "\n"
        "## Diagnose before retrying\n"
        "When something fails:\n"
        "- Read the error message carefully — it usually tells you exactly what's wrong\n"
        "- Understand WHY it failed before trying again\n"
        "- Never retry the same thing hoping for a different result\n"
        "- If stuck after 2 attempts, try a completely different approach\n"
        "\n"
        "## Know when to stop\n"
        "- Answer the question that was asked — nothing more\n"
        "- Don't add features, comments, docstrings, or refactoring nobody requested\n"
        "- Don't 'improve' code you weren't asked to touch\n"
        "- When the task is done, say so. Don't look for more work.\n"
        "\n"
        "# How to use tools\n"
        "\n"
        "## CRITICAL: Use dedicated tools — NEVER use Bash for file operations\n"
        "- Read files → Read (NEVER cat, head, tail, less, bat)\n"
        "- Edit files → Edit (NEVER sed, awk, perl -i)\n"
        "- Create files → Write (NEVER echo, printf, cat with heredoc)\n"
        "- Search content → Grep (NEVER grep, rg, ack, ag)\n"
        "- Find files → Glob (NEVER find, ls -R, dir /s, tree)\n"
        "- Bash is ONLY for: git, build tools (make, npm, pip), test runners (pytest, jest), "
        "package managers, and system commands (env, which, whoami)\n"
        "\n"
        "## How to explore a codebase\n"
        "\n"
        "Follow this exact sequence — 5-8 calls total, not 50:\n"
        "\n"
        "Step 1 — Structure (1 call):\n"
        "  Glob('**/*.py') or Glob('src/**/*') → see all files at once\n"
        "\n"
        "Step 2 — Entry points (2 calls, in parallel):\n"
        "  Grep('def main|__main__|entry', glob='*.py')\n"
        "  Read('pyproject.toml') or Read('package.json')\n"
        "\n"
        "Step 3 — Targeted reads (parallel, just the top of key files):\n"
        "  Read('src/main.py', limit=50)\n"
        "  Read('README.md', limit=80)\n"
        "\n"
        "Step 4 — Deep dive only where needed:\n"
        "  Grep('class UserService') → find exact file + line\n"
        "  Read('src/services/user.py', offset=42, limit=30) → read just that section\n"
        "\n"
        "## How to edit code\n"
        "1. Read the file (or section) first — Edit fails on unread files\n"
        "2. Edit with exact old_string copied from the Read output\n"
        "3. Read the changed section to verify — catch mistakes immediately\n"
        "4. Run tests — Bash('pytest') or Bash('npm test')\n"
        "5. Check 'lint' in Edit/Write results — fix errors before moving on\n"
        "\n"
        "## How to work efficiently\n"
        "- Call multiple independent tools in the SAME turn — they run in parallel\n"
        "  (3x Read + 2x Grep + 1x Glob = one round trip, not six)\n"
        "- For complex tasks (3+ steps): create tasks with TaskCreate, update as you go\n"
        "- Delegate heavy work to sub-agents (Agent tool) to protect your context window\n"
        "- Store key findings with Remember — they survive context compaction\n"
        "- When uncertain about user intent: ask instead of guessing"
    )

    if plan_first:
        parts.append(
            "# How to communicate\n"
            "\n"
            "The user can only see your text responses. They cannot see tool names, "
            "parameters, or raw results — only what you write.\n"
            "\n"
            "For every request, include a **content** field in your response alongside "
            "any tool calls. In that text, briefly describe what you are about to do. "
            "Example:\n"
            "\n"
            '  content: "I\'ll set up the project structure with a backend API and '
            'database models. Let me start."\n'
            "  tool_calls: [ ... ]\n"
            "\n"
            "After tool results come back, explain what happened and what you'll do next.\n"
            "\n"
            "This is critical — without your explanations the user sees a blank screen "
            "while tools run silently."
        )

    channels_section = _build_channels_section(channels_info, default_channel)
    if channels_section:
        parts.append(channels_section)

    if setup_summary:
        lines = ["# PRE-CONFIGURED RESOURCES", ""]
        lines.append("The following resources were set up at startup and are ready to use:")
        for entry in setup_summary:
            lines.append(f"- {entry}")
        lines.append("")
        lines.append("You do NOT need to configure these again — use them directly.")
        parts.append("\n".join(lines))

    if skills:
        skill_lines = ["# Available Skills", ""]
        skill_lines.append(
            "You have reusable workflows (skills) available. "
            "Call use_skill(command=\"/name\") to load detailed instructions."
        )
        skill_lines.append("")
        for s in skills:
            cmd = s.get("command", "")
            desc = s.get("description", "")
            skill_lines.append(f"  - {cmd}: {desc}")
        parts.append("\n".join(skill_lines))

    # Collect prompt sections from active modules
    if modules:
        end_sections: list[tuple[int, str]] = []  # (priority, content)
        for _mod_id, _mod in modules.items():
            _sections_fn = getattr(_mod, "get_prompt_sections", None)
            if _sections_fn is None:
                continue
            try:
                for sec in _sections_fn():
                    title = sec.get("title", "")
                    content = sec.get("content", "")
                    priority = sec.get("priority", 50)
                    if not content:
                        continue
                    block = f"# {title}\n{content}" if title else content
                    end_sections.append((priority, block))
            except Exception:
                logger.debug("failed to build prompt section", exc_info=True)
        for _, block in sorted(end_sections):
            parts.append(block)

    # Inject tool_prompt instructions from indexed tools
    # These are detailed usage guides for each tool — injected in the system prompt
    # so the LLM knows how to use tools correctly (like Claude Code's prompt() method)
    if index is not None:
        # Collect dynamic tool prompts from modules that override them at runtime.
        # Any module can implement get_dynamic_tool_prompts() -> dict[fqn, prompt]
        # to inject app-specific instructions (e.g. workspace module injects
        # "you write LaTeX" or "you write React" based on app.yaml config).
        dynamic_prompts: dict[str, str] = {}
        for _mod in (modules or {}).values():
            getter = getattr(_mod, "get_dynamic_tool_prompts", None)
            if getter and callable(getter):
                dynamic_prompts.update(getter())

        tool_prompts: list[str] = []
        for _fqn, _tool in index.tools.items():
            # Dynamic prompt overrides static tool_prompt
            tp = dynamic_prompts.get(_fqn) or getattr(_tool, "tool_prompt", "")
            if tp:
                tool_prompts.append(f"## {_tool.action_name}\n{tp}")
        if tool_prompts:
            parts.append("# Tool Usage Instructions\n" + "\n\n".join(tool_prompts))

    if user_prompt.strip():
        parts.append(
            "# APP-DEFINED PERSONALITY\n"
            "(The following section was written by the app developer.)\n\n"
            + user_prompt.strip()
        )

    return "\n\n".join(parts)

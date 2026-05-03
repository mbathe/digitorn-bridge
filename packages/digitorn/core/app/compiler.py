"""AppYAMLCompiler - parse, validate, and resolve an app YAML into executable IR.

The compiler is **pure validation** - no side effects, no I/O beyond reading
the YAML file.  It needs read access to the ``ModuleRegistry`` to look up
manifests and action specs for validation, but never mutates anything.

Pipeline::

    YAML file/dict
      → Parse & validate against AppDefinition (Pydantic)
      → Resolve {{variables}} in all params/constraints
      → Validate modules exist in registry
      → Validate actions exist on each module
      → Validate params against each action's params_model
      → Validate constraints against ConstraintSpec
      → Build SecurityProfile from capabilities
      → Return CompiledApp

All errors are collected and raised together so the user sees every problem
at once rather than fixing them one by one.
"""

from __future__ import annotations

import logging
import re as _re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from digitorn.core.app.errors import (
    ActionNotFoundError,
    AppCompilationError,
    ConstraintValidationError,
    ModuleNotFoundError,
    ParamsValidationError,
    VariableResolutionError,
)
from digitorn.core.app.schema import (
    AgentBrain,
    AgentDefinition,
    AppDefinition,
    AppMeta,
    ChannelInstanceConfig,
    ExecutionConfig,
    ModuleBlock,
)
from digitorn.core.app.variables import resolve_variables
from digitorn.core.app.yaml_loader import (
    Position,
    PositionMap,
    format_location,
    load_with_positions,
    pydantic_loc_to_path,
)
from digitorn.core.security import ModuleGrant, SecurityProfile

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from digitorn.modules.registry import ModuleRegistry


def _is_safe_name(name: str) -> bool:
    """True iff ``name`` is a safe identifier for URL segments + filenames."""
    import re as _re
    return bool(_re.match(r"^[a-z0-9][a-z0-9_-]*[a-z0-9]$|^[a-z0-9]$", name))


_PLACEHOLDER_RE = _re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

_ALLOWED_FILTERS: frozenset[str] = frozenset({
    "upper", "lower", "title", "capitalize",
    "truncate", "default", "length", "trim", "strip",
    "date", "relative_time", "money", "number", "percent",
    "json", "json_pretty", "yaml",
    "filter", "map", "pluck", "join", "split",
    "first", "last", "sort", "reverse", "slice",
    "replace", "markdown",
    "plus_days", "minus_days", "plus_hours", "minus_hours",
    "filter_search", "source_icon", "tree_icon", "kind_color",
    "status_color", "sev_color",
    "urlencode", "b64encode", "b64decode",
    "int", "float", "bool", "str",
    "escape", "safe", "md5", "sha1", "sha256",
})


def _walk_strings(value: Any, path: str = "$") -> Any:
    """Yield ``(path, string_value)`` tuples for every string inside a nested structure."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _walk_strings(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk_strings(v, f"{path}[{i}]")


def _validate_placeholder_references(
    raw: dict[str, Any], definition: Any, errors: list[str],
) -> None:
    """Walk the raw YAML, find every ``{{...}}`` placeholder, and validate:

    - ``{{credential.PROVIDER.FIELD}}``: PROVIDER must be declared in
      ``execution.credentials_schema.providers`` with a matching FIELD.
    - ``{{variable_name}}``: must be declared in the ``variables:`` block
      or be a reserved namespace (``env``, ``secret``, ``sys``, ``app``,
      ``credential``, ``field``, ``config_data``, ``client``, ``tool``,
      ``event``, ``state``, ``runtime_context``, ``workspace``).

    Emits clear errors with path, offending placeholder, and fuzzy-match
    suggestions for typos.
    """
    declared_vars: set[str] = set((getattr(getattr(definition, "dev", None), "variables", {}) or {}).keys())

    providers_map: dict[str, set[str]] = {}
    sec = getattr(definition, "security", None)
    cs = getattr(sec, "credentials_schema", None) if sec is not None else None
    if cs is not None:
        for prov in (getattr(cs, "providers", []) or []):
            pname = getattr(prov, "name", "") or ""
            if not pname:
                continue
            providers_map[pname] = {
                getattr(f, "name", "") for f in (getattr(prov, "fields", []) or [])
                if getattr(f, "name", "")
            }

    _RESERVED_ROOT = {
        "env", "secret", "sys", "app", "field", "config_data", "client",
        "tool", "event", "state", "runtime_context", "workspace", "agent",
        "prompt", "skill", "asset", "behavior",
        "input", "steps", "output", "caller", "request",
        # Flow runtime context (Phase 2 flow: block).
        "previous", "approvals", "session",
    }

    for path, s in _walk_strings(raw):
        if path.startswith("$.behavior"):
            continue
        for m in _PLACEHOLDER_RE.finditer(s):
            full_expr = m.group(1).strip()
            parts = [p.strip() for p in full_expr.split("|")]
            expr = parts[0]
            for fpart in parts[1:]:
                if not fpart:
                    continue
                fname = fpart.split(":", 1)[0].split("(", 1)[0].strip()
                if not fname:
                    continue
                if fname not in _ALLOWED_FILTERS:
                    import difflib as _df
                    sug = _df.get_close_matches(fname, _ALLOWED_FILTERS, n=3, cutoff=0.6)
                    hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
                    errors.append(
                        f"{path}: placeholder '{{{{{full_expr}}}}}' uses unknown "
                        f"filter '{fname}'. Known filters: "
                        f"{sorted(_ALLOWED_FILTERS)}.{hint}"
                    )
            if expr.startswith("credential."):
                rest = expr[len("credential."):].split(".")
                if len(rest) < 2:
                    errors.append(
                        f"{path}: credential placeholder '{{{{{expr}}}}}' must be "
                        f"of the form '{{{{credential.PROVIDER.FIELD}}}}'"
                    )
                    continue
                prov_name, field_name = rest[0], rest[1]
                if prov_name not in providers_map:
                    import difflib as _df
                    suggestions = _df.get_close_matches(
                        prov_name, providers_map.keys(), n=3, cutoff=0.6,
                    )
                    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
                    if providers_map:
                        errors.append(
                            f"{path}: credential placeholder references provider "
                            f"'{prov_name}' which is NOT declared in "
                            f"execution.credentials_schema.providers. "
                            f"Declared: {sorted(providers_map.keys())}.{hint}"
                        )
                    else:
                        errors.append(
                            f"{path}: credential placeholder '{{{{{expr}}}}}' is used "
                            f"but execution.credentials_schema.providers is empty. "
                            f"Declare provider '{prov_name}' before referencing it."
                        )
                    continue
                fields = providers_map[prov_name]
                if field_name not in fields:
                    import difflib as _df
                    suggestions = _df.get_close_matches(
                        field_name, fields, n=3, cutoff=0.6,
                    )
                    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
                    errors.append(
                        f"{path}: field '{field_name}' is not declared on provider "
                        f"'{prov_name}'. Known fields: {sorted(fields)}.{hint}"
                    )
                continue

            head = expr.split(".", 1)[0].strip()
            if "[" in head:
                head = head.split("[", 1)[0].strip()
            if head in _RESERVED_ROOT or head == "credential":
                continue
            if head in declared_vars:
                continue
            if declared_vars or exe is None:
                import difflib as _df
                candidates = list(declared_vars) + sorted(_RESERVED_ROOT) + ["credential"]
                suggestions = _df.get_close_matches(head, candidates, n=3, cutoff=0.6)
                hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
                errors.append(
                    f"{path}: placeholder '{{{{{expr}}}}}' references undefined "
                    f"variable '{head}'. Declared: {sorted(declared_vars)}.{hint}"
                )


def _validate_dependency_graph(definition: Any, errors: list[str]) -> None:
    """Cross-check references between sections of the YAML.

    Currently checks:
    - Agent ids unique
    - Coordinator's delegate_to references existing specialist ids
    - Capabilities.grant modules are declared in the modules block
    - execution.default_channel exists in modules.channels.config.providers
    - hooks referencing module_action target an actually-loaded module
    """
    agent_ids: list[str] = []
    seen_agent_ids: set[str] = set()
    for i, a in enumerate(getattr(definition, "agents", []) or []):
        aid = getattr(a, "id", "")
        if not aid:
            errors.append(f"agents[{i}]: missing required 'id' field")
            continue
        if aid in seen_agent_ids:
            errors.append(f"agents[{i}]: duplicate agent id '{aid}'")
        seen_agent_ids.add(aid)
        agent_ids.append(aid)

    for i, a in enumerate(getattr(definition, "agents", []) or []):
        delegates = getattr(a, "delegate_to", None) or []
        for target in delegates:
            if target not in seen_agent_ids:
                import difflib as _df
                sug = _df.get_close_matches(target, seen_agent_ids, n=3, cutoff=0.6)
                hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
                errors.append(
                    f"agents[{i}].delegate_to: unknown agent id '{target}'. "
                    f"Declared: {sorted(seen_agent_ids)}.{hint}"
                )

    module_ids = set((getattr(getattr(definition, "tools", None), "modules", {}) or {}).keys())
    _SYSTEM_MODULES = {"context_builder", "llm_provider", "index", "agent_spawn"}
    caps = getattr(getattr(definition, "tools", None), "capabilities", None)
    if caps is not None:
        for i, grant in enumerate(getattr(caps, "grant", []) or []):
            gmod = getattr(grant, "module", "")
            if gmod.startswith("mcp_"):
                continue
            if gmod and gmod not in module_ids and gmod not in _SYSTEM_MODULES:
                import difflib as _df
                candidates = module_ids | _SYSTEM_MODULES
                sug = _df.get_close_matches(gmod, candidates, n=3, cutoff=0.6)
                hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
                errors.append(
                    f"capabilities.grant[{i}].module: module '{gmod}' is not "
                    f"declared in the modules block. Declared: "
                    f"{sorted(module_ids)}.{hint}"
                )

    exe = getattr(definition, "runtime", None)
    default_channel = getattr(exe, "default_channel", "") if exe is not None else ""
    _BUILTIN_CHANNELS = {
        "llm_notification", "webhook", "log", "gmail",
        "telegram", "sms", "slack", "email", "hook",
    }
    if default_channel and default_channel not in _BUILTIN_CHANNELS:
        providers = set()
        channels_block = (definition.tools.modules or {}).get("channels")
        if channels_block is not None:
            cfg = getattr(channels_block, "config", {}) or {}
            providers = set((cfg.get("providers") or {}).keys())
        top_channels = getattr(getattr(definition, "tools", None), "channels", {}) or {}
        providers |= set(top_channels.keys())
        if default_channel not in providers:
            import difflib as _df
            candidates = providers | _BUILTIN_CHANNELS
            sug = _df.get_close_matches(default_channel, candidates, n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
            errors.append(
                f"execution.default_channel: '{default_channel}' is neither a "
                f"built-in channel type nor declared in "
                f"modules.channels.config.providers. Built-in: "
                f"{sorted(_BUILTIN_CHANNELS)}. Declared providers: "
                f"{sorted(providers)}.{hint}"
            )

    hooks_list = list(getattr(exe, "hooks", []) or []) if exe is not None else []
    for a in getattr(definition, "agents", []) or []:
        hooks_list.extend(getattr(a, "hooks", []) or [])
    for i, hook in enumerate(hooks_list):
        action = getattr(hook, "action", None)
        if action is None:
            continue
        action_type = getattr(action, "type", "")
        if action_type not in ("module_action", "module_action_inject"):
            continue
        params = action.model_dump() if hasattr(action, "model_dump") else {}
        target_module = params.get("module", "")
        if target_module and target_module not in module_ids:
            import difflib as _df
            sug = _df.get_close_matches(target_module, module_ids, n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
            errors.append(
                f"hooks[{hook.id}].action.module: module '{target_module}' "
                f"is not declared. Declared: {sorted(module_ids)}.{hint}"
            )

    _BUILTIN_APP_MIDDLEWARE = {
        "mask_secrets", "prompt_inject", "content_filter",
        "rag_inject", "response_filter",
    }
    _BUILTIN_MODULE_MIDDLEWARE = {"audit", "retry", "timeout"}
    app_mw = list(getattr(getattr(definition, "runtime", None), "middleware", []) or [])
    for i, entry in enumerate(app_mw):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        if name and name not in _BUILTIN_APP_MIDDLEWARE and not name.startswith("custom:"):
            import difflib as _df
            sug = _df.get_close_matches(name, _BUILTIN_APP_MIDDLEWARE, n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
            errors.append(
                f"middleware[{i}]: unknown app-level middleware '{name}'. "
                f"Built-in: {sorted(_BUILTIN_APP_MIDDLEWARE)}. Use "
                f"'custom:path.to.Class' for custom middleware.{hint}"
            )
    for mod_id, block in (getattr(getattr(definition, "tools", None), "modules", {}) or {}).items():
        mod_mw = list(getattr(block, "middleware", []) or [])
        for i, entry in enumerate(mod_mw):
            if not isinstance(entry, dict):
                continue
            name = entry.get("name", "")
            if name and name not in _BUILTIN_MODULE_MIDDLEWARE and not name.startswith("custom:"):
                import difflib as _df
                sug = _df.get_close_matches(name, _BUILTIN_MODULE_MIDDLEWARE, n=3, cutoff=0.6)
                hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
                errors.append(
                    f"modules.{mod_id}.middleware[{i}]: unknown module-level "
                    f"middleware '{name}'. Built-in: "
                    f"{sorted(_BUILTIN_MODULE_MIDDLEWARE)}. Use "
                    f"'custom:path.to.Class' for custom middleware.{hint}"
                )


def _collect_known_tools(definition: Any, registry: Any) -> set[str]:
    """Build the comprehensive set of tool identifiers an app exposes.

    Includes:
      - Every module id (so ``module_action.module: web`` validates)
      - For each declared module, every ``module.action`` FQN
      - The short and double-underscore variants of every action
      - Tools listed in ``capabilities.grant.actions`` (in case the
        registry isn't fully loaded in test mode)

    Tolerant: when the registry can't enumerate a module's actions
    (registry not loaded, MCP module discovered at runtime, ...), we
    skip silently rather than emitting a phantom error.
    """
    tools: set[str] = set()
    for mod_id in (getattr(getattr(definition, "tools", None), "modules", {}) or {}).keys():
        tools.add(mod_id)
        try:
            module = registry.get(mod_id)
            manifest = module.get_manifest()
            for action in manifest.action_names():
                tools.add(action)
                tools.add(f"{mod_id}.{action}")
                tools.add(f"{mod_id}__{action}")
        except Exception:
            pass

    caps = getattr(getattr(definition, "tools", None), "capabilities", None)
    if caps is not None:
        for grant in (getattr(caps, "grant", []) or []):
            mod_id = getattr(grant, "module", "")
            if mod_id:
                tools.add(mod_id)
            for action in (getattr(grant, "actions", []) or []):
                tools.add(action)
                if mod_id:
                    tools.add(f"{mod_id}.{action}")
                    tools.add(f"{mod_id}__{action}")
    return tools


def _validate_behavior_rule_triggers(
    definition: Any, known_tools: set[str], errors: list[str],
) -> None:
    """Strict FQN check on behavior custom rule triggers.

    Complements the lenient ``validate_behavior_config_structured`` which
    accepts a short-name fallback (``trigger: write`` matches any
    ``write`` action). When a trigger uses dotted FQN form
    (``module.action``), the exact FQN must exist - otherwise a typo on
    the module half slides through (e.g. ``filesytem.write`` would have
    matched ``write`` short and gone unnoticed).
    """
    if not known_tools:
        return
    behavior = getattr(getattr(definition, "security", None), "behavior", None)
    if behavior is None:
        return
    for i, rule in enumerate(getattr(behavior, "custom", []) or []):
        trig = getattr(rule, "trigger", "")
        if not trig or "." not in trig:
            continue
        if trig in known_tools:
            continue
        import difflib as _df
        sug = _df.get_close_matches(trig, known_tools, n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
        rid = getattr(rule, "id", f"#{i}")
        errors.append(
            f"behavior.custom[{rid}].trigger: '{trig}' is not a known tool "
            f"FQN. Use either an exact FQN (module.action) or a short "
            f"action name without the dot.{hint}"
        )


def _walk_hook_actions(hook: Any):
    """Yield every leaf action declared by a hook (resolves nested
    ``chain`` and ``transform_*`` wrappers). Always yields the action
    itself first so callers can inspect the wrapper's ``type``."""
    action = getattr(hook, "action", None)
    if action is None:
        return
    yield action
    action_dict = action.model_dump() if hasattr(action, "model_dump") else action
    if isinstance(action_dict, dict) and action_dict.get("type") == "chain":
        for sub in (action_dict.get("actions") or []):
            yield sub


def _validate_hook_action_consistency(
    definition: Any, warnings: list[str], errors: list[str],
) -> None:
    """Catch hook actions whose runtime requirements are not declared.

    Two checks today:

      - ``action.type == 'compact_context'`` only meaningful when the
        app has multiple turns. In ``mode: one_shot`` the hook fires
        once over a one-turn history, so the action does nothing.
        Emit a warning, NOT an error - the runtime is harmless.

      - ``action.type == 'shell'`` requires the ``shell`` module to be
        declared in the modules block (the action delegates to
        ``shell.bash`` for sandboxing). Without it, the hook silently
        no-ops at runtime. Emit a hard ERROR because that's a real
        configuration mistake, not a deferred warning.
    """
    exe = getattr(definition, "runtime", None)
    mode = getattr(exe, "mode", "conversation") if exe is not None else "conversation"
    tools = getattr(definition, "tools", None)
    declared_modules = set((getattr(tools, "modules", {}) or {}).keys()) if tools else set()

    def _check(actions_iter, ctx: str) -> None:
        for action in actions_iter:
            atype = ""
            if hasattr(action, "type"):
                atype = action.type
            elif isinstance(action, dict):
                atype = action.get("type", "")

            if atype == "compact_context" and mode == "one_shot":
                warnings.append(
                    f"{ctx}: action 'compact_context' is meaningless in "
                    f"mode='one_shot' (no turns to compact). Either change "
                    f"mode to 'conversation' or remove the hook."
                )
            elif atype == "shell" and "shell" not in declared_modules:
                errors.append(
                    f"{ctx}: action 'shell' requires the 'shell' module "
                    f"to be declared in the modules block. Without it the "
                    f"hook silently no-ops at runtime."
                )

    hooks_iter = list(getattr(exe, "hooks", []) or []) if exe is not None else []
    for i, hook in enumerate(hooks_iter):
        hid = getattr(hook, "id", f"#{i}")
        _check(_walk_hook_actions(hook), f"execution.hooks[{hid}].action")
    for ai, agent in enumerate(getattr(definition, "agents", []) or []):
        for hi, hook in enumerate(getattr(agent, "hooks", []) or []):
            hid = getattr(hook, "id", f"#{hi}")
            _check(_walk_hook_actions(hook), f"agents[{ai}].hooks[{hid}].action")


def _validate_hook_expressions(definition: Any, errors: list[str]) -> None:
    """Lint every ``condition.expr`` (and nested composite conditions).

    Catches syntactic errors at compile time so a typo in an expression
    can't slip into a deployed app and cause the runtime ``eval()`` to
    silently swallow the exception (returning False forever).

    Phase 9 upgrade: also validates that identifier paths reference
    one of ``HOOK_CONTEXT_ROOTS`` (the names actually exposed by
    ``hooks._eval_expression``)."""
    from digitorn.core.app.expressions import (
        HOOK_CONTEXT_ROOTS,
        validate_expression_against_context,
    )

    def _walk(cond: Any, ctx: str) -> None:
        if cond is None:
            return
        cond_dict = cond.model_dump() if hasattr(cond, "model_dump") else cond
        if not isinstance(cond_dict, dict):
            return
        ctype = cond_dict.get("type", "")
        if ctype in ("all_of", "any_of"):
            for j, sub in enumerate(cond_dict.get("conditions", []) or []):
                _walk(sub, f"{ctx}.conditions[{j}]")
            return
        if ctype == "not":
            sub = cond_dict.get("condition")
            if sub is not None:
                _walk(sub, f"{ctx}.condition")
            return
        if ctype == "expression":
            expr = cond_dict.get("expr", "")
            if expr or expr == "":
                errs = validate_expression_against_context(
                    expr, ctx=f"{ctx}.expr",
                    allowed_roots=HOOK_CONTEXT_ROOTS,
                )
                errors.extend(errs)

    exe = getattr(definition, "runtime", None)
    hooks_iter = list(getattr(exe, "hooks", []) or []) if exe is not None else []
    for i, hook in enumerate(hooks_iter):
        hid = getattr(hook, "id", f"#{i}")
        _walk(getattr(hook, "condition", None), f"execution.hooks[{hid}].condition")
    for ai, agent in enumerate(getattr(definition, "agents", []) or []):
        for hi, hook in enumerate(getattr(agent, "hooks", []) or []):
            hid = getattr(hook, "id", f"#{hi}")
            _walk(
                getattr(hook, "condition", None),
                f"agents[{ai}].hooks[{hid}].condition",
            )


def _validate_flow_expressions(definition: Any, errors: list[str]) -> None:
    """Lint flow ``routes[].when`` and ``decision.expr`` clauses.

    Two semantics for ``when:`` depending on the source node type:

      - On a ``decision`` node, ``when:`` matches the value produced by
        ``expr:``. So ``when: 'refund'`` is a literal string match
        against the expr result, NOT an expression to evaluate. We
        only require syntactic well-formedness here, no identifier
        check, because bare words are valid literals.

      - On every other node type (agent, tool, parallel, approval),
        ``when:`` is a boolean expression evaluated against the flow
        context. We enforce both syntactic correctness AND identifier
        roots from ``FLOW_CONTEXT_ROOTS``.

    Phase 9 upgrade over Phase 4: identifier path validation against
    the runtime context schema."""
    from digitorn.core.app.expressions import (
        FLOW_CONTEXT_ROOTS,
        validate_expression,
        validate_expression_against_context,
    )

    flow = getattr(definition, "flow", None)
    if flow is None:
        return
    for n in (getattr(flow, "nodes", []) or []):
        nid = getattr(n, "id", "?")
        ntype = getattr(n, "type", "")

        # decision-node routes: literal value matches against the
        # expr result. Only syntactic check needed.
        if ntype == "decision":
            for i, route in enumerate(getattr(n, "routes", []) or []):
                when = getattr(route, "when", "default")
                if not when or when == "default":
                    continue
                errs = validate_expression(
                    when, ctx=f"flow.nodes[{nid}].routes[{i}].when",
                )
                errors.extend(errs)
            # decision.expr IS evaluated against the context.
            expr = getattr(n, "expr", "")
            if expr:
                errs = validate_expression_against_context(
                    expr, ctx=f"flow.nodes[{nid}].expr",
                    allowed_roots=FLOW_CONTEXT_ROOTS,
                )
                errors.extend(errs)
            continue

        # Other nodes: when: is a boolean expression - strict.
        for i, route in enumerate(getattr(n, "routes", []) or []):
            when = getattr(route, "when", "default")
            if not when or when == "default":
                continue
            errs = validate_expression_against_context(
                when, ctx=f"flow.nodes[{nid}].routes[{i}].when",
                allowed_roots=FLOW_CONTEXT_ROOTS,
            )
            errors.extend(errs)
        if ntype == "parallel":
            for i, route in enumerate(getattr(n, "branches", []) or []):
                when = getattr(route, "when", "default")
                if not when or when == "default":
                    continue
                errs = validate_expression_against_context(
                    when, ctx=f"flow.nodes[{nid}].branches[{i}].when",
                    allowed_roots=FLOW_CONTEXT_ROOTS,
                )
                errors.extend(errs)


def _validate_hook_tool_refs(
    definition: Any, known_tools: set[str], errors: list[str],
) -> None:
    """Validate every ``tool_name`` condition in hooks against known tools.

    The condition is a regex (``match: "web.search|web.fetch"``). We try
    to match it against the known tools set: if at least one known tool
    matches, the regex is valid for this app. If none match, the user
    almost certainly typed a wrong name.

    Skipped when known_tools is empty (registry not loaded).
    """
    if not known_tools:
        return
    from fnmatch import fnmatchcase

    def _check_one_pattern(pattern: str, ctx: str) -> None:
        """The runtime uses fnmatch (glob), not regex. Match the same
        semantics here so the linter's verdict aligns with reality."""
        if not pattern:
            return
        # Glob has very few invalid forms; only an unclosed bracket
        # raises. Most "invalid" patterns are user errors against the
        # known-tools set.
        try:
            if any(fnmatchcase(t, pattern) for t in known_tools):
                return
        except Exception as exc:
            errors.append(
                f"{ctx}: tool_name match '{pattern}' is not a valid glob: {exc}"
            )
            return
        import difflib as _df
        sug = _df.get_close_matches(pattern, known_tools, n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
        errors.append(
            f"{ctx}: tool_name match '{pattern}' does not match any "
            f"known tool of this app.{hint}"
        )

    def _walk_conditions(cond: Any, ctx: str) -> None:
        if cond is None:
            return
        cond_dict = cond.model_dump() if hasattr(cond, "model_dump") else cond
        if not isinstance(cond_dict, dict):
            return
        ctype = cond_dict.get("type", "")
        if ctype in ("all_of", "any_of"):
            for j, sub in enumerate(cond_dict.get("conditions", []) or []):
                _walk_conditions(sub, f"{ctx}.conditions[{j}]")
            return
        if ctype == "not":
            sub = cond_dict.get("condition")
            if sub is not None:
                _walk_conditions(sub, f"{ctx}.condition")
            return
        if ctype == "tool_name":
            match = cond_dict.get("match", "")
            # Runtime accepts: a list, a pipe-separated string, or a
            # single pattern. Validate every individual entry.
            if isinstance(match, list):
                for j, p in enumerate(match):
                    _check_one_pattern(str(p), f"{ctx}.match[{j}]")
            elif isinstance(match, str):
                if "|" in match:
                    for j, p in enumerate(match.split("|")):
                        _check_one_pattern(p.strip(), f"{ctx}.match[{j}]")
                else:
                    _check_one_pattern(match, f"{ctx}.match")

    exe = getattr(definition, "runtime", None)
    hooks_iter = list(getattr(exe, "hooks", []) or []) if exe is not None else []
    for i, hook in enumerate(hooks_iter):
        cond = getattr(hook, "condition", None)
        hid = getattr(hook, "id", f"#{i}")
        _walk_conditions(cond, f"execution.hooks[{hid}].condition")
    for ai, agent in enumerate(getattr(definition, "agents", []) or []):
        for hi, hook in enumerate(getattr(agent, "hooks", []) or []):
            cond = getattr(hook, "condition", None)
            hid = getattr(hook, "id", f"#{hi}")
            _walk_conditions(
                cond, f"agents[{ai}].hooks[{hid}].condition",
            )


def _validate_mode_specific_fields(
    definition: Any, warnings: list[str], errors: list[str],
) -> None:
    """Discriminated mode gating: every ``execution.*`` field is checked
    against the declared mode. Splits into two severities:

      - **errors** (hard refuse): functional fields that the runtime
        actively branches on. Setting them outside their mode means
        the runtime will never honour them, which is always a bug.

          watchers, scheduler, session_mode (!= mono),
          max_sessions_per_user (!= 10), payload_schema
          (each only consumed in mode='background')

      - **warnings** (non-fatal smell): cosmetic fields the runtime
        silently ignores in the wrong mode. The user might leave them
        for documentation / future migration.

          greeting (only conversation), input/output customised
          (only one_shot)
    """
    exe = getattr(definition, "runtime", None)
    if exe is None:
        return
    mode = getattr(exe, "mode", "conversation")
    ui = getattr(definition, "ui", None)

    # ── Cosmetic mismatches: warn only ───────────────────────────
    if mode != "conversation":
        if getattr(ui, "greeting", ""):
            warnings.append(
                f"ui.greeting is only shown in 'conversation' mode "
                f"(current mode: {mode!r}). It will be ignored at runtime."
            )

    if mode != "one_shot":
        input_cfg = getattr(exe, "input", None)
        if input_cfg is not None:
            customised = (
                getattr(input_cfg, "type", "text") != "text"
                or list(getattr(input_cfg, "accept", []) or [])
                or getattr(input_cfg, "max_size", "")
                or getattr(input_cfg, "description", "")
            )
            if customised:
                warnings.append(
                    f"execution.input shapes the one_shot input contract "
                    f"(current mode: {mode!r}). It will be ignored unless "
                    f"you switch to mode: one_shot."
                )
        output_cfg = getattr(exe, "output", None)
        if output_cfg is not None:
            customised = (
                getattr(output_cfg, "type", "text") != "text"
                or getattr(output_cfg, "format", "")
                or getattr(output_cfg, "description", "")
                or dict(getattr(output_cfg, "schema_def", {}) or {})
            )
            if customised:
                warnings.append(
                    f"execution.output shapes the one_shot output contract "
                    f"(current mode: {mode!r}). It will be ignored unless "
                    f"you switch to mode: one_shot."
                )

    # ── Functional mismatches: hard error ───────────────────────
    if mode != "background":
        if getattr(exe, "session_mode", "mono") != "mono":
            errors.append(
                f"execution.session_mode='{exe.session_mode}' is only "
                f"consumed in mode='background' (current: {mode!r}). "
                f"Either switch the mode or remove session_mode."
            )
        if getattr(exe, "max_sessions_per_user", 10) != 10:
            errors.append(
                f"execution.max_sessions_per_user={exe.max_sessions_per_user} "
                f"is only consumed in mode='background' (current: {mode!r})."
            )
        if getattr(exe, "watchers", False):
            errors.append(
                f"execution.watchers=true requires mode='background' "
                f"(current: {mode!r}). The watcher scheduler will not start."
            )
        if getattr(exe, "scheduler", False):
            errors.append(
                f"execution.scheduler=true requires mode='background' "
                f"(current: {mode!r})."
            )
        if getattr(exe, "payload_schema", None) is not None:
            errors.append(
                f"execution.payload_schema is only consumed in "
                f"mode='background' (current: {mode!r}). Remove it or "
                f"switch the mode."
            )


def _validate_credential_refs(definition: Any, errors: list[str]) -> None:
    """Cross-check that every ``credential:`` reference points at a provider
    declared in ``execution.credentials_schema.providers``.

    Skipped entirely when no credentials_schema is declared - in that case
    refs are treated as opaque vault keys (current behaviour). When the
    schema IS declared, references that don't match must fail compilation
    so the runtime never tries to resolve a non-existent credential.
    """
    sec = getattr(definition, "security", None)
    schema = getattr(sec, "credentials_schema", None) if sec is not None else None
    if schema is None:
        return
    declared: set[str] = set()
    for prov in (getattr(schema, "providers", []) or []):
        name = getattr(prov, "name", "") or ""
        if name:
            declared.add(name)
    if not declared:
        return

    def _ref_name(value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return value.get("ref")
        return None

    def _check(ref: Any, ctx: str) -> None:
        name = _ref_name(ref)
        if not name or name in declared:
            return
        import difflib as _df
        sug = _df.get_close_matches(name, declared, n=3, cutoff=0.6)
        hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
        errors.append(
            f"{ctx}: credential ref '{name}' is not declared in "
            f"execution.credentials_schema.providers. Declared: "
            f"{sorted(declared)}.{hint}"
        )

    for i, agent in enumerate(getattr(definition, "agents", []) or []):
        brain = getattr(agent, "brain", None)
        if brain is None:
            continue
        _check(getattr(brain, "credential", None), f"agents[{i}].brain.credential")
        fallback = getattr(brain, "fallback", None)
        if fallback is not None:
            _check(getattr(fallback, "credential", None), f"agents[{i}].brain.fallback.credential")

    for mod_id, block in (getattr(getattr(definition, "tools", None), "modules", {}) or {}).items():
        _check(getattr(block, "credential", None), f"modules.{mod_id}.credential")


def _validate_plugin_params(
    errors: list[str],
    ctx: str,
    plugin_name: str,
    supplied: dict[str, Any],
    schema: dict[str, str] | None,
) -> None:
    """Check hook condition/action params against a declared schema.

    schema is ``{param_name: "required" | "optional"}``. ``None`` means
    no schema declared - no validation performed. When a schema is
    declared, unknown params and missing required params both error.
    Closest-match suggestion is included for unknown params to catch
    typos like ``value`` vs ``match``.
    """
    if schema is None:
        return
    known = set(schema.keys())
    unknown = [k for k in supplied.keys() if k not in known]
    if unknown:
        import difflib
        for bad in unknown:
            hints = difflib.get_close_matches(bad, known, n=2, cutoff=0.5)
            hint = f" Did you mean: {', '.join(hints)}?" if hints else ""
            errors.append(
                f"{ctx}: Unknown param '{bad}' for '{plugin_name}'. "
                f"Known params: {sorted(known)}.{hint}"
            )
    missing = [k for k, kind in schema.items() if kind == "required" and k not in supplied]
    if missing:
        errors.append(
            f"{ctx}: Missing required param(s) for '{plugin_name}': {sorted(missing)}"
        )


@dataclass
class CompiledSetupStep:
    """A single validated, resolved setup action ready for execution."""

    module_id: str
    action: str
    resolved_params: dict[str, Any]
    params_model: type | None = None


@dataclass
class CompiledModuleConfig:
    """Fully validated module configuration from the app YAML."""

    module_id: str
    config: dict[str, Any] = field(default_factory=dict)
    setup_steps: list[CompiledSetupStep] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    middleware: list[dict[str, Any]] = field(default_factory=list)
    # Raw `credential:` ref from the YAML block (str compact form OR
    # `{ref, scope, provider}` mapping). Resolved at deploy time
    # (system_wide / per_app_shared) and at session-start (per_user /
    # per_app_per_user). None when the block does not bind a vault
    # credential.
    credential: Any = None


@dataclass
class CompiledBrain:
    """Compiled brain configuration for an agent.

    Either references a named provider (provider_id) or contains
    a fully resolved inline config that will be registered as a
    provider at bootstrap time.
    """

    provider_id: str
    is_inline: bool = False
    inline_config: dict[str, Any] = field(default_factory=dict)
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    timeout: float | None = None
    native_tool_use: bool | None = None
    context: CompiledContextConfig | None = None
    fallback: "CompiledBrain | None" = None
    # Raw `credential:` ref from the brain block (see
    # `CompiledModuleConfig.credential`). Same lifecycle.
    credential: Any = None


@dataclass
class CompiledAgent:
    """Compiled agent definition ready for runtime instantiation."""

    agent_id: str
    role: str
    brain: CompiledBrain
    system_prompt: str = ""
    plan_first: bool = True
    specialty: str = ""
    skills_content: str = ""
    modules: list[str] = field(default_factory=list)
    pool_max_workers: int = 3
    pool_progress: bool = False
    pool_auto_retry: int = 0
    # Per-agent hooks - each CompiledHook in this list has agent_id set
    # to this agent's id so the runtime filter fires them only for the
    # matching agent's turns. Empty list = no per-agent hooks.
    hooks: list["CompiledHook"] = field(default_factory=list)


@dataclass
class CompiledInput:
    """Compiled input contract for one_shot mode."""

    type: str = "text"
    accept: list[str] = field(default_factory=list)
    max_size: str = ""
    description: str = ""
    required: bool = True


@dataclass
class CompiledOutput:
    """Compiled output contract for one_shot mode."""

    type: str = "text"
    format: str = ""
    description: str = ""
    schema_def: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompiledTrigger:
    """Compiled trigger for background mode."""

    id: str
    type: str
    schedule: str = ""
    paths: list[str] = field(default_factory=list)
    path: str = ""
    method: str = "POST"
    port: int = 9100
    message: str = ""
    routing: str = "broadcast"
    routing_key: str = ""


@dataclass
class CompiledContextConfig:
    """Compiled context management configuration."""

    max_tokens: int = 0
    output_reserved: int = 4096
    strategy: str = "summarize"
    keep_recent: int = 10
    compression_trigger: float = 0.75
    summary_max_tokens: int = 1024
    auto_compact: bool = True
    summary_brain: Any = None


@dataclass
class CompiledHook:
    """A compiled internal hook ready for runtime."""

    id: str
    on: str = "turn_end"
    condition_type: str = "always"
    condition_params: dict[str, Any] = field(default_factory=dict)
    action_type: str = "log"
    action_params: dict[str, Any] = field(default_factory=dict)
    cooldown: float = 0.0
    max_fires: int = 0
    priority: int = 100
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    # Optional scope - when set, this hook only fires for the named
    # agent (sub-agent specialisation). ``None`` = app-wide.
    agent_id: str | None = None
    # Hard wall on action runtime (seconds). Cancels the action if
    # exceeded. Default 30s = enough for compaction; lower in YAML
    # for cheap hooks.
    timeout: float = 30.0


@dataclass
class CompiledExecution:
    """Compiled execution configuration."""

    mode: str = "conversation"
    entry_agent: str = ""
    max_turns: int = 50
    timeout: float = 300.0
    workspace: str = ""
    workspace_mode: str = "auto"
    input: CompiledInput = field(default_factory=CompiledInput)
    output: CompiledOutput = field(default_factory=CompiledOutput)
    greeting: str = ""
    triggers: list[CompiledTrigger] = field(default_factory=list)
    context: CompiledContextConfig = field(default_factory=CompiledContextConfig)
    hooks: list[CompiledHook] = field(default_factory=list)
    watchers: bool = False
    scheduler: bool = False
    tool_injection: str | None = None
    project_memory: str = "auto"
    direct_modules: list[str] = field(default_factory=list)
    default_channel: str = "llm_notification"
    session_mode: str = "mono"
    max_sessions_per_user: int = 10
    max_concurrent_activations: int = 20
    # Optional declarative payload schema, normalised to a plain dict
    # so the API can ship it to the Flutter dashboard verbatim and the
    # validator can read it without a Pydantic dependency. ``None`` =
    # no schema declared (legacy / free-form payloads).
    payload_schema: dict[str, Any] | None = None
    # Optional declarative credentials schema. Declares external
    # services (API keys, OAuth providers, MCP servers, DB
    # connections) the app needs. Same normalisation as above.
    credentials_schema: dict[str, Any] | None = None


@dataclass
class CompiledChannelInstance:
    """A compiled channel instance ready for runtime instantiation."""

    instance_name: str
    channel_type: str
    config: dict[str, Any] = field(default_factory=dict)
    user_resolver: dict[str, Any] | None = None


@dataclass
class CompiledApp:
    """The fully validated, resolved app definition ready for bootstrapping."""

    meta: AppMeta
    modules: dict[str, CompiledModuleConfig] = field(default_factory=dict)
    channels: dict[str, CompiledChannelInstance] = field(default_factory=dict)
    agents: list[CompiledAgent] = field(default_factory=list)
    execution: CompiledExecution = field(default_factory=CompiledExecution)
    security_profile: SecurityProfile | None = None
    source_path: Path | None = None
    middleware: list[dict[str, Any]] = field(default_factory=list)
    skills: list[dict[str, str]] = field(default_factory=list)
    hidden_actions: list[dict[str, Any]] = field(default_factory=list)
    behavior: Any = None  # BehaviorConfig from the YAML - passed to bootstrap for wiring

    # Every external file the compiler read while producing this
    # CompiledApp, keyed by its path relative to the YAML source dir.
    # This is the raw material the AppSyncer freezes into an AppBundle
    # so the daemon can reload the app without going back to the source
    # filesystem. Keys are always forward-slash relative paths.
    collected_assets: dict[str, str] = field(default_factory=dict)

    # The raw YAML text used to produce this compiled app. Always
    # populated - by ``compile_file`` from the file bytes and by
    # ``compile_string`` from its content argument. The AppSyncer uses
    # this as the bundle's ``app.yaml`` payload instead of re-reading
    # ``source_path`` from disk, which can be missing, moved, or replaced
    # by the time the sync runs.
    raw_yaml: str = ""

    # Optional workspace block carried through from the YAML root
    # ``workspace:`` block. Tells the client this app uses a virtual
    # file workspace (render_mode, entry_file, title). The daemon emits
    # the metadata via preview:state_changed on the first file write.
    workspace: Any = None  # WorkspaceBlock | None

    # Optional declarative widgets tree carried through from the YAML
    # root ``widgets:`` block. The compiler validates the tree at
    # deploy time; the daemon serves it via /api/apps/{id}/widgets/*
    # and the agent can push live render/update events via the
    # ``widget`` module's actions.
    widgets: Any = None  # WidgetsConfig | None

    # ── Client manifest extensions ────────────────────────────────
    # Opaque pass-through blocks read only by the Flutter/web client to
    # customise its UI. The daemon does not interpret their values; it
    # simply parses and exposes them via DeployedApp.summary() so the
    # client can read them from GET /api/apps/{id}.
    features: dict[str, bool] = field(default_factory=dict)
    theme: dict[str, str] = field(default_factory=dict)
    slash_commands: list[dict[str, str]] = field(default_factory=list)

    # Optional declarative orchestration graph carried through from the
    # YAML root ``flow:`` block. The compiler validates every cross-ref
    # at deploy time; the runtime drives the agents along this graph
    # when present, the canvas renders it as a flowchart.
    flow: Any = None  # FlowConfig | None

    # Non-fatal warnings emitted during compilation. Surfaced to clients
    # (CLI, Builder canvas, Web validator) so the user sees configuration
    # smells the compiler accepts but probably did not intend - e.g.
    # ``triggers`` declared with ``mode: conversation`` (the trigger will
    # never fire), ``compact_context`` hooks with ``mode: one_shot``
    # (nothing to compact across a single turn).
    warnings: list[str] = field(default_factory=list)

    @property
    def app_id(self) -> str:
        return self.meta.app_id

    @property
    def module_ids(self) -> list[str]:
        return list(self.modules.keys())

    @property
    def agent_ids(self) -> list[str]:
        return [a.agent_id for a in self.agents]


_UNIVERSAL_CONSTRAINT_KEYS = frozenset({"allowed_actions", "blocked_actions"})
_CONSTRAINT_SIZE_RE = _re.compile(r'^\d+\s*(?:B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$', _re.IGNORECASE)
_CONSTRAINT_DURATION_RE = _re.compile(r'^\d+\s*(?:ms|s|m|h|d)$', _re.IGNORECASE)


def _validate_prompt_metadata(
    metadata: dict[str, dict[str, Any]],
    *,
    declared_variables: set[str],
    errors: list[str],
) -> None:
    """Validate YAML frontmatter found in prompt/skill files.

    Runs after variable resolution so every prompt has been read.
    Checks:

    - ``variables_required`` lists - each must be declared in the
      app's ``variables:`` block
    - ``max_tokens_estimate`` - warn-level, adds an informational
      error (compiler still succeeds) if the estimate exceeds a
      hard cap of 200k (above which no model can accept it)
    - ``min_model`` - informational, no enforcement in v1

    Errors are appended to the ``errors`` list - the compiler
    raises ``AppCompilationError`` after this pass.
    """
    for full_key, fm in metadata.items():
        required = fm.get("variables_required") or []
        if isinstance(required, list):
            for var in required:
                if var not in declared_variables and not var.startswith((
                    "app.", "sys.", "env.", "secret.",
                )):
                    errors.append(
                        f"{full_key}: frontmatter declares "
                        f"variables_required={var!r} but it is not "
                        f"defined in the app's variables: block"
                    )
        estimate = fm.get("max_tokens_estimate")
        if isinstance(estimate, (int, float)) and estimate > 200_000:
            errors.append(
                f"{full_key}: max_tokens_estimate={estimate} exceeds "
                f"200k - no model can accept a prompt that large"
            )


def _compile_brain_context(brain: Any) -> CompiledContextConfig | None:
    """Compile per-brain context config. Returns None if not set."""
    if brain.context is None:
        return None
    ctx = brain.context
    return CompiledContextConfig(
        max_tokens=ctx.max_tokens,
        output_reserved=ctx.output_reserved,
        strategy=ctx.strategy,
        keep_recent=ctx.keep_recent,
        compression_trigger=ctx.compression_trigger,
        summary_max_tokens=ctx.summary_max_tokens,
        auto_compact=ctx.auto_compact,
        summary_brain=ctx.summary_brain,
    )


class AppYAMLCompiler:
    """Stateless compiler: YAML → CompiledApp.

    Usage::

        compiler = AppYAMLCompiler(registry)
        compiled = compiler.compile_file(Path("my-app.yaml"))
        compiled = compiler.compile({"app": {...}, "modules": {...}})
    """

    def __init__(self, registry: "ModuleRegistry") -> None:
        self._registry = registry
        self._secrets: dict[str, str] | None = None
        self._source_dir: Path | None = None
        self._source_file: Path | None = None
        self._positions: PositionMap = {}
        self._source_name: str = ""
        self._asset_loader: Any = None
        self._collected_assets: dict[str, str] = {}
        # Non-fatal warnings collected during a single compile() call.
        # Reset at the start of compile(), bubbled into CompiledApp.warnings
        # at the end. Surfaced to the user (CLI, Builder canvas, web
        # validator) without aborting the build.
        self._warnings: list[str] = []

    # ── External-file loading ───────────────────────────────────────────

    def _load_external_text(
        self, path_str: str, *, label: str,
    ) -> tuple[str, str]:
        """Resolve an external file referenced by the YAML.

        Returns ``(normalised_relpath, content)``. The normalised rel
        path is always forward-slash relative to the source dir (or just
        the original string if it was absolute and couldn't be made
        relative).

        Resolution order:
          1. If an ``_asset_loader`` is set (bundle-reload mode), delegate
             entirely to it - the source filesystem is NOT touched.
          2. Otherwise read from ``_source_dir / path_str`` on disk. The
             resulting content is stored in ``_collected_assets`` so the
             AppSyncer can freeze it into a bundle on the next deploy.

        Raises ``FileNotFoundError`` if the asset cannot be resolved, so
        the caller can produce a precise error message (e.g. "skills: ...").
        """
        # Normalise to a forward-slash relative path for bundle storage.
        rel_path = path_str.replace("\\", "/").strip()
        while rel_path.startswith("./"):
            rel_path = rel_path[2:]

        if self._asset_loader is not None:
            content = self._asset_loader(rel_path)
            if content is None:
                raise FileNotFoundError(
                    f"{label}: asset not found in bundle: {rel_path}"
                )
            self._collected_assets[rel_path] = content
            return rel_path, content

        path = Path(path_str)
        if not path.is_absolute() and self._source_dir is not None:
            path = self._source_dir / path
        if not path.is_file():
            raise FileNotFoundError(f"{label}: file not found: {path}")
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise FileNotFoundError(f"{label}: cannot read '{path}': {exc}")

        # Re-derive the rel path now that we know the real on-disk
        # location - this handles edge cases where the caller passed an
        # absolute path that happens to live under the source dir.
        if self._source_dir is not None:
            try:
                rel_path = path.resolve().relative_to(
                    self._source_dir.resolve()
                ).as_posix()
            except ValueError:
                # Not under source_dir (e.g. absolute path elsewhere).
                # Keep the original normalised rel_path so the bundle
                # records something sensible.
                pass

        self._collected_assets[rel_path] = content
        return rel_path, content

    # ── Entry points ────────────────────────────────────────────────────

    def compile_file(
        self, path: Path, *, secrets: dict[str, str] | None = None
    ) -> CompiledApp:
        """Load a YAML file and compile it."""
        self._secrets = secrets
        self._asset_loader = None
        self._collected_assets = {}
        path = Path(path)
        if not path.exists():
            raise AppCompilationError(
                [f"File not found: {path}"], source=str(path)
            )
        raw_text = path.read_text(encoding="utf-8")
        source_name = path.name
        try:
            raw, positions = load_with_positions(raw_text, source=source_name)
        except yaml.YAMLError as exc:
            loc = ""
            if hasattr(exc, "problem_mark") and exc.problem_mark is not None:
                loc = f"{source_name}:{exc.problem_mark.line + 1}:{exc.problem_mark.column + 1}"
            msg = f"YAML parse error: {exc}"
            raise AppCompilationError(
                [f"{loc}: {msg}" if loc else msg], source=str(path),
            ) from exc

        if not isinstance(raw, dict):
            raise AppCompilationError(
                [f"{source_name}:1:1: YAML root must be a mapping"],
                source=str(path),
            )
        self._source_dir = path.parent
        self._source_file = path
        self._positions = positions
        self._source_name = source_name
        try:
            compiled = self.compile(raw)
            compiled.source_path = path
            compiled.raw_yaml = raw_text
            compiled.collected_assets = dict(self._collected_assets)
            return compiled
        finally:
            self._secrets = None
            self._source_dir = None
            self._source_file = None
            self._positions = {}
            self._source_name = ""
            self._collected_assets = {}

    def compile_string(
        self,
        content: str,
        *,
        source: str = "<string>",
        secrets: dict[str, str] | None = None,
        asset_loader: Any = None,
    ) -> CompiledApp:
        """Compile a YAML string into a CompiledApp.

        Two modes:

        - Default: relative paths in the YAML (skills/, agent prompt
          files, …) are resolved against ``source``'s parent directory on
          the real filesystem.
        - Bundle mode: pass an ``asset_loader`` callable - a function that
          takes a forward-slash relative path and returns its content
          (or None). The compiler uses that instead of reading from disk,
          so reloading an app from an AppBundle never touches the
          original source tree.
        """
        self._secrets = secrets
        self._asset_loader = asset_loader
        self._collected_assets = {}
        # Resolve _source_dir from source path so relative skill/agent
        # paths work even when recompiling from DB-stored YAML content.
        source_path = Path(source) if source and source != "<string>" else None
        if source_path is not None and source_path.is_file():
            self._source_dir = source_path.parent
        elif source_path is not None and source_path.parent.is_dir():
            self._source_dir = source_path.parent
        else:
            self._source_dir = None

        source_name = Path(source).name if source and source != "<string>" else source
        try:
            raw, positions = load_with_positions(content, source=source_name)
        except yaml.YAMLError as exc:
            loc = ""
            if hasattr(exc, "problem_mark") and exc.problem_mark is not None:
                loc = f"{source_name}:{exc.problem_mark.line + 1}:{exc.problem_mark.column + 1}"
            msg = f"YAML parse error: {exc}"
            raise AppCompilationError(
                [f"{loc}: {msg}" if loc else msg], source=source,
            ) from exc

        if not isinstance(raw, dict):
            raise AppCompilationError(
                [f"{source_name}:1:1: YAML root must be a mapping"],
                source=source,
            )
        self._positions = positions
        self._source_name = source_name
        try:
            compiled = self.compile(raw)
            compiled.raw_yaml = content
            compiled.collected_assets = dict(self._collected_assets)
            return compiled
        finally:
            self._secrets = None
            self._source_dir = None
            self._asset_loader = None
            self._positions = {}
            self._source_name = ""
            self._collected_assets = {}

    def compile(self, raw: dict[str, Any]) -> CompiledApp:
        """Compile a raw dict (parsed YAML) into a CompiledApp.

        Collects all errors and raises a single ``AppCompilationError``
        with the full list.
        """
        errors: list[str] = []
        # Fresh warnings list per compile call. Bubbled into CompiledApp.
        self._warnings = []

        # Apply top-level schema aliases first: rewrite ``runtime:``
        # -> ``execution:``, ``ui:`` -> top-level fields, ``dependencies:``
        # -> legacy locations. This lets new YAMLs use the cleaner
        # shape without breaking the legacy AppDefinition schema.
        # Deprecation warnings (app.features / app.theme) flow into
        # CompiledApp.warnings via self._warnings.
        if isinstance(raw, dict):
            from digitorn.core.app.schema_aliases import apply_schema_aliases
            raw = apply_schema_aliases(
                raw, deprecation_warnings=self._warnings,
            )

        # Apply fragmentation: auto-load ./agents, ./hooks and any
        # explicit include: block. Two modes:
        #   - source-tree: we have a real filesystem path (compile_file).
        #     Fragments are read from disk AND copied into
        #     `_collected_assets` so the bundle stores them.
        #   - bundle-reload: we have an `_asset_loader`. Fragments are
        #     read through it, no filesystem access.
        if isinstance(raw, dict) and (
            self._source_dir is not None or self._asset_loader is not None
        ):
            from digitorn.core.app.include_loader import apply_includes

            if self._asset_loader is not None and self._source_dir is None:
                # Bundle mode: build a list_dir over the loader's known
                # asset keys. The bundle store exposes a side-channel
                # via ``list_dir`` when present; we degrade gracefully
                # if it's missing (no fragments seen on reload).
                _list_dir = getattr(self._asset_loader, "list_dir", None)
                if _list_dir is None:
                    _list_dir = lambda _rel: []  # bundle has no fragments
                raw, _include_errors = apply_includes(
                    raw, None,
                    asset_loader=self._asset_loader,
                    list_dir=_list_dir,
                )
            else:
                raw, _include_errors = apply_includes(
                    raw, self._source_dir,
                    collected_assets=self._collected_assets,
                )

            if _include_errors:
                raise AppCompilationError(_include_errors)

        # Pre-flight: catch the most common LLM hallucinations and
        # return a clear "did you mean ..." message BEFORE the Pydantic
        # validator drowns the user in low-level errors. BUG-040 found
        # the builder writing `name:` at root, `modules` as a list,
        # `agent.model:` instead of `brain:` etc. - these used to come
        # out as a cryptic 30-error pydantic dump; now we short-circuit.
        if isinstance(raw, dict):
            pre_errors: list[str] = []
            if "name" in raw and not isinstance(raw.get("app"), dict):
                pre_errors.append(
                    "top-level `name:` is not a valid root key. Put it "
                    "under `app.name:` (with `app.app_id` required)."
                )
            mods = raw.get("modules")
            if isinstance(mods, list):
                pre_errors.append(
                    "`modules:` must be a DICT keyed by module_id (e.g. "
                    "`modules: {memory: {}, filesystem: {}}`), not a list."
                )
            agents = raw.get("agents")
            if isinstance(agents, dict):
                pre_errors.append(
                    "`agents:` must be a LIST of {id, role, brain, ...} "
                    "objects, not a dict."
                )
            if isinstance(agents, list):
                for i, a in enumerate(agents):
                    if isinstance(a, dict):
                        if "model" in a and "brain" not in a:
                            pre_errors.append(
                                f"agents[{i}]: `model:` at agent level is invalid. "
                                f"Wrap it in `brain: {{provider: ..., model: ...}}`."
                            )
                        if "type" in a and not a.get("role"):
                            pre_errors.append(
                                f"agents[{i}]: `type:` is not a valid field. "
                                f"Use `role:` (coordinator/specialist/assistant)."
                            )
            app_block = raw.get("app")
            if isinstance(app_block, dict):
                if "type" in app_block and "entrypoint" in app_block:
                    pre_errors.append(
                        "app.type + app.entrypoint are invalid. Use "
                        "`execution.mode:` (conversation|one_shot|background) "
                        "and `execution.entry_agent:`."
                    )
            caps = raw.get("capabilities")
            if isinstance(caps, dict):
                grant = caps.get("grant")
                if (isinstance(grant, list) and grant
                        and all(isinstance(g, str) for g in grant)):
                    pre_errors.append(
                        "`capabilities.grant:` must be a list of "
                        "`{module, actions}` objects, not plain strings."
                    )
            if pre_errors:
                raise AppCompilationError([
                    f"<root>: {e}" for e in pre_errors
                ])

        try:
            definition = AppDefinition.model_validate(raw)
        except ValidationError as exc:
            for e in exc.errors():
                path = pydantic_loc_to_path(e.get("loc", ()))
                loc = format_location(self._positions, path, self._source_name)
                field_path = ".".join(str(p) for p in path) or "<root>"
                errors.append(
                    f"{loc}: schema error at '{field_path}': {e.get('msg', '')}"
                )
            raise AppCompilationError(errors)

        # Inject app.* variables so {{app.id}}, {{app.name}} etc. work
        from digitorn.core.app.variables import (
            inject_app_variables,
            bundle_context,
            collected_prompt_metadata,
        )
        inject_app_variables(definition.dev.variables, definition.app)

        # Filesystem namespaces ({{prompt.X}} / {{skill.X}} /
        # {{asset.X}}) need the bundle dir + app_id. We set both
        # once here via a context manager so every downstream
        # resolve_variables() call in this compile picks them up.
        _bundle_cm = bundle_context(
            bundle_dir=self._source_dir,
            app_id=getattr(definition.app, "app_id", "") or "",
        )
        _bundle_cm.__enter__()
        try:
            compiled = self._compile_body(raw, definition, errors)
            _validate_prompt_metadata(
                collected_prompt_metadata(),
                declared_variables=set(definition.dev.variables.keys()),
                errors=errors,
            )
            _validate_placeholder_references(raw, definition, errors)
            _validate_dependency_graph(definition, errors)
            _validate_credential_refs(definition, errors)
            _known_tools = _collect_known_tools(definition, self._registry)
            _validate_hook_tool_refs(definition, _known_tools, errors)
            _validate_behavior_rule_triggers(definition, _known_tools, errors)
            _validate_hook_expressions(definition, errors)
            _validate_flow_expressions(definition, errors)
            _validate_hook_action_consistency(
                definition, self._warnings, errors,
            )
            _validate_mode_specific_fields(definition, self._warnings, errors)
            if definition.flow is not None:
                from digitorn.core.app.flow import validate_flow_references
                _agent_ids = {a.id for a in (definition.agents or [])}
                validate_flow_references(
                    definition.flow,
                    declared_agents=_agent_ids,
                    known_tools=_known_tools,
                    errors=errors,
                )

            # Behavior engine validator: deep static check catches
            # every runtime bug at deploy time - typos in enum values,
            # bad regex, undefined sets/counters/flags, unknown condition
            # keys, placeholder references to ghost names, trigger names
            # that aren't in granted capabilities.
            if definition.security.behavior:
                try:
                    from digitorn.modules.behavior.validator import (
                        validate_behavior_config_structured,
                    )
                    from digitorn.modules.behavior.source_map import (
                        load_with_source,
                        overlay_included_file,
                    )
                    bh_dict = definition.security.behavior.model_dump() if hasattr(
                        definition.security.behavior, "model_dump"
                    ) else dict(definition.security.behavior)

                    # Collect tool names known to this app via the
                    # comprehensive helper (modules, FQN actions, short
                    # names). Reuses _known_tools computed above when
                    # available.
                    known_tools: set[str] = set(_known_tools) if _known_tools else _collect_known_tools(definition, self._registry)
                    if definition.tools.capabilities is not None:
                        for grant in (definition.tools.capabilities.grant or []):
                            module_id = grant.module
                            known_tools.add(module_id)
                            for action in (grant.actions or []):
                                known_tools.add(action)
                                known_tools.add(f"{module_id}.{action}")
                                known_tools.add(f"{module_id}__{action}")

                    # Build source map for line numbers (main file + overlay
                    # any included ./behavior/*.yaml)
                    src_map = None
                    if self._source_file:
                        try:
                            _, src_map = load_with_source(self._source_file)
                            if src_map and self._source_dir:
                                behavior_dir = self._source_dir / "behavior"
                                if behavior_dir.is_dir():
                                    for yf in behavior_dir.glob("*.yaml"):
                                        overlay_included_file(
                                            src_map, yf,
                                            at_parent_path=f"behavior.{yf.stem}",
                                        )
                        except Exception:
                            src_map = None

                    bh_errors = validate_behavior_config_structured(
                        bh_dict,
                        known_tools=known_tools if known_tools else None,
                    )

                    for e in bh_errors:
                        location = ""
                        if src_map:
                            full_path = f"behavior.{e.path}"
                            file, line, _col = src_map.get(full_path)
                            if file and line:
                                try:
                                    rel = Path(file).relative_to(
                                        self._source_dir
                                    ) if self._source_dir else Path(file).name
                                    location = f"{rel}:{line}: "
                                except (ValueError, TypeError):
                                    location = f"{Path(file).name}:{line}: "
                        errors.append(f"behavior: {location}{e}")
                except ImportError:
                    pass

            if errors:
                errors = [
                    self._annotate_error_with_location(e) for e in errors
                ]
                raise AppCompilationError(errors)
            # Refresh warnings: validations that run AFTER `_compile_body`
            # (the dependency graph pass, expression linter, action
            # consistency) append to ``self._warnings`` after the
            # CompiledApp was built. Sync the list back here so callers
            # see the full picture.
            compiled.warnings = list(self._warnings)
            return compiled
        finally:
            _bundle_cm.__exit__(None, None, None)

    def _annotate_error_with_location(self, err: str) -> str:
        """Prepend ``file:line:col:`` to a compiler error by parsing its
        ``ctx:`` prefix and looking up the YAML position.

        Works with errors like ``"execution.hooks[0].condition.type: ..."``
        by converting the ctx to a tuple path and querying the position
        map. Leaves already-positioned errors alone (heuristic: error
        already starts with ``<name>:<digit>``).
        """
        import re as _re2
        if _re2.match(r"^[^:\s]+:\d+:\d+:", err):
            return err
        m = _re2.match(r"^([a-zA-Z_][\w.\[\]]*?)(?::|\s)", err)
        if not m:
            return err if self._source_name == "" else f"{self._source_name}: {err}"
        ctx_str = m.group(1)
        tokens: list = []
        for part in _re2.split(r"[\.]", ctx_str):
            while part:
                if part.startswith("["):
                    idx_end = part.find("]")
                    if idx_end == -1:
                        break
                    try:
                        tokens.append(int(part[1:idx_end]))
                    except ValueError:
                        tokens.append(part[1:idx_end])
                    part = part[idx_end + 1:]
                else:
                    key_end = part.find("[")
                    if key_end == -1:
                        if part:
                            tokens.append(part)
                        part = ""
                    else:
                        if part[:key_end]:
                            tokens.append(part[:key_end])
                        part = part[key_end:]
        loc = format_location(self._positions, tuple(tokens), self._source_name)
        return f"{loc}: {err}"

    def _compile_body(
        self,
        raw: dict[str, Any],
        definition: Any,
        errors: list[str],
    ) -> CompiledApp:
        """Body of ``compile`` extracted so the bundle_context manager
        can wrap it cleanly without indenting 400+ lines."""
        resolved_modules: dict[str, dict[str, Any]] = {}
        for module_id, block in definition.tools.modules.items():
            try:
                # For the channels module: activation blocks inside provider
                # configs contain runtime templates (e.g. {{config_data}},
                # {{client}}) that are resolved by the channels pipeline at
                # execution time, not by the compiler.  Temporarily remove
                # them before variable resolution and restore afterwards.
                saved_activations: dict[str, Any] = {}
                if module_id == "channels" and "providers" in block.config:
                    for prov_id, prov_cfg in block.config.get("providers", {}).items():
                        if isinstance(prov_cfg, dict) and "activation" in prov_cfg:
                            saved_activations[prov_id] = prov_cfg.pop("activation")

                resolved_config = resolve_variables(
                    block.config, definition.dev.variables,
                    secrets=self._secrets,
                )

                # Restore activation blocks (unresolved - they are runtime templates)
                for prov_id, activation in saved_activations.items():
                    if prov_id in resolved_config.get("providers", {}):
                        resolved_config["providers"][prov_id]["activation"] = activation

                resolved_setup = []
                for step in block.setup:
                    resolved_params = resolve_variables(
                        step.params, definition.dev.variables,
                        secrets=self._secrets,
                    )
                    resolved_setup.append(
                        {"action": step.action, "params": resolved_params}
                    )
                resolved_constraints = resolve_variables(
                    block.constraints, definition.dev.variables,
                    secrets=self._secrets,
                )
                resolved_modules[module_id] = {
                    "config": resolved_config,
                    "setup": resolved_setup,
                    "constraints": resolved_constraints,
                }
            except ValueError as exc:
                errors.append(f"modules.{module_id}: {exc}")

        if errors:
            raise VariableResolutionError(errors)

        available = set(self._registry.list_available())
        missing_modules = []
        for module_id in definition.tools.modules:
            if module_id not in available:
                missing_modules.append(
                    f"Module '{module_id}' is not loaded "
                    f"(available: {sorted(available)})"
                )
        if missing_modules:
            raise ModuleNotFoundError(missing_modules)

        compiled_modules: dict[str, CompiledModuleConfig] = {}

        for module_id, resolved in resolved_modules.items():
            module = self._registry.get(module_id)
            manifest = module.get_manifest()
            action_names = set(manifest.action_names())
            action_registry = getattr(module, "_action_registry", {})

            resolved_config = resolved.get("config", {})
            config_model = getattr(module, "CONFIG_MODEL", None)
            if resolved_config and config_model is not None:
                try:
                    config_model.model_validate(resolved_config)
                except ValidationError as exc:
                    for e in exc.errors():
                        loc = ".".join(str(x) for x in e["loc"])
                        suggestions = ""
                        if e.get("type") == "extra_forbidden":
                            try:
                                import difflib as _df
                                known = set(config_model.model_fields.keys())
                                bad = e.get("loc", [None])[-1] or ""
                                sug = _df.get_close_matches(str(bad), known, n=3, cutoff=0.5)
                                if sug:
                                    suggestions = f" Did you mean: {', '.join(sug)}?"
                            except Exception:
                                pass
                        errors.append(
                            f"modules.{module_id}.config: {loc} - {e['msg']}.{suggestions}"
                        )
            elif resolved_config and config_model is None:
                logger.warning(
                    "module '%s' has no CONFIG_MODEL - config block will be "
                    "accepted silently (typos not caught at compile)", module_id,
                )

            compiled_steps: list[CompiledSetupStep] = []

            for i, step_data in enumerate(resolved["setup"]):
                action_name = step_data["action"]
                params = step_data["params"]

                if action_name not in action_names:
                    errors.append(
                        f"modules.{module_id}.setup[{i}]: "
                        f"Action '{action_name}' not found "
                        f"(available: {sorted(action_names)})"
                    )
                    continue

                params_model = None
                entry = action_registry.get(action_name)
                if entry is not None:
                    params_model = entry.params_model

                if params_model is not None:
                    try:
                        params_model.model_validate(params)
                    except ValidationError as exc:
                        for e in exc.errors():
                            loc = ".".join(str(x) for x in e["loc"])
                            errors.append(
                                f"modules.{module_id}.setup[{i}].params "
                                f"({action_name}): {loc} - {e['msg']}"
                            )
                        continue

                compiled_steps.append(
                    CompiledSetupStep(
                        module_id=module_id,
                        action=action_name,
                        resolved_params=params,
                        params_model=params_model,
                    )
                )

            constraints = resolved["constraints"]
            validated_constraints = self._validate_constraints(
                module_id, constraints, manifest, errors
            )

            # Re-fetch the actual ModuleBlock for THIS module_id so we
            # don't carry the stale `block` from the previous loop.
            # Bug fix: the first loop iterated definition.tools.modules.items()
            # and `block` retained the LAST module's value when reused
            # here, causing every CompiledModuleConfig to get the same
            # `credential` and `middleware` from whatever module was
            # last in YAML order.
            cur_block = definition.tools.modules.get(module_id)
            compiled_modules[module_id] = CompiledModuleConfig(
                module_id=module_id,
                config=resolved_config,
                setup_steps=compiled_steps,
                constraints=validated_constraints,
                middleware=getattr(cur_block, "middleware", []) or [],
                credential=getattr(cur_block, "credential", None),
            )

        self._validate_capabilities(definition, available, errors)

        compiled_agents = self._compile_agents(
            definition, compiled_modules, errors,
            source_dir=getattr(self, "_source_dir", None),
        )

        compiled_execution = self._compile_execution(
            definition, compiled_agents, errors
        )

        # ── Error: app without agents (except pipeline mode) ──
        if not compiled_agents and compiled_execution.mode != "pipeline":
            errors.append(
                "agents: At least one agent is required "
                "(except in 'pipeline' mode which chains other apps)."
            )

        # ── Error: triggers declared in non-background mode ──
        if definition.runtime.triggers and compiled_execution.mode != "background":
            errors.append(
                f"execution.triggers: Triggers are only valid in 'background' mode "
                f"(current mode: '{compiled_execution.mode}'). "
                f"Either change mode to 'background' or remove the triggers."
            )

        # ── Validate direct_modules against compiled modules ──
        if compiled_execution.direct_modules:
            all_module_ids = set(compiled_modules.keys()) | set(definition.tools.modules.keys())
            for dm in compiled_execution.direct_modules:
                if dm not in all_module_ids:
                    errors.append(
                        f"execution.direct_modules: Module '{dm}' not found "
                        f"in modules block (available: {sorted(all_module_ids)})"
                    )

        compiled_channels = self._compile_channels(definition, errors)

        has_action_constraints = any(
            block.constraints.get("allowed_actions") or block.constraints.get("blocked_actions")
            for block in definition.tools.modules.values()
        )
        security_profile = (
            self._build_security_profile(definition, errors)
            if definition.tools.capabilities is not None or has_action_constraints
            else None
        )

        # ── Warn if MCP servers lack sandbox declarations ──
        if security_profile is not None and "mcp" in compiled_modules:
            mcp_config = compiled_modules["mcp"].config or {}
            mcp_servers = mcp_config.get("servers", {})
            if isinstance(mcp_servers, dict):
                for srv_id, srv_cfg in mcp_servers.items():
                    if not isinstance(srv_cfg, dict):
                        continue
                    sandbox_decl = srv_cfg.get("sandbox")
                    if not sandbox_decl:
                        errors.append(
                            f"modules.mcp.config.servers.{srv_id}: "
                            f"No 'sandbox' block declared. When the app has "
                            f"capabilities (security profile), every MCP server "
                            f"must declare explicit sandbox permissions. "
                            f"Add: sandbox: {{permissions: [process.exec, net.http]}}"
                        )

        if "index" not in compiled_modules and "index" in available:
            compiled_modules["index"] = CompiledModuleConfig(module_id="index")

        has_specialists = any(a.role == "specialist" for a in compiled_agents)
        if has_specialists and "agent_spawn" not in compiled_modules and "agent_spawn" in available:
            compiled_modules["agent_spawn"] = CompiledModuleConfig(module_id="agent_spawn")

        if errors:
            raise AppCompilationError(errors)

        compiled_skills: list[dict[str, str]] = []
        for i, skill_def in enumerate(definition.dev.skills):
            # SkillEntry is a Pydantic model now; attribute access. The
            # required-field checks below are redundant with the model's
            # own min_length=1 constraints but kept as a safety net for
            # any code path that bypasses Pydantic validation.
            command = getattr(skill_def, "command", "") or ""
            description = getattr(skill_def, "description", "") or ""
            skill_path_str = getattr(skill_def, "path", "") or ""
            skill_ctx = f"skills[{i}]"
            if not command:
                errors.append(f"{skill_ctx}: missing required 'command' field")
                continue
            if not skill_path_str:
                errors.append(f"{skill_ctx}: missing required 'path' field")
                continue
            try:
                rel_path, content = self._load_external_text(
                    skill_path_str, label=f"{skill_ctx} (command={command})",
                )
            except FileNotFoundError as exc:
                errors.append(str(exc))
                continue

            if rel_path.endswith(".md") or rel_path.endswith(".markdown"):
                try:
                    from digitorn.core.app.yaml_loader import (
                        load_frontmatter_with_positions,
                        merge_positions,
                    )
                    fm_data, body, fm_positions = load_frontmatter_with_positions(
                        content, source=rel_path,
                    )
                    if fm_positions:
                        merge_positions(
                            self._positions, fm_positions,
                            prefix=("skills", i, "_frontmatter"),
                        )
                    if fm_data and not isinstance(fm_data, dict):
                        errors.append(
                            f"{skill_ctx}: frontmatter in {rel_path} must be a "
                            f"mapping, got {type(fm_data).__name__}"
                        )
                except Exception as exc:
                    errors.append(
                        f"{skill_ctx}: failed to parse frontmatter in "
                        f"{rel_path}: {exc}"
                    )
            compiled_skills.append({
                "command": command,
                "description": description,
                "content": content,
            })

        if errors:
            raise AppCompilationError(errors)

        # Extract hidden_actions from capabilities
        _hidden_raw = []
        if hasattr(definition, "capabilities") and definition.tools.capabilities:
            _ha = getattr(definition.tools.capabilities, "hidden_actions", [])
            for item in _ha:
                if hasattr(item, "module"):
                    _hidden_raw.append({"module": item.module, "actions": list(item.actions)})
                elif isinstance(item, dict):
                    _hidden_raw.append(item)

        compiled_widgets = self._compile_widgets(definition.ui.widgets, errors)

        resolved_behavior = definition.security.behavior
        from pathlib import Path as _CompileTracePath
        _compile_trace = _CompileTracePath.home() / ".digitorn" / "logs" / "compile_trace.log"
        try:
            with open(_compile_trace, "a", encoding="utf-8") as _f:
                _f.write(f"\n=== _compile_body end behavior={resolved_behavior is not None} app_id={getattr(definition.app, 'app_id', '?')}\n")
                if resolved_behavior:
                    _f.write(f"  profile raw: {str(getattr(resolved_behavior, 'profile', None))[:80]}\n")
                    _f.write(f"  rule_definitions: {len(getattr(resolved_behavior, 'rule_definitions', []) or [])}\n")
        except Exception:
            pass

        if resolved_behavior is not None:
            try:
                brain = getattr(resolved_behavior, "brain", None)
                if brain is not None and getattr(brain, "config", None):
                    brain.config = resolve_variables(
                        brain.config, definition.dev.variables,
                        secrets=self._secrets,
                    )
            except Exception as _exc:
                errors.append(f"behavior.brain.config: variable resolution failed: {_exc}")
            try:
                profile_val = getattr(resolved_behavior, "profile", None)
                if isinstance(profile_val, str) and "{{" in profile_val:
                    resolved_profile = resolve_variables(
                        profile_val, definition.dev.variables,
                        secrets=self._secrets,
                    )
                    resolved_behavior.profile = resolved_profile
                    try:
                        with open(_compile_trace, "a", encoding="utf-8") as _f:
                            _f.write(f"  PROFILE RESOLVED len={len(resolved_profile)} preview: {str(resolved_profile)[:150]}\n")
                    except Exception:
                        pass
            except Exception as _exc:
                errors.append(f"behavior.profile: variable resolution failed: {_exc}")
                try:
                    with open(_compile_trace, "a", encoding="utf-8") as _f:
                        _f.write(f"  PROFILE FAILED: {_exc}\n")
                except Exception:
                    pass
            try:
                cls = getattr(resolved_behavior, "classifier", None)
                if cls is not None:
                    sp = getattr(cls, "system_prompt", None)
                    if isinstance(sp, str) and "{{" in sp:
                        cls.system_prompt = resolve_variables(
                            sp, definition.dev.variables,
                            secrets=self._secrets,
                        )
            except Exception as _exc:
                errors.append(f"behavior.classifier.system_prompt: variable resolution failed: {_exc}")

        return CompiledApp(
            meta=definition.app,
            modules=compiled_modules,
            channels=compiled_channels,
            agents=compiled_agents,
            execution=compiled_execution,
            security_profile=security_profile,
            middleware=definition.runtime.middleware,
            skills=compiled_skills,
            hidden_actions=_hidden_raw,
            behavior=resolved_behavior,
            workspace=definition.ui.workspace,
            widgets=compiled_widgets,
            # Opaque pass-through blocks for the Flutter/web client.
            features=dict(definition.ui.features),
            theme=dict(definition.ui.theme),
            # Phase 2 typed slash_commands as Pydantic SlashCommand
            # objects, but the compiled output stays a list[dict] so
            # the API surface (summary(), Flutter client, downstream
            # filters) keeps working with attribute-or-key access.
            slash_commands=[
                (s.model_dump() if hasattr(s, "model_dump") else dict(s))
                for s in (definition.ui.slash_commands or [])
            ],
            warnings=list(self._warnings),
            flow=definition.flow,
        )

    def _load_widget_files(self, errors: list[str]) -> dict[str, Any]:
        """Discover ``./widgets/*.yaml`` next to app.yaml and parse each.

        Returns a ``{stem: parsed_dict}`` map. Errors during parsing
        are appended to the shared error list and the offending file
        is skipped - partial loads still allow other widgets to work.

        Bundle-mode aware: when the compiler is reading from an asset
        loader (recompile from a bundle store) the function lists files
        via the asset_loader contract instead of touching the disk.
        """
        import yaml as _yaml

        loaded: dict[str, Any] = {}

        # Bundle/asset loader path - used during reload_from_db
        if self._asset_loader is not None:
            list_fn = getattr(self._asset_loader, "list", None)
            if callable(list_fn):
                try:
                    candidates = [
                        p for p in list_fn()
                        if p.startswith("widgets/") and p.endswith(".yaml")
                    ]
                except Exception as exc:
                    errors.append(f"widgets/: asset_loader.list failed: {exc}")
                    candidates = []
                for rel in candidates:
                    try:
                        text = self._asset_loader(rel)
                        if not text:
                            continue
                        parsed = _yaml.safe_load(text)
                    except Exception as exc:
                        errors.append(f"widgets/{rel}: parse error - {exc}")
                        continue
                    name = Path(rel).stem
                    loaded[name] = parsed
            return loaded

        # Disk path - normal compile_file flow
        if self._source_dir is None:
            return loaded
        widgets_dir = self._source_dir / "widgets"
        if not widgets_dir.is_dir():
            return loaded

        for fp in sorted(widgets_dir.glob("*.yaml")):
            try:
                text = fp.read_text(encoding="utf-8")
                parsed, sub_positions = load_with_positions(text, source=f"widgets/{fp.name}")
            except Exception as exc:
                errors.append(f"widgets/{fp.name}: parse error - {exc}")
                continue
            loaded[fp.stem] = parsed
            try:
                from digitorn.core.app.yaml_loader import merge_positions
                merge_positions(
                    self._positions, sub_positions,
                    prefix=("widgets", "inline", fp.stem),
                )
            except Exception:
                pass
        return loaded

    def _compile_widgets(self, widgets_def: Any, errors: list[str]) -> Any:
        """Validate a ``widgets:`` block and merge external ./widgets/*.yaml files.

        Phase 1 - parse + version check + closed-set walk. The deeper
        validation (filters, action targets, ref cycles) lives in
        :meth:`_validate_widget_tree` invoked via ``errors``.

        Returns the WidgetsConfig instance unchanged if all checks
        pass, or a stub ``None`` if widgets were absent. On error the
        compiler appends to the shared ``errors`` list - the caller
        raises after collecting them all.
        """
        if widgets_def is None:
            # Even when no inline widgets: block is declared, an
            # external ./widgets/*.yaml folder can supply named inline
            # widgets that the agent pushes via ref:. Build an empty
            # WidgetsConfig and merge into it below.
            from digitorn.core.app.schema import WidgetsConfig
            widgets_def = WidgetsConfig()

        # ── External ./widgets/*.yaml loading ─────────────────────
        # Same pattern as ./skills/ - each .yaml file under the
        # bundle's widgets/ subdir defines one named inline widget.
        # The file stem becomes the inline key (so an agent can do
        # ``widget.render(ref="confirm_delete")`` after dropping
        # ``widgets/confirm_delete.yaml`` next to app.yaml).
        #
        # File shape:
        #
        #   # widgets/confirm_delete.yaml
        #   data: {}            # optional
        #   tree:
        #     type: confirm
        #     text: "Delete?"
        #     confirm_label: Delete
        #     confirm_action: { action: tool, tool: delete, ... }
        #
        # If the file contains only a ``tree:`` it is wrapped
        # automatically in an InlineWidget.
        loaded_external = self._load_widget_files(errors)
        if loaded_external:
            from digitorn.core.app.schema import InlineWidget
            for name, content in loaded_external.items():
                if name in widgets_def.inline:
                    errors.append(
                        f"widgets.inline.{name}: external file "
                        f"widgets/{name}.yaml collides with an inline "
                        f"entry already declared in app.yaml"
                    )
                    continue
                try:
                    if isinstance(content, dict) and "tree" in content:
                        widgets_def.inline[name] = InlineWidget(**content)
                    elif isinstance(content, dict) and "type" in content:
                        widgets_def.inline[name] = InlineWidget(tree=content)
                    else:
                        errors.append(
                            f"widgets/{name}.yaml: must be either a "
                            "tree node (with ``type:``) or a wrapper "
                            "with ``tree:`` + optional ``data:``"
                        )
                except Exception as exc:
                    errors.append(
                        f"widgets/{name}.yaml: invalid widget - {exc}"
                    )

        # Version gate
        if widgets_def.version != 1:
            errors.append(
                f"widgets.version: unsupported version {widgets_def.version!r} "
                "(only v1 is supported)"
            )
            return widgets_def

        # Walk every tree and validate each node against the closed set.
        from digitorn.core.app.schema import (
            WIDGET_PRIMITIVES, WIDGET_ACTIONS, WIDGET_ACCENTS,
            WIDGET_DENSITIES,
        )

        def _walk(node: Any, path: str, in_form: set[str] | None = None) -> None:
            if node is None:
                return
            if isinstance(node, list):
                for i, child in enumerate(node):
                    _walk(child, f"{path}[{i}]", in_form)
                return
            if not hasattr(node, "type"):
                return

            if node.type not in WIDGET_PRIMITIVES:
                errors.append(
                    f"{path}.type: unknown primitive {node.type!r}. "
                    f"Allowed: {sorted(WIDGET_PRIMITIVES)[:8]}…"
                )
                return

            if node.accent and node.accent not in WIDGET_ACCENTS:
                errors.append(
                    f"{path}.accent: unknown accent {node.accent!r} "
                    f"(allowed: {sorted(WIDGET_ACCENTS)})"
                )
            if node.density and node.density not in WIDGET_DENSITIES:
                errors.append(
                    f"{path}.density: unknown density {node.density!r} "
                    f"(allowed: {sorted(WIDGET_DENSITIES)})"
                )

            # Form input name uniqueness
            if node.type == "form":
                in_form = set()
            if in_form is not None and node.type in {
                "text_input", "textarea", "select", "multi_select",
                "radio", "checkbox", "switch", "slider",
                "date", "time", "datetime", "file_upload", "code_editor",
            }:
                name = getattr(node, "name", None) or (
                    node.model_dump().get("name") if hasattr(node, "model_dump") else None
                )
                if name:
                    if name in in_form:
                        errors.append(
                            f"{path}: duplicate input name {name!r} "
                            "in the same form"
                        )
                    else:
                        in_form.add(name)

            # Recurse into known container fields
            for child_field in (
                "children", "item", "first", "second",
                "body", "render", "empty", "loading",
            ):
                child = getattr(node, child_field, None)
                if child is not None:
                    _walk(child, f"{path}.{child_field}", in_form)

            # Validate actions on this node + standard attached fields
            extra = node.model_dump() if hasattr(node, "model_dump") else {}
            for action_field in ("action", "submit", "on_select", "row_action", "on_move"):
                a = extra.get(action_field)
                if isinstance(a, dict) and a:
                    _validate_action(a, f"{path}.{action_field}")

        def _validate_action(act: Any, path: str) -> None:
            if not isinstance(act, dict):
                return
            kind = act.get("action")
            # ``submit:`` wraps an inner action under ``action:`` along
            # with display fields (label, icon, etc) - peel one layer.
            if isinstance(kind, dict):
                _validate_action(kind, f"{path}.action")
                return
            if kind is None:
                # Some containers (submit, reset) might omit the action
                # altogether - that's fine; validate nested fields if
                # they happen to be present.
                for f in ("then", "on_success", "on_error"):
                    if isinstance(act.get(f), dict):
                        _validate_action(act[f], f"{path}.{f}")
                return
            if not isinstance(kind, str):
                errors.append(
                    f"{path}.action: action kind must be a string, got "
                    f"{type(kind).__name__}"
                )
                return
            if kind not in WIDGET_ACTIONS:
                errors.append(
                    f"{path}.action: unknown action {kind!r} "
                    f"(allowed: {sorted(WIDGET_ACTIONS)})"
                )
            for nested_field in ("then", "on_success", "on_error"):
                if isinstance(act.get(nested_field), dict):
                    _validate_action(act[nested_field], f"{path}.{nested_field}")
            if kind == "sequence" and isinstance(act.get("steps"), list):
                for i, step in enumerate(act["steps"]):
                    if isinstance(step, dict):
                        _validate_action(step, f"{path}.steps[{i}]")

        # Walk all 4 zones
        if widgets_def.chat_side is not None:
            _walk(widgets_def.chat_side.tree, "widgets.chat_side.tree")
        for i, tab in enumerate(widgets_def.workspace_tabs):
            _walk(tab.tree, f"widgets.workspace_tabs[{i}].tree")
        for name, modal in widgets_def.modals.items():
            _walk(modal.tree, f"widgets.modals.{name}.tree")
        for name, inline in widgets_def.inline.items():
            _walk(inline.tree, f"widgets.inline.{name}.tree")

        self._validate_widget_refs(widgets_def, errors)

        return widgets_def

    def _validate_widget_refs(self, widgets_def: Any, errors: list[str]) -> None:
        """Verify every ``ref:`` in a widget tree points to widgets.inline.<name>,
        and detect cycles (A → B → A).
        """
        inline_names = set((getattr(widgets_def, "inline", {}) or {}).keys())

        def _collect_refs(node: Any) -> list[str]:
            refs: list[str] = []
            if not isinstance(node, dict):
                data = node.model_dump() if hasattr(node, "model_dump") else {}
            else:
                data = node
            for k, v in (data or {}).items():
                if k == "ref" and isinstance(v, str) and v:
                    refs.append(v)
                elif isinstance(v, dict):
                    refs.extend(_collect_refs(v))
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, (dict, list)):
                            refs.extend(_collect_refs(item))
            return refs

        graph: dict[str, list[str]] = {}
        for name, inline in (getattr(widgets_def, "inline", {}) or {}).items():
            tree = getattr(inline, "tree", None)
            refs = _collect_refs(tree) if tree is not None else []
            graph[name] = refs
            for r in refs:
                if r not in inline_names:
                    import difflib as _df
                    sug = _df.get_close_matches(r, inline_names, n=3, cutoff=0.6)
                    hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
                    errors.append(
                        f"widgets.inline.{name}: ref '{r}' does not match any "
                        f"declared inline widget. Declared: {sorted(inline_names)}.{hint}"
                    )

        def _visit(node: str, path: tuple[str, ...]) -> None:
            if node in path:
                cycle = " → ".join(list(path[path.index(node):]) + [node])
                errors.append(
                    f"widgets.inline: cycle detected in widget refs: {cycle}"
                )
                return
            for child in graph.get(node, []):
                if child in inline_names:
                    _visit(child, path + (node,))

        for root in inline_names:
            _visit(root, ())

        for zone_name, zone in [
            ("chat_side", getattr(widgets_def, "chat_side", None)),
        ]:
            if zone is not None:
                tree = getattr(zone, "tree", None)
                if tree is not None:
                    for r in _collect_refs(tree):
                        if r not in inline_names:
                            import difflib as _df
                            sug = _df.get_close_matches(r, inline_names, n=3, cutoff=0.6)
                            hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
                            errors.append(
                                f"widgets.{zone_name}: ref '{r}' does not match any "
                                f"declared inline widget. Declared: {sorted(inline_names)}.{hint}"
                            )
        for i, tab in enumerate(getattr(widgets_def, "workspace_tabs", []) or []):
            tree = getattr(tab, "tree", None)
            if tree is not None:
                for r in _collect_refs(tree):
                    if r not in inline_names:
                        import difflib as _df
                        sug = _df.get_close_matches(r, inline_names, n=3, cutoff=0.6)
                        hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
                        errors.append(
                            f"widgets.workspace_tabs[{i}]: ref '{r}' not declared. "
                            f"Inline: {sorted(inline_names)}.{hint}"
                        )
        for name, modal in (getattr(widgets_def, "modals", {}) or {}).items():
            tree = getattr(modal, "tree", None)
            if tree is not None:
                for r in _collect_refs(tree):
                    if r not in inline_names:
                        import difflib as _df
                        sug = _df.get_close_matches(r, inline_names, n=3, cutoff=0.6)
                        hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
                        errors.append(
                            f"widgets.modals.{name}: ref '{r}' not declared. "
                            f"Inline: {sorted(inline_names)}.{hint}"
                        )

    def _validate_constraints(
        self,
        module_id: str,
        constraints: dict[str, Any],
        manifest: Any,
        errors: list[str],
    ) -> dict[str, Any]:
        """Validate constraint keys against module's ConstraintSpec declarations."""
        if not constraints:
            return {}

        supported: dict[str, Any] = {}
        for spec in getattr(manifest, "supported_constraints", []):
            supported[spec.name] = spec

        validated: dict[str, Any] = {}

        for key, value in constraints.items():
            if key in _UNIVERSAL_CONSTRAINT_KEYS:
                if not isinstance(value, list):
                    errors.append(
                        f"modules.{module_id}.constraints.{key}: "
                        f"Expected a list, got {type(value).__name__}"
                    )
                    continue
                validated[key] = value
                continue

            if key not in supported:
                errors.append(
                    f"modules.{module_id}.constraints.{key}: "
                    f"Unknown constraint. Module '{module_id}' supports: "
                    f"{sorted(list(supported.keys()) + list(_UNIVERSAL_CONSTRAINT_KEYS))}"
                )
                continue

            spec = supported[key]
            coerced = self._coerce_constraint(key, value, spec.type, module_id, errors)
            if coerced is not None:
                validated[key] = coerced

        return validated

    def _coerce_constraint(
        self,
        key: str,
        value: Any,
        expected_type: str,
        module_id: str,
        errors: list[str],
    ) -> Any:
        """Coerce a constraint value to the expected type."""
        try:
            if expected_type == "integer":
                return int(value)
            if expected_type == "string_list":
                if isinstance(value, list):
                    items = [str(v).strip() for v in value]
                    empties = [i for i, v in enumerate(items) if not v]
                    if empties:
                        errors.append(
                            f"modules.{module_id}.constraints.{key}: "
                            f"Empty strings at indices {empties}"
                        )
                        return None
                    return items
                errors.append(
                    f"modules.{module_id}.constraints.{key}: "
                    f"Expected a list of strings"
                )
                return None
            if expected_type == "size":
                s = str(value).strip()
                if not _CONSTRAINT_SIZE_RE.match(s):
                    errors.append(
                        f"modules.{module_id}.constraints.{key}: "
                        f"Invalid size format '{s}' - expected e.g. '50MB', '1GB'"
                    )
                    return None
                return s
            if expected_type == "duration":
                s = str(value).strip()
                if not _CONSTRAINT_DURATION_RE.match(s):
                    errors.append(
                        f"modules.{module_id}.constraints.{key}: "
                        f"Invalid duration format '{s}' - expected e.g. '30s', '5m', '1h'"
                    )
                    return None
                return s
            if expected_type == "boolean":
                if isinstance(value, bool):
                    return value
                if isinstance(value, str):
                    return value.lower() in ("true", "1", "yes")
                return bool(value)
            return value
        except (ValueError, TypeError) as exc:
            errors.append(
                f"modules.{module_id}.constraints.{key}: "
                f"Cannot coerce to {expected_type}: {exc}"
            )
            return None

    def _validate_capabilities(
        self,
        definition: AppDefinition,
        available_modules: set[str],
        errors: list[str],
    ) -> None:
        """Validate that capability references point to real modules/actions."""
        caps = definition.tools.capabilities
        if caps is None:
            return
        for i, grant in enumerate(caps.grant):
            if grant.module.startswith("mcp_"):
                continue
            if grant.module not in available_modules:
                errors.append(
                    f"capabilities.grant[{i}]: Module '{grant.module}' not available"
                )
            elif grant.actions:
                self._check_actions_exist(
                    f"capabilities.grant[{i}]", grant.module, grant.actions, errors
                )

        for i, entry in enumerate(caps.approve):
            if entry.module.startswith("mcp_"):
                continue
            if entry.module not in available_modules:
                errors.append(
                    f"capabilities.approve[{i}]: Module '{entry.module}' not available"
                )
            elif entry.actions:
                self._check_actions_exist(
                    f"capabilities.approve[{i}]", entry.module, entry.actions, errors
                )

        for i, deny in enumerate(caps.deny):
            if deny.module.startswith("mcp_"):
                continue
            if deny.module not in available_modules:
                errors.append(
                    f"capabilities.deny[{i}]: Module '{deny.module}' not available"
                )
            elif deny.actions:
                self._check_actions_exist(
                    f"capabilities.deny[{i}]", deny.module, deny.actions, errors
                )

        for mid in caps.hidden_modules:
            if mid.startswith("mcp_"):
                continue
            if mid not in available_modules:
                errors.append(
                    f"capabilities.hidden_modules: Module '{mid}' not available"
                )

        for i, entry in enumerate(caps.hidden_actions):
            if entry.module.startswith("mcp_"):
                continue
            if entry.module not in available_modules:
                errors.append(
                    f"capabilities.hidden_actions[{i}]: Module '{entry.module}' not available"
                )
            elif entry.actions:
                self._check_actions_exist(
                    f"capabilities.hidden_actions[{i}]", entry.module, entry.actions, errors
                )

    def _compile_agents(
        self,
        definition: AppDefinition,
        compiled_modules: dict[str, CompiledModuleConfig],
        errors: list[str],
        source_dir: Path | None = None,
    ) -> list[CompiledAgent]:
        """Compile agent definitions and resolve brain configurations.

        For inline brains: generates a provider config that will be
        auto-registered in llm_provider at bootstrap time, and ensures
        llm_provider is in the compiled modules.

        For reference brains: validates the provider_id exists in
        modules.llm_provider.config.providers.
        """
        if not definition.agents:
            return []

        compiled_agents: list[CompiledAgent] = []
        seen_ids: set[str] = set()

        llm_block = definition.tools.modules.get("llm_provider")
        named_providers: set[str] = set()
        if llm_block and llm_block.config.get("providers"):
            named_providers = set(llm_block.config["providers"].keys())

        inline_providers: dict[str, dict[str, Any]] = {}

        for i, agent_def in enumerate(definition.agents):
            ctx = f"agents[{i}]"

            if agent_def.id in seen_ids:
                errors.append(f"{ctx}: Duplicate agent id '{agent_def.id}'")
                continue
            seen_ids.add(agent_def.id)

            brain = agent_def.brain

            # Pool config: AgentPoolConfig (Pydantic) enforces every
            # constraint - max_workers >= 1, auto_retry >= 0, no extras.
            # We just unpack the validated values here.
            pool_max_workers = agent_def.pool.max_workers
            pool_progress = agent_def.pool.progress
            pool_auto_retry = agent_def.pool.auto_retry

            # ── Validate specialist agent modules (independent of variables) ──
            if agent_def.role == "specialist" and getattr(agent_def, "modules", None):
                all_module_ids = set(compiled_modules.keys()) | set(definition.tools.modules.keys())
                for m_idx, m in enumerate(agent_def.modules):
                    mid = ""
                    granular_actions: list[str] = []
                    if isinstance(m, str):
                        mid = m
                    elif isinstance(m, dict):
                        if len(m) != 1:
                            errors.append(
                                f"{ctx}.modules[{m_idx}]: granular module block must have "
                                f"exactly one key (the module id), got {list(m.keys())}"
                            )
                            continue
                        mid = next(iter(m))
                        val = m[mid]
                        if val is None or val == []:
                            granular_actions = []
                        elif isinstance(val, list):
                            if not all(isinstance(a, str) for a in val):
                                errors.append(
                                    f"{ctx}.modules[{m_idx}].{mid}: action list must "
                                    f"contain strings, got {val!r}"
                                )
                                continue
                            granular_actions = list(val)
                        else:
                            errors.append(
                                f"{ctx}.modules[{m_idx}].{mid}: value must be a list of "
                                f"action names, got {type(val).__name__}"
                            )
                            continue
                    else:
                        errors.append(
                            f"{ctx}.modules[{m_idx}]: entry must be a string (module id) "
                            f"or dict (granular access), got {type(m).__name__}"
                        )
                        continue
                    if not mid:
                        continue
                    if mid not in all_module_ids:
                        import difflib as _df
                        suggestions = _df.get_close_matches(mid, all_module_ids, n=3, cutoff=0.6)
                        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
                        errors.append(
                            f"{ctx}.modules[{m_idx}]: module '{mid}' is not declared in "
                            f"the top-level modules block. Available: {sorted(all_module_ids)}.{hint}"
                        )
                        continue
                    if granular_actions:
                        self._check_actions_exist(
                            f"{ctx}.modules[{m_idx}].{mid}",
                            mid, granular_actions, errors,
                        )

            try:
                resolved_brain_config = resolve_variables(
                    brain.config, definition.dev.variables,
                    secrets=self._secrets,
                )
                resolved_system_prompt = resolve_variables(
                    agent_def.system_prompt, definition.dev.variables,
                    secrets=self._secrets,
                ) if agent_def.system_prompt else ""

                # ── Auto-load capabilities from skills/ ──
                # When the agent declares ``capabilities: [commit,
                # review]``, read ``skills/<name>.md`` for each and
                # append the content under a dedicated section so
                # the LLM sees the skill definitions inline.
                capabilities = getattr(agent_def, "capabilities", []) or []
                if capabilities:
                    sections: list[str] = []
                    for skill_name in capabilities:
                        try:
                            body = resolve_variables(
                                "{{skill." + skill_name + "}}",
                                definition.dev.variables,
                                secrets=self._secrets,
                            )
                        except ValueError as exc:
                            errors.append(
                                f"{ctx}.capabilities[{skill_name!r}]: "
                                f"{exc}"
                            )
                            continue
                        if body and not body.startswith("{{skill."):
                            sections.append(f"### {skill_name}\n{body}")
                    if sections:
                        resolved_system_prompt = (
                            (resolved_system_prompt or "").rstrip()
                            + "\n\n## Available capabilities\n\n"
                            + "\n\n".join(sections)
                        )
            except ValueError as exc:
                errors.append(f"{ctx}: {exc}")
                continue

            if brain.is_reference:
                pid = brain.provider_id
                if pid not in named_providers:
                    errors.append(
                        f"{ctx}.brain: provider_id '{pid}' not found in "
                        f"modules.llm_provider.config.providers "
                        f"(available: {sorted(named_providers)})"
                    )
                    continue

                compiled_brain = CompiledBrain(
                    provider_id=pid,
                    is_inline=False,
                    temperature=brain.temperature,
                    max_tokens=brain.max_tokens,
                    top_p=brain.top_p,
                    timeout=brain.timeout,
                    native_tool_use=brain.native_tool_use,
                    context=_compile_brain_context(brain),
                    credential=getattr(brain, "credential", None),
                )
            else:
                if not brain.model:
                    errors.append(
                        f"{ctx}.brain: 'model' is required for inline brain config"
                    )
                    continue

                auto_pid = f"{agent_def.id}_brain"

                _KNOWN_PROVIDERS = {
                    "openai", "deepseek", "groq", "mistral", "together",
                    "lm_studio", "vllm", "ollama", "anthropic",
                    "google-gemini", "gemini", "xai", "grok",
                    "cerebras", "perplexity", "fireworks",
                    "github_copilot",
                }
                _KNOWN_BACKENDS = {
                    "openai_compat", "anthropic", "github_copilot",
                }

                if brain.backend and brain.backend not in _KNOWN_BACKENDS:
                    import difflib as _df
                    sug = _df.get_close_matches(brain.backend, _KNOWN_BACKENDS, n=2, cutoff=0.6)
                    hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
                    errors.append(
                        f"{ctx}.brain.backend: unknown backend '{brain.backend}'. "
                        f"Supported: {sorted(_KNOWN_BACKENDS)}.{hint}"
                    )
                if brain.provider and brain.provider.lower() not in _KNOWN_PROVIDERS:
                    import difflib as _df
                    sug = _df.get_close_matches(brain.provider.lower(), _KNOWN_PROVIDERS, n=3, cutoff=0.6)
                    hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
                    errors.append(
                        f"{ctx}.brain.provider: unknown provider '{brain.provider}'. "
                        f"Built-in: {sorted(_KNOWN_PROVIDERS)}.{hint}"
                    )

                detected_backend = brain.backend
                if detected_backend == "openai_compat" and brain.provider:
                    prov_lower = brain.provider.lower()
                    if "anthropic" in prov_lower or "claude" in prov_lower:
                        detected_backend = "anthropic"

                provider_conf: dict[str, Any] = {
                    "backend": detected_backend,
                    "model": brain.model,
                }
                if brain.provider:
                    provider_conf["provider"] = brain.provider
                if brain.timeout:
                    provider_conf["timeout"] = brain.timeout

                provider_conf.update(resolved_brain_config)

                if brain.temperature is not None:
                    provider_conf["temperature"] = brain.temperature
                if brain.max_tokens is not None:
                    provider_conf["max_tokens"] = brain.max_tokens
                if brain.top_p is not None:
                    provider_conf["top_p"] = brain.top_p

                inline_providers[auto_pid] = provider_conf

                compiled_brain = CompiledBrain(
                    provider_id=auto_pid,
                    is_inline=True,
                    inline_config=provider_conf,
                    temperature=brain.temperature,
                    max_tokens=brain.max_tokens,
                    top_p=brain.top_p,
                    timeout=brain.timeout,
                    native_tool_use=brain.native_tool_use,
                    context=_compile_brain_context(brain),
                    credential=getattr(brain, "credential", None),
                )

            skills_content = ""
            if agent_def.skills:
                try:
                    _, skills_content = self._load_external_text(
                        agent_def.skills, label=f"agents[{i}].skills",
                    )
                except FileNotFoundError as exc:
                    errors.append(str(exc))

            # ── Cross-check: every module/action referenced in the
            # specialist's modules: list must exist. We accept system
            # modules (agent_spawn, context_builder, llm_provider, index)
            # because they are auto-loaded by bootstrap even when not
            # declared at top-level.
            _SYSTEM_MODULES = {
                "agent_spawn", "context_builder", "llm_provider", "index",
            }
            declared_modules = set(definition.tools.modules.keys()) | _SYSTEM_MODULES
            for j, mod_entry in enumerate(agent_def.modules or []):
                if isinstance(mod_entry, str):
                    mod_id = mod_entry
                    requested_actions: list[str] | None = None
                else:
                    mod_id = next(iter(mod_entry.keys()))
                    requested_actions = list(mod_entry[mod_id])
                if mod_id.startswith("mcp_"):
                    continue
                if mod_id not in declared_modules:
                    import difflib as _df
                    sug = _df.get_close_matches(mod_id, declared_modules, n=3, cutoff=0.6)
                    hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
                    errors.append(
                        f"{ctx}.modules[{j}]: module '{mod_id}' is not declared "
                        f"in the modules block. Declared: "
                        f"{sorted(definition.tools.modules.keys())}.{hint}"
                    )
                    continue
                if requested_actions and mod_id not in _SYSTEM_MODULES:
                    try:
                        module = self._registry.get(mod_id)
                        manifest = module.get_manifest()
                        known_actions = set(manifest.action_names())
                    except Exception:
                        known_actions = set()
                    for act in requested_actions:
                        if act not in known_actions:
                            import difflib as _df
                            sug = _df.get_close_matches(act, known_actions, n=3, cutoff=0.6)
                            hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
                            errors.append(
                                f"{ctx}.modules[{j}].{mod_id}: action '{act}' "
                                f"not found on module '{mod_id}'. Available: "
                                f"{sorted(known_actions)}.{hint}"
                            )

            # Per-agent hooks: reuse the execution-hooks compiler to
            # validate condition/action types, then stamp each with
            # agent_id=<this agent> so the runtime filter knows to
            # fire them only for this agent's turns.
            agent_hooks_list: list[CompiledHook] = []
            if getattr(agent_def, "hooks", None):
                agent_hooks_list = self._compile_agent_hooks(
                    agent_def.id, agent_def.hooks, errors,
                )

            compiled_agents.append(CompiledAgent(
                agent_id=agent_def.id,
                role=agent_def.role,
                brain=compiled_brain,
                system_prompt=resolved_system_prompt,
                plan_first=agent_def.plan_first,
                specialty=agent_def.specialty,
                skills_content=skills_content,
                modules=agent_def.modules,
                pool_max_workers=pool_max_workers,
                pool_progress=pool_progress,
                pool_auto_retry=pool_auto_retry,
                hooks=agent_hooks_list,
            ))

        if inline_providers:
            if "llm_provider" not in compiled_modules:
                compiled_modules["llm_provider"] = CompiledModuleConfig(
                    module_id="llm_provider",
                    config={"providers": {}},
                )

            existing_providers = compiled_modules["llm_provider"].config.get("providers", {})
            existing_providers.update(inline_providers)
            compiled_modules["llm_provider"].config["providers"] = existing_providers

        return compiled_agents

    def _compile_execution(
        self,
        definition: AppDefinition,
        compiled_agents: list[CompiledAgent],
        errors: list[str],
    ) -> CompiledExecution:
        """Compile the runtime block and validate references.

        Reads from ``definition.runtime`` for most fields, plus
        ``definition.security`` (sandbox, credentials_schema) and
        ``definition.ui.greeting`` for fields that moved out of the
        legacy ``execution:`` block.
        """
        exe = definition.runtime

        valid_modes = {"one_shot", "conversation", "background", "pipeline"}
        if exe.mode not in valid_modes:
            errors.append(
                f"execution.mode: Invalid mode '{exe.mode}'. "
                f"Must be one of: {sorted(valid_modes)}"
            )

        entry_agent = exe.entry_agent
        if not entry_agent and compiled_agents:
            entry_agent = compiled_agents[0].agent_id
        elif entry_agent:
            agent_ids = {a.agent_id for a in compiled_agents}
            if entry_agent not in agent_ids:
                errors.append(
                    f"execution.entry_agent: Agent '{entry_agent}' not found "
                    f"(available: {sorted(agent_ids)})"
                )

        compiled_triggers: list[CompiledTrigger] = []
        # The channels module manages its own triggers via providers - when
        # present, legacy execution.triggers are not required.
        has_channels_module = "channels" in definition.tools.modules
        if exe.mode == "background":
            if not exe.triggers and not has_channels_module:
                errors.append(
                    "execution.triggers: Background mode requires at least one trigger "
                    "(or load the 'channels' module with providers)"
                )
            seen_trigger_ids: set[str] = set()
            for i, t in enumerate(exe.triggers):
                if t.id in seen_trigger_ids:
                    errors.append(f"execution.triggers[{i}]: Duplicate trigger id '{t.id}'")
                seen_trigger_ids.add(t.id)

                if t.type not in ("cron", "watch", "http"):
                    errors.append(
                        f"execution.triggers[{i}]: Invalid type '{t.type}'. "
                        f"Must be 'cron', 'watch', or 'http'"
                    )
                if t.type == "cron" and not t.schedule:
                    errors.append(f"execution.triggers[{i}]: 'schedule' required for cron trigger")
                if t.type == "watch" and not t.paths:
                    errors.append(f"execution.triggers[{i}]: 'paths' required for watch trigger")
                if t.type == "http" and not t.path:
                    errors.append(f"execution.triggers[{i}]: 'path' required for http trigger")

                # Validate routing
                if hasattr(t, "routing") and t.routing not in ("broadcast", "user", "session"):
                    errors.append(
                        f"execution.triggers[{i}]: Invalid routing '{t.routing}'. "
                        f"Must be 'broadcast', 'user', or 'session'"
                    )
                if hasattr(t, "routing") and t.routing in ("user", "session") and not getattr(t, "routing_key", ""):
                    errors.append(
                        f"execution.triggers[{i}]: 'routing_key' required when routing is '{t.routing}'"
                    )

                compiled_triggers.append(CompiledTrigger(
                    id=t.id,
                    type=t.type,
                    schedule=t.schedule,
                    paths=t.paths,
                    path=t.path,
                    method=t.method,
                    port=t.port,
                    message=t.message,
                    routing=getattr(t, "routing", "broadcast"),
                    routing_key=getattr(t, "routing_key", ""),
                ))
        elif exe.triggers:
            # Triggers declared but mode is not 'background'. The runtime
            # silently ignores them - that's a footgun. Surface as a
            # non-fatal warning so the user sees it without breaking
            # builds that explicitly set the mode for another reason.
            trigger_ids = ", ".join(t.id for t in exe.triggers)
            self._warnings.append(
                f"execution.triggers: {len(exe.triggers)} trigger(s) declared "
                f"({trigger_ids}) but mode='{exe.mode}'. Triggers only fire "
                f"in mode: background. They will be ignored at runtime."
            )

        valid_input_types = {"text", "image", "audio", "video", "file", "json", "any"}
        if exe.input.type not in valid_input_types:
            errors.append(
                f"execution.input.type: Invalid type '{exe.input.type}'. "
                f"Must be one of: {sorted(valid_input_types)}"
            )

        valid_output_types = {"text", "json", "markdown", "file", "image", "audio"}
        if exe.output.type not in valid_output_types:
            errors.append(
                f"execution.output.type: Invalid type '{exe.output.type}'. "
                f"Must be one of: {sorted(valid_output_types)}"
            )

        # `greeting` now lives under `ui:` (pure display string).
        greeting = definition.ui.greeting
        if greeting:
            try:
                from digitorn.core.app.variables import resolve_variables
                greeting = resolve_variables(
                    greeting, definition.dev.variables, secrets=self._secrets,
                )
            except ValueError as exc:
                errors.append(f"ui.greeting: {exc}")

        # `workspace` was renamed to `workdir` to disambiguate from
        # `ui.workspace` (the renderer block, a different concept).
        workspace = exe.workdir
        if workspace:
            try:
                from digitorn.core.app.variables import resolve_variables
                workspace = resolve_variables(
                    workspace, definition.dev.variables, secrets=self._secrets,
                )
            except ValueError as exc:
                errors.append(f"runtime.workdir: {exc}")

        compiled_hooks = self._compile_hooks(exe, errors)

        ctx_cfg = exe.context
        valid_strategies = {"truncate", "summarize"}
        if ctx_cfg.strategy not in valid_strategies:
            errors.append(
                f"execution.context.strategy: Invalid '{ctx_cfg.strategy}'. "
                f"Must be one of: {sorted(valid_strategies)}"
            )
        if not (0.0 < ctx_cfg.compression_trigger <= 1.0):
            errors.append(
                f"execution.context.compression_trigger: Must be between 0.0 and 1.0 "
                f"(got {ctx_cfg.compression_trigger})"
            )

        compiled_context = CompiledContextConfig(
            max_tokens=ctx_cfg.max_tokens,
            output_reserved=ctx_cfg.output_reserved,
            strategy=ctx_cfg.strategy,
            keep_recent=ctx_cfg.keep_recent,
            compression_trigger=ctx_cfg.compression_trigger,
            summary_max_tokens=ctx_cfg.summary_max_tokens,
            auto_compact=ctx_cfg.auto_compact,
            summary_brain=ctx_cfg.summary_brain,
        )

        return CompiledExecution(
            mode=exe.mode,
            entry_agent=entry_agent,
            max_turns=exe.max_turns,
            timeout=exe.timeout,
            workspace=workspace,
            workspace_mode=getattr(exe, "workdir_mode", "auto"),
            input=CompiledInput(
                type=exe.input.type,
                accept=exe.input.accept,
                max_size=exe.input.max_size,
                description=exe.input.description,
                required=exe.input.required,
            ),
            output=CompiledOutput(
                type=exe.output.type,
                format=exe.output.format,
                description=exe.output.description,
                schema_def=exe.output.schema_def,
            ),
            greeting=greeting,
            triggers=compiled_triggers,
            context=compiled_context,
            hooks=compiled_hooks,
            watchers=exe.watchers,
            scheduler=exe.scheduler,
            default_channel=exe.default_channel,
            project_memory=exe.project_memory,
            direct_modules=list(exe.direct_modules),
            tool_injection=getattr(exe, "tool_injection", None),
            session_mode=getattr(exe, "session_mode", "mono"),
            max_sessions_per_user=getattr(exe, "max_sessions_per_user", 10),
            max_concurrent_activations=getattr(exe, "max_concurrent_activations", 20),
            payload_schema=self._compile_payload_schema(exe, errors),
            credentials_schema=self._compile_credentials_schema(definition, errors),
        )

    def _compile_credentials_schema(
        self,
        definition: AppDefinition,
        errors: list[str],
    ) -> dict[str, Any] | None:
        """Validate and freeze the optional credentials_schema into a plain dict.

        The schema lives under ``security.credentials_schema`` in the
        canonical shape (it migrated out of the legacy ``execution:``
        block because credential vault is a security concern, not an
        execution concern).

        Rules enforced here:

        - ``oauth2`` providers MUST use ``scope: per_user`` (an OAuth
          access_token is tied to a single user account and can't be
          meaningfully shared).
        - Provider names must be unique within the schema.
        - Provider names must be valid identifiers (kebab-case allowed
          since we use them as URL segments).
        - Scope must be one of the 4 declared values.

        Returns ``None`` when no credentials_schema is declared so
        existing apps are unaffected.
        """
        cs = definition.security.credentials_schema
        if cs is None:
            return None

        seen_names: set[str] = set()
        for i, prov in enumerate(cs.providers):
            ctx = f"execution.credentials_schema.providers[{i}]"

            # name validation
            if not prov.name:
                errors.append(f"{ctx}: name is required")
                continue
            if not _is_safe_name(prov.name):
                errors.append(
                    f"{ctx}.name: '{prov.name}' must be a kebab-case identifier"
                )
            if prov.name in seen_names:
                errors.append(f"{ctx}.name: duplicate '{prov.name}'")
            seen_names.add(prov.name)

            # oauth2 scope enforcement
            if prov.type == "oauth2" and prov.scope != "per_user":
                errors.append(
                    f"{ctx}: oauth2 providers MUST use scope='per_user' "
                    f"(access tokens are tied to a specific user account), "
                    f"got scope='{prov.scope}'"
                )
            if prov.type == "oauth2" and not prov.oauth_provider:
                errors.append(
                    f"{ctx}: oauth2 providers must declare 'oauth_provider' "
                    f"(the daemon-registered OAuth client key)"
                )

            # mcp_server specifics
            if prov.type == "mcp_server":
                if prov.transport not in ("stdio", "http", "ws"):
                    errors.append(
                        f"{ctx}: mcp_server providers must declare "
                        f"transport (stdio | http | ws)"
                    )
                if prov.transport == "stdio" and not prov.command:
                    errors.append(
                        f"{ctx}: stdio MCP servers must declare 'command'"
                    )
                if prov.transport in ("http", "ws") and not prov.url:
                    errors.append(
                        f"{ctx}: http/ws MCP servers must declare 'url'"
                    )

            # fields - require at least one for types that need user input
            needs_fields = prov.type in (
                "api_key", "multi_field", "connection_string", "mcp_server",
                "custom",
            )
            if needs_fields and not prov.fields:
                errors.append(
                    f"{ctx}: type={prov.type} requires at least one "
                    f"declared field"
                )

            # per-field identifier check
            seen_field_names: set[str] = set()
            for j, field in enumerate(prov.fields):
                fctx = f"{ctx}.fields[{j}]"
                if not field.name or not _is_safe_name(field.name):
                    errors.append(
                        f"{fctx}.name: '{field.name}' is not a valid identifier"
                    )
                if field.name in seen_field_names:
                    errors.append(f"{fctx}.name: duplicate '{field.name}'")
                seen_field_names.add(field.name)
                if field.type == "select" and not field.options:
                    errors.append(
                        f"{fctx}: type=select requires non-empty 'options'"
                    )

        return cs.model_dump(mode="json")

    def _compile_payload_schema(
        self,
        exe: ExecutionConfig,
        errors: list[str],
    ) -> dict[str, Any] | None:
        """Validate and freeze the optional payload_schema into a plain dict.

        Returns ``None`` when no schema is declared. Returns a dict with
        the same shape as the YAML so the API can ship it verbatim and
        validators can read it without importing Pydantic.
        """
        ps = getattr(exe, "payload_schema", None)
        if ps is None:
            return None

        # payload_schema only makes sense in background mode.
        if exe.mode != "background":
            errors.append(
                "execution.payload_schema: only valid when execution.mode='background'"
            )
            return None

        # Per-field name uniqueness on metadata
        seen_meta_names: set[str] = set()
        for i, fld in enumerate(ps.metadata):
            if not fld.name.isidentifier():
                errors.append(
                    f"execution.payload_schema.metadata[{i}].name: "
                    f"'{fld.name}' is not a valid identifier"
                )
            if fld.name in seen_meta_names:
                errors.append(
                    f"execution.payload_schema.metadata[{i}].name: "
                    f"duplicate field '{fld.name}'"
                )
            seen_meta_names.add(fld.name)
            if fld.type == "select" and not fld.options:
                errors.append(
                    f"execution.payload_schema.metadata[{i}]: "
                    f"type='select' requires non-empty 'options'"
                )

        # Per-slot name uniqueness on file slots
        seen_file_names: set[str] = set()
        for i, slot in enumerate(ps.files):
            if slot.name in seen_file_names:
                errors.append(
                    f"execution.payload_schema.files[{i}].name: "
                    f"duplicate slot '{slot.name}'"
                )
            seen_file_names.add(slot.name)
            if slot.max_size_mb > 25:
                errors.append(
                    f"execution.payload_schema.files[{i}].max_size_mb: "
                    f"exceeds server hard cap of 25 MB"
                )

        # Freeze to dict - model_dump gives us deep-copied JSON-safe data.
        return ps.model_dump(mode="json")

    def _compile_hooks(
        self,
        exe: ExecutionConfig,
        errors: list[str],
    ) -> list[CompiledHook]:
        """Compile internal hooks from the execution config."""
        if not exe.hooks:
            return []

        from digitorn.core.runtime.hooks import get_condition, get_action
        import digitorn.core.runtime.tool_hooks  # registers tool_match condition  # noqa: F401

        compiled_hooks: list[CompiledHook] = []
        seen_ids: set[str] = set()
        # Full set of 15 events. The runtime currently emits 4 directly
        # (turn_start, turn_end, tool_start, tool_end) plus aliases
        # (pre_tool_use → tool_start, post_tool_use → tool_end,
        # user_prompt → turn_start). The others fire from their natural
        # integration points (manager, approval queue, agent_spawn, …)
        # once wired. We accept all 15 names at compile time so apps
        # can declare forward-compatible hook configs.
        valid_events = {
            "turn_start", "turn_end",
            "tool_start", "tool_end",
            "pre_tool_use", "post_tool_use",
            "user_prompt",
            "session_start", "session_end",
            "pre_compact",
            "error",
            "approval_request",
            "agent_spawn", "agent_complete",
            "activation",
        }

        for i, hook in enumerate(exe.hooks):
            ctx = f"execution.hooks[{i}]"

            if hook.id in seen_ids:
                errors.append(f"{ctx}: Duplicate hook id '{hook.id}'")
                continue
            seen_ids.add(hook.id)

            if hook.on not in valid_events:
                errors.append(
                    f"{ctx}.on: Invalid event '{hook.on}'. "
                    f"Must be one of: {sorted(valid_events)}"
                )

            from digitorn.core.runtime.hooks import (
                get_condition_params, get_action_params,
                all_condition_names, all_action_names,
            )
            if get_condition(hook.condition.type) is None:
                errors.append(
                    f"{ctx}.condition.type: Unknown condition '{hook.condition.type}'. "
                    f"Available: {all_condition_names()}"
                )

            if get_action(hook.action.type) is None:
                errors.append(
                    f"{ctx}.action.type: Unknown action '{hook.action.type}'. "
                    f"Available: {all_action_names()}"
                )

            condition_params = {
                k: v for k, v in hook.condition.model_dump().items()
                if k != "type"
            }

            action_params = {
                k: v for k, v in hook.action.model_dump().items()
                if k != "type"
            }

            _validate_plugin_params(
                errors, f"{ctx}.condition", hook.condition.type,
                condition_params, get_condition_params(hook.condition.type),
            )
            _validate_plugin_params(
                errors, f"{ctx}.action", hook.action.type,
                action_params, get_action_params(hook.action.type),
            )

            compiled_hooks.append(CompiledHook(
                id=hook.id,
                on=hook.on,
                condition_type=hook.condition.type,
                condition_params=condition_params,
                action_type=hook.action.type,
                action_params=action_params,
                cooldown=hook.cooldown,
                max_fires=getattr(hook, "max_fires", 0),
                priority=getattr(hook, "priority", 100),
                enabled=getattr(hook, "enabled", True),
                tags=list(getattr(hook, "tags", []) or []),
                timeout=float(getattr(hook, "timeout", 30.0) or 30.0),
            ))

        return compiled_hooks

    def _compile_agent_hooks(
        self,
        agent_id: str,
        hooks: list[Any],
        errors: list[str],
    ) -> list[CompiledHook]:
        """Compile per-agent hooks - stamps ``agent_id`` on each.

        Reuses the same validation rules as ``_compile_hooks`` but tags
        each compiled hook with its owning agent so the runtime filter
        (see ``HookRunner.run``) only fires them for that agent's turns.
        """
        if not hooks:
            return []

        from digitorn.core.runtime.hooks import get_condition, get_action
        import digitorn.core.runtime.tool_hooks  # noqa: F401

        compiled: list[CompiledHook] = []
        seen_ids: set[str] = set()
        valid_events = {
            "turn_start", "turn_end",
            "tool_start", "tool_end",
            "pre_tool_use", "post_tool_use",
            "user_prompt",
            "session_start", "session_end",
            "pre_compact",
            "error",
            "approval_request",
            "agent_spawn", "agent_complete",
            "activation",
        }

        for i, hook in enumerate(hooks):
            ctx = f"agents[{agent_id}].hooks[{i}]"
            if hook.id in seen_ids:
                errors.append(f"{ctx}: Duplicate hook id '{hook.id}'")
                continue
            seen_ids.add(hook.id)

            if hook.on not in valid_events:
                errors.append(
                    f"{ctx}.on: Invalid event '{hook.on}'. "
                    f"Must be one of: {sorted(valid_events)}"
                )
            if get_condition(hook.condition.type) is None:
                errors.append(
                    f"{ctx}.condition.type: Unknown condition "
                    f"'{hook.condition.type}'"
                )
            if get_action(hook.action.type) is None:
                errors.append(
                    f"{ctx}.action.type: Unknown action "
                    f"'{hook.action.type}'"
                )

            condition_params = {
                k: v for k, v in hook.condition.model_dump().items()
                if k != "type"
            }
            action_params = {
                k: v for k, v in hook.action.model_dump().items()
                if k != "type"
            }

            from digitorn.core.runtime.hooks import (
                get_condition_params, get_action_params,
            )
            _validate_plugin_params(
                errors, f"{ctx}.condition", hook.condition.type,
                condition_params, get_condition_params(hook.condition.type),
            )
            _validate_plugin_params(
                errors, f"{ctx}.action", hook.action.type,
                action_params, get_action_params(hook.action.type),
            )

            compiled.append(CompiledHook(
                id=hook.id,
                on=hook.on,
                condition_type=hook.condition.type,
                condition_params=condition_params,
                action_type=hook.action.type,
                action_params=action_params,
                cooldown=hook.cooldown,
                max_fires=getattr(hook, "max_fires", 0),
                priority=getattr(hook, "priority", 100),
                enabled=getattr(hook, "enabled", True),
                tags=list(getattr(hook, "tags", []) or []),
                agent_id=agent_id,
                timeout=float(getattr(hook, "timeout", 30.0) or 30.0),
            ))

        return compiled

    def _compile_channels(
        self,
        definition: AppDefinition,
        errors: list[str],
    ) -> dict[str, "CompiledChannelInstance"]:
        """Compile the channels block - resolve variables in channel configs."""
        compiled: dict[str, CompiledChannelInstance] = {}

        if not definition.tools.channels:
            # Still validate default_channel even without a channels block
            default = definition.runtime.default_channel
            if default != "llm_notification":
                errors.append(
                    f"execution.default_channel: '{default}' not found in "
                    f"channels block (no channels declared)"
                )
            return compiled

        _BUILTIN_CHANNEL_TYPES = {
            "llm_notification", "webhook", "log", "gmail",
            "telegram", "sms", "slack", "email", "hook",
        }
        for name, channel_cfg in definition.tools.channels.items():
            if channel_cfg.type not in _BUILTIN_CHANNEL_TYPES:
                import difflib as _df
                sug = _df.get_close_matches(
                    channel_cfg.type, _BUILTIN_CHANNEL_TYPES, n=3, cutoff=0.6,
                )
                hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
                errors.append(
                    f"channels.{name}.type: unknown channel type "
                    f"'{channel_cfg.type}'. Built-in: "
                    f"{sorted(_BUILTIN_CHANNEL_TYPES)}.{hint}"
                )
                continue
            try:
                resolved_config = resolve_variables(
                    channel_cfg.config, definition.dev.variables,
                    secrets=self._secrets,
                )
            except ValueError as exc:
                errors.append(f"channels.{name}.config: {exc}")
                continue

            resolver_dict: dict[str, Any] | None = None
            if channel_cfg.user_resolver is not None:
                resolver_dict = channel_cfg.user_resolver.model_dump()

            compiled[name] = CompiledChannelInstance(
                instance_name=name,
                channel_type=channel_cfg.type,
                config=resolved_config,
                user_resolver=resolver_dict,
            )

        default = definition.runtime.default_channel
        if default != "llm_notification" and default not in compiled:
            errors.append(
                f"execution.default_channel: '{default}' not found in "
                f"channels block (available: {sorted(compiled.keys())})"
            )

        return compiled

    def _check_actions_exist(
        self, context: str, module_id: str, actions: list[str], errors: list[str]
    ) -> None:
        """Check that action names exist on a module."""
        try:
            module = self._registry.get(module_id)
            manifest = module.get_manifest()
            known = set(manifest.action_names())
            for action in actions:
                if action not in known:
                    errors.append(
                        f"{context}: Action '{action}' not found on "
                        f"module '{module_id}' (available: {sorted(known)})"
                    )
        except Exception as exc:
            logger.warning(
                "Action check failed for module '%s': %s", module_id, exc,
            )

    def _build_security_profile(
        self,
        definition: AppDefinition,
        errors: list[str] | None = None,
    ) -> SecurityProfile:
        """Build a SecurityProfile from the capabilities section.

        May be called with ``definition.tools.capabilities = None`` if only
        module-level constraints (allowed/blocked actions) are present.
        """
        caps = definition.tools.capabilities
        module_grants: dict[str, ModuleGrant] = {}

        granted_permissions: set[str] = set()

        default_mod_policy = (caps.default_policy if caps else "auto")
        if default_mod_policy == "auto":
            default_mod_policy = "approve"

        for grant in (caps.grant if caps else []):
            mid = grant.module
            existing = module_grants.get(mid)
            overrides = dict(existing.action_overrides) if existing else {}
            # Preserve any previously-recorded allowed_actions and extend
            # with the new grant's actions (if any were listed).
            allowed = set(existing.allowed_actions) if existing else set()

            for action in grant.actions:
                overrides[action] = "auto"
                allowed.add(action)

            module_grants[mid] = ModuleGrant(
                module_id=mid,
                visibility="full",
                default_action_policy=existing.default_action_policy if existing else default_mod_policy,
                action_overrides=overrides,
                allowed_actions=frozenset(allowed),
            )

            for action in grant.actions:
                granted_permissions.add(f"{mid}:{action}")

        for entry in (caps.approve if caps else []):
            mid = entry.module
            existing = module_grants.get(mid)
            overrides = dict(existing.action_overrides) if existing else {}

            for action in entry.actions:
                overrides[action] = "approve"

            module_grants[mid] = ModuleGrant(
                module_id=mid,
                visibility=existing.visibility if existing else "full",
                default_action_policy=existing.default_action_policy if existing else "approve",
                action_overrides=overrides,
                allowed_actions=existing.allowed_actions if existing else frozenset(),
            )

        for deny in (caps.deny if caps else []):
            mid = deny.module
            existing = module_grants.get(mid)
            overrides = dict(existing.action_overrides) if existing else {}

            for action in deny.actions:
                overrides[action] = "block"

            module_grants[mid] = ModuleGrant(
                module_id=mid,
                visibility=existing.visibility if existing else "full",
                default_action_policy=existing.default_action_policy if existing else "approve",
                action_overrides=overrides,
                allowed_actions=existing.allowed_actions if existing else frozenset(),
            )

        for module_id, block in definition.tools.modules.items():
            allowed = block.constraints.get("allowed_actions")
            blocked = block.constraints.get("blocked_actions")

            if allowed or blocked:
                existing = module_grants.get(module_id)
                overrides = dict(existing.action_overrides) if existing else {}

                if allowed:
                    if not isinstance(allowed, list):
                        # Already caught by _validate_constraints, skip iteration
                        allowed = None
                if allowed:
                    try:
                        module = self._registry.get(module_id)
                        manifest = module.get_manifest()
                        all_actions = set(manifest.action_names())

                        unknown = set(allowed) - all_actions
                        if unknown and errors is not None:
                            errors.append(
                                f"modules.{module_id}.constraints: "
                                f"unknown action(s) in allowed_actions: "
                                f"{sorted(unknown)}. "
                                f"Available: {sorted(all_actions)}"
                            )

                        for action in all_actions:
                            if action in allowed:
                                if action not in overrides:
                                    overrides[action] = "auto"
                            elif action not in overrides:
                                overrides[action] = "block"
                    except Exception as exc:
                        logger.warning(
                            "Security profile: failed to resolve module '%s': %s",
                            module_id, exc,
                        )

                if blocked:
                    if not isinstance(blocked, list):
                        blocked = None
                if blocked:
                    for action in blocked:
                        overrides[action] = "block"

                module_grants[module_id] = ModuleGrant(
                    module_id=module_id,
                    visibility=existing.visibility if existing else "full",
                    default_action_policy=(
                        existing.default_action_policy if existing else "approve"
                    ),
                    action_overrides=overrides,
                    allowed_actions=existing.allowed_actions if existing else frozenset(),
                )

        for mid in (caps.hidden_modules if caps else []):
            existing = module_grants.get(mid)
            module_grants[mid] = ModuleGrant(
                module_id=mid,
                visibility="hidden",
                default_action_policy=(
                    existing.default_action_policy if existing else "block"
                ),
                action_overrides=dict(existing.action_overrides) if existing else {},
                allowed_actions=existing.allowed_actions if existing else frozenset(),
            )

        for entry in (caps.hidden_actions if caps else []):
            mid = entry.module
            existing = module_grants.get(mid)
            new_hidden = set(existing.hidden_actions) if existing else set()
            new_hidden.update(entry.actions)
            module_grants[mid] = ModuleGrant(
                module_id=mid,
                visibility=existing.visibility if existing else "full",
                default_action_policy=(
                    existing.default_action_policy if existing else "approve"
                ),
                action_overrides=dict(existing.action_overrides) if existing else {},
                hidden_actions=frozenset(new_hidden),
                allowed_actions=existing.allowed_actions if existing else frozenset(),
            )

        for sys_mod in ("context_builder", "llm_provider", "index"):
            existing = module_grants.get(sys_mod)
            # Preserve explicitly granted action_overrides from capabilities.grant.
            # This allows apps to expose specific actions (e.g. ask_user) from
            # system modules that are otherwise hidden.
            module_grants[sys_mod] = ModuleGrant(
                module_id=sys_mod,
                visibility="hidden",
                default_action_policy="auto",
                system_module=True,
                action_overrides=dict(existing.action_overrides) if existing and existing.action_overrides else {},
            )

        default_policy = caps.default_policy if caps else "auto"
        max_risk_level = caps.max_risk_level if caps else "high"

        if default_policy in ("grant", "auto"):
            risk_rules = {"low": "auto", "medium": "auto", "high": "auto"}
        elif default_policy == "approve":
            risk_rules = {"low": "auto", "medium": "approve", "high": "approve"}
        else:
            risk_rules = {}

        approval_timeout = float(caps.approval_timeout) if caps else 300.0

        return SecurityProfile(
            app_id=definition.app.app_id,
            is_active=True,
            default_policy=default_policy,
            granted_permissions=frozenset(granted_permissions),
            max_risk_level=max_risk_level,
            risk_approval_rules=risk_rules,
            module_grants=module_grants,
            approval_timeout=approval_timeout,
        )

"""Index builder - modules + security profile → ToolIndex."""

from __future__ import annotations

import logging
import re
import threading
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

from digitorn.modules.context_builder.scoring import tokenize_for_index
from digitorn.modules.context_builder.tool_schema import action_entry_to_json_schema
from digitorn.core.runtime.strict_schema import normalize_strict_schema
from digitorn.modules.context_builder.types import (
    CategoryInfo,
    Decision,
    IndexedTool,
    ToolIndex,
)

if TYPE_CHECKING:
    from digitorn.core.security import SecurityProfile
    from digitorn.modules.base import BaseModule

_HIDDEN_MODULES = frozenset({"context_builder", "mcp", "llm_provider", "index"})

# Small LRU cache of recently built indices, keyed on a structural
# fingerprint. Protected by _BUILD_CACHE_LOCK against multi-thread races.
_BUILD_CACHE: dict[tuple, Any] = {}
_BUILD_CACHE_MAX = 8
_BUILD_CACHE_LOCK = threading.RLock()

def _index_cache_key(
    modules: dict[str, BaseModule],
    security_profile: SecurityProfile | None,
    skip_embeddings: bool,
) -> tuple | None:
    if "mcp" in modules:
        # MCP servers can come/go - never cache.
        return None
    try:
        sp_id = id(security_profile) if security_profile is not None else 0
        mod_fingerprint = tuple(sorted((mid, id(m)) for mid, m in modules.items()))
        return (len(modules), mod_fingerprint, sp_id, bool(skip_embeddings))
    except Exception:
        return None

def build_index(
    modules: dict[str, BaseModule],
    security_profile: SecurityProfile | None = None,
    skip_embeddings: bool = False,
    action_filter: dict[str, list[str]] | None = None,
) -> ToolIndex:
    """Build a ToolIndex from live module instances."""
    cache_key = _index_cache_key(modules, security_profile, skip_embeddings) if not action_filter else None
    if cache_key is not None:
        with _BUILD_CACHE_LOCK:
            cached = _BUILD_CACHE.get(cache_key)
            if cached is not None:
                return cached

    index = ToolIndex()

    # Explicit grants for hidden modules (e.g. context_builder.ask_user)
    # must still be indexed.
    _explicitly_granted_hidden: dict[str, set[str]] = {}
    if security_profile:
        for mid in _HIDDEN_MODULES:
            grant = security_profile.module_grants.get(mid)
            if grant is not None and grant.visibility != "hidden":
                _explicitly_granted_hidden[mid] = set()  # empty = all actions
            elif grant is not None and hasattr(grant, "action_overrides") and grant.action_overrides:
                _explicitly_granted_hidden[mid] = set(grant.action_overrides.keys())

    for module_id, module in modules.items():
        if module_id in _HIDDEN_MODULES:
            if module_id not in _explicitly_granted_hidden:
                continue
            _index_module(
                index, module_id, module, security_profile,
                only_actions=_explicitly_granted_hidden.get(module_id) or None,
            )
            continue

        if security_profile and not security_profile.is_module_visible(module_id):
            continue

        _only = None
        if action_filter and module_id in action_filter:
            _only = set(action_filter[module_id])

        _index_module(index, module_id, module, security_profile, only_actions=_only)

    mcp_module = modules.get("mcp")
    if mcp_module is not None:
        _index_mcp_servers(index, mcp_module, security_profile)

    if not skip_embeddings:
        # fault-tolerant -- ONNX model build can fail under RAM
        # pressure; fall back to keyword search rather than crash.
        try:
            from digitorn.modules.context_builder.embeddings import SemanticIndex
            index.semantic_index = SemanticIndex.build(index)
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).warning(
                "semantic_index_build_failed: %s - falling back to keyword "
                "search. Set `discovery.skip_embeddings: true` in "
                "~/.digitorn/config.yaml to silence this warning.",
                exc,
            )
            index.semantic_index = None

    if cache_key is not None:
        with _BUILD_CACHE_LOCK:
            _BUILD_CACHE[cache_key] = index
            while len(_BUILD_CACHE) > _BUILD_CACHE_MAX:
                first_key = next(iter(_BUILD_CACHE))
                _BUILD_CACHE.pop(first_key, None)

    return index

from digitorn.core.runtime.tool_names import to_short, to_fqn

resolve_short_name = to_fqn

def _short_tool_name(fqn: str, tool: IndexedTool) -> str:
    return to_short(fqn)

def _schema_has_open_dict(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    ap = schema.get("additionalProperties")
    if isinstance(ap, dict):
        return True
    if ap is True:
        return True
    for key in ("properties", "patternProperties", "$defs", "definitions",
                "dependentSchemas"):
        block = schema.get(key)
        if isinstance(block, dict):
            for v in block.values():
                if _schema_has_open_dict(v):
                    return True
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        block = schema.get(key)
        if isinstance(block, list):
            for v in block:
                if _schema_has_open_dict(v):
                    return True
    for key in ("not", "if", "then", "else", "contains", "propertyNames"):
        if _schema_has_open_dict(schema.get(key)):
            return True
    items = schema.get("items")
    if isinstance(items, dict) and _schema_has_open_dict(items):
        return True
    if isinstance(items, list):
        for it in items:
            if _schema_has_open_dict(it):
                return True
    return False

def build_direct_tools(
    index: ToolIndex,
    *,
    inject_intent: bool = False,
) -> list[dict[str, Any]]:
    """Build OpenAI-format tool schemas for all visible tools."""
    from digitorn.modules.context_builder.tool_schema import (
        inject_intent_field,
    )
    tools: list[dict[str, Any]] = []
    for fqn, tool in index.tools.items():
        if getattr(tool, "hidden", False):
            continue
        schema = dict(tool.params_schema)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})

        if inject_intent:
            schema = inject_intent_field(schema)

        # detect open-dict BEFORE normalize wipes it to false;
        # OpenAI strict 400s on open-dict patterns.
        has_open_dict = _schema_has_open_dict(schema)

        schema["additionalProperties"] = False
        normalize_strict_schema(schema)

        api_name = _short_tool_name(fqn, tool)

        func_def: dict[str, Any] = {
            "name": api_name,
            "description": tool.description,
            "parameters": schema,
        }

        # drop strict only when open-dict was detected (OpenAI 400s on it).
        func_def["strict"] = not has_open_dict

        tools.append({
            "type": "function",
            "function": func_def,
        })

    # Boot-time self-check: log any strict-mode violation we missed.
    from digitorn.core.runtime.strict_schema import assert_strict_tools
    violations = assert_strict_tools(tools)
    if violations:
        for tool_name, path, reason in violations[:20]:
            logger.error(
                "strict_schema_post_build_violation tool=%s path=%s reason=%s "
                "(this tool will 400 the gateway -- extend the strict_schema "
                "walker before next deploy)",
                tool_name, ".".join(path) or "<root>", reason,
            )
        if len(violations) > 20:
            logger.error(
                "strict_schema_post_build_violation +%d more violations suppressed",
                len(violations) - 20,
            )

    return tools

def _index_module(
    index: ToolIndex,
    module_id: str,
    module: BaseModule,
    security_profile: SecurityProfile | None,
    only_actions: set[str] | None = None,
) -> None:
    action_registry: dict[str, Any] = getattr(module, "_action_registry", {})
    if not action_registry:
        return

    try:
        manifest = module.get_manifest()
        module_description = manifest.description or f"Module: {module_id}"
    except Exception as exc:
        logger.warning(
            "context_builder: get_manifest failed for module=%s: %s",
            module_id, exc,
        )
        module_description = f"Module: {module_id}"

    category_tools: list[str] = []

    for action_name, entry in action_registry.items():
        if only_actions is not None and action_name not in only_actions:
            continue
        spec = entry.spec

        # internal actions stay callable via bus.call() but are
        # hidden from the LLM discovery surface.
        if getattr(spec, "internal", False):
            continue

        decision = _resolve_decision(
            security_profile, module_id, action_name, spec.risk_level,
            require_approval=spec.require_approval,
        )
        if decision == Decision.BLOCK:
            continue

        is_hidden = False
        if security_profile is not None:
            grant = security_profile.module_grants.get(module_id)
            if grant is not None and grant.is_action_hidden(action_name):
                is_hidden = True

        fqn = f"{module_id}.{action_name}"

        try:
            params_schema = action_entry_to_json_schema(entry)
        except Exception as exc:
            logger.warning(
                "context_builder: action_entry_to_json_schema failed for %s.%s: %s",
                module_id, action_name, exc,
            )
            params_schema = {"type": "object", "properties": {}, "required": []}

        tool = IndexedTool(
            fqn=fqn,
            module_id=module_id,
            action_name=action_name,
            description=spec.description or "",
            risk_level=spec.risk_level or "low",
            tags=list(spec.tags or []),
            params_schema=params_schema,
            examples=list(spec.examples or []),
            module=module,
            policy_decision=decision,
            aliases=list(spec.aliases or []),
            side_effects=list(spec.side_effects or []),
            irreversible=spec.irreversible,
            output_schema=getattr(spec, "output_schema", None),
            tool_prompt=getattr(spec, "tool_prompt", "") or "",
            hidden=is_hidden,
        )

        index.tools[fqn] = tool

        if is_hidden:
            continue

        category_tools.append(fqn)

        _index_keywords(index, fqn, tool)

        for tag in tool.tags:
            tag_lower = tag.lower()
            index.tag_index.setdefault(tag_lower, set()).add(fqn)

        for fqn_token in tokenize_for_index(fqn):
            index.fqn_index.setdefault(fqn_token, set()).add(fqn)

    if category_tools:
        index.categories[module_id] = CategoryInfo(
            module_id=module_id,
            summary=module_description,
            tool_count=len(category_tools),
            tool_names=category_tools,
        )

def _index_keywords(index: ToolIndex, fqn: str, tool: IndexedTool) -> None:
    text_parts = [
        tool.description,
        tool.action_name,
        tool.module_id,
        " ".join(tool.tags),
        " ".join(tool.aliases),
    ]
    text = " ".join(text_parts)
    tokens = tokenize_for_index(text)

    for token in tokens:
        index.keyword_index.setdefault(token, set()).add(fqn)

        if len(token) >= 3:
            prefix = token[:3]
            index.keyword_prefixes.setdefault(prefix, set()).add(token)

def _resolve_decision(
    security_profile: SecurityProfile | None,
    module_id: str,
    action_name: str,
    risk_level: str,
    *,
    require_approval: bool = False,
) -> Decision:
    if require_approval:
        return Decision.APPROVE

    if security_profile is None:
        return Decision.AUTO

    from digitorn.core.security import resolve_action_policy

    policy = resolve_action_policy(security_profile, module_id, action_name, risk_level)
    if policy == "block":
        return Decision.BLOCK
    if policy == "approve":
        return Decision.APPROVE
    return Decision.AUTO

def _index_mcp_servers(
    index: ToolIndex,
    mcp_module: BaseModule,
    security_profile: SecurityProfile | None,
) -> None:
    pool = getattr(mcp_module, "_pool", None)
    if pool is None:
        return

    servers = getattr(pool, "_servers", None) or {}
    if not isinstance(servers, dict):
        logger.warning("mcp_index_invalid: _servers is not a dict: %s", type(servers))
        return
    for server_id, entry in servers.items():
        is_degraded = entry.status != "connected"

        if is_degraded and not entry.tools:
            continue

        virtual_module_id = f"mcp_{server_id}"

        if security_profile and not security_profile.can_access_module(virtual_module_id):
            continue

        category_tools: list[str] = []

        for mcp_tool in entry.tools:
            tool_risk = _infer_mcp_tool_risk(mcp_tool.name)

            decision = _resolve_decision(
                security_profile,
                virtual_module_id,
                mcp_tool.name,
                tool_risk,
            )
            if decision == Decision.BLOCK:
                continue

            fqn = f"{virtual_module_id}.{mcp_tool.name}"

            mcp_action_name = f"{virtual_module_id}__{mcp_tool.name}"

            params_schema = dict(mcp_tool.input_schema) if mcp_tool.input_schema else {}
            params_schema.setdefault("type", "object")
            params_schema.setdefault("properties", {})

            yaml_examples = getattr(entry, "tool_examples", None) or {}
            examples = yaml_examples.get(mcp_tool.name, []) if isinstance(yaml_examples, dict) else []
            if not examples:
                examples = _extract_mcp_examples(mcp_tool, params_schema)

            desc = mcp_tool.description or f"MCP tool: {mcp_tool.name}"
            if tool_risk == "high":
                desc = f"[DESTRUCTIVE - external MCP] {desc}"

            tool = IndexedTool(
                fqn=fqn,
                module_id=virtual_module_id,
                action_name=mcp_action_name,
                description=desc,
                risk_level=tool_risk,
                tags=["mcp", server_id],
                params_schema=params_schema,
                examples=examples,
                module=mcp_module,
                policy_decision=decision,
                aliases=_generate_mcp_aliases(mcp_tool.name),
                side_effects=["external_api_call"],
                irreversible=tool_risk == "high",
            )

            index.tools[fqn] = tool
            category_tools.append(fqn)
            _index_keywords(index, fqn, tool)

            for tag in tool.tags:
                index.tag_index.setdefault(tag.lower(), set()).add(fqn)

            for fqn_token in tokenize_for_index(fqn):
                index.fqn_index.setdefault(fqn_token, set()).add(fqn)

        if category_tools:
            server_info = getattr(entry, "server_info", {})
            server_name = server_info.get("name", server_id) if server_info else server_id
            status_hint = " [DISCONNECTED - will auto-reconnect]" if is_degraded else ""
            index.categories[virtual_module_id] = CategoryInfo(
                module_id=virtual_module_id,
                summary=f"MCP server: {server_name} ({len(category_tools)} tools){status_hint}",
                tool_count=len(category_tools),
                tool_names=category_tools,
            )

_HIGH_RISK_PATTERNS = re.compile(
    # Only truly destructive verbs are high-risk + irreversible.
    r"(?:^|_)(?:delete|drop|destroy|remove|kill|"
    r"batch_delete|batch_remove)(?:_|$)",
    re.IGNORECASE,
)
_LOW_RISK_PATTERNS = re.compile(
    r"(?:^|_)(?:get|list|search|read|describe|count|fetch|check|"
    r"browse|view|show|info|status|ping|health)(?:_|$)",
    re.IGNORECASE,
)

def _infer_mcp_tool_risk(tool_name: str) -> str:
    if _HIGH_RISK_PATTERNS.search(tool_name):
        return "high"
    if _LOW_RISK_PATTERNS.search(tool_name):
        return "low"
    return "medium"

_MCP_VERB_ALIASES: dict[str, list[str]] = {
    "create": ["créer", "ajouter", "nouveau", "add", "new"],
    "list": ["lister", "afficher", "voir", "show", "enumerate"],
    "get": ["obtenir", "récupérer", "voir", "fetch", "retrieve"],
    "search": ["chercher", "rechercher", "trouver", "find", "query"],
    "update": ["modifier", "mettre_à_jour", "éditer", "edit", "modify"],
    "delete": ["supprimer", "effacer", "retirer", "remove", "drop"],
    "read": ["lire", "consulter", "afficher", "view", "show"],
    "write": ["écrire", "rédiger", "sauvegarder", "save"],
    "send": ["envoyer", "transmettre", "poster", "post"],
    "query": ["interroger", "requêter", "chercher", "search"],
    "append": ["ajouter", "insérer", "insert", "add"],
    "describe": ["décrire", "détailler", "schema", "info"],
    "connect": ["connecter", "relier", "join"],
    "disconnect": ["déconnecter", "fermer", "close"],
    "commit": ["valider", "enregistrer", "sauvegarder", "save"],
    "diff": ["comparer", "différence", "compare"],
    "log": ["historique", "journal", "history"],
    "status": ["état", "statut", "info"],
    "checkout": ["basculer", "changer", "switch"],
    "push": ["pousser", "publier", "publish"],
    "pull": ["tirer", "récupérer", "fetch"],
    "fetch": ["télécharger", "récupérer", "download", "get"],
    "browse": ["parcourir", "explorer", "navigate"],
    "upload": ["téléverser", "envoyer", "send"],
    "download": ["télécharger", "récupérer", "fetch"],
    "close": ["fermer", "clore", "terminer", "end"],
}

def _generate_mcp_aliases(tool_name: str) -> list[str]:
    aliases: list[str] = []
    parts = tool_name.lower().split("_")

    if parts and parts[0] in _MCP_VERB_ALIASES:
        verb = parts[0]
        rest = "_".join(parts[1:]) if len(parts) > 1 else ""
        for synonym in _MCP_VERB_ALIASES[verb]:
            if rest:
                aliases.append(f"{synonym}_{rest}")
            else:
                aliases.append(synonym)

    if len(parts) > 1 and parts[-1] in _MCP_VERB_ALIASES:
        verb = parts[-1]
        prefix = "_".join(parts[:-1])
        for synonym in _MCP_VERB_ALIASES[verb]:
            aliases.append(f"{prefix}_{synonym}")

    spaced = tool_name.replace("_", " ")
    if spaced != tool_name:
        aliases.append(spaced)

    seen: set[str] = set()
    unique: list[str] = []
    for a in aliases:
        if a not in seen and a != tool_name:
            seen.add(a)
            unique.append(a)
    return unique

_EXAMPLE_VALUES: dict[str, Any] = {
    "string": "example",
    "integer": 1,
    "number": 1.0,
    "boolean": True,
    "array": [],
    "object": {},
}

def _extract_mcp_examples(
    mcp_tool: Any,
    params_schema: dict[str, Any],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    schema_examples = params_schema.get("examples", [])
    if isinstance(schema_examples, list):
        for i, ex in enumerate(schema_examples):
            if isinstance(ex, dict):
                results.append({"name": f"schema_{i + 1}", "value": ex})

    meta = getattr(mcp_tool, "meta", None) or {}
    meta_examples = meta.get("examples", [])
    if isinstance(meta_examples, list):
        for ex in meta_examples:
            if isinstance(ex, dict):
                name = ex.get("name", "meta")
                value = ex.get("value") or ex.get("arguments")
                if isinstance(value, dict):
                    results.append({"name": name, "value": value})

    annotations = getattr(mcp_tool, "annotations", None) or {}
    ann_examples = annotations.get("examples", [])
    if isinstance(ann_examples, list):
        for ex in ann_examples:
            if isinstance(ex, dict):
                name = ex.get("name", "annotation")
                value = ex.get("value") or ex.get("arguments")
                if isinstance(value, dict):
                    results.append({"name": name, "value": value})

    if results:
        return results[:3]

    return _auto_examples_from_schema(params_schema)

def _auto_examples_from_schema(
    schema: dict[str, Any],
) -> list[dict[str, Any]]:
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    if not props:
        return []

    example: dict[str, Any] = {}
    for name, prop in props.items():
        if name not in required:
            continue
        if "examples" in prop and prop["examples"]:
            example[name] = prop["examples"][0]
        elif "default" in prop:
            example[name] = prop["default"]
        elif "enum" in prop and prop["enum"]:
            example[name] = prop["enum"][0]
        else:
            example[name] = _smart_placeholder(name, prop)

    if not example:
        return []
    return [{"name": "basic", "value": example}]

def _smart_placeholder(name: str, prop: dict[str, Any]) -> Any:
    ptype = prop.get("type", "string")
    if "anyOf" in prop:
        for option in prop["anyOf"]:
            if option.get("type") != "null":
                ptype = option.get("type", "string")
                break

    if ptype != "string":
        return _EXAMPLE_VALUES.get(ptype, "...")

    lower = name.lower()
    if lower.endswith("_json"):
        return "{}"
    desc = (prop.get("description") or "").lower()
    if desc and any(kw in desc for kw in ("json string", "json object", "json array", "serialized json", "json-encoded")):
        return "{}"
    if lower.endswith("_id") or lower == "id":
        return "<id>"
    if "url" in lower or "uri" in lower:
        return "https://..."
    if "path" in lower:
        return "/path/to/resource"
    if "query" in lower or "search" in lower:
        return ""
    if "name" in lower:
        return "my-name"
    return "..."

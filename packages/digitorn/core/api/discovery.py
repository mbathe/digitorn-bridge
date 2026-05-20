"""Discovery API - meta-routes for the App Builder agent."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from digitorn.core.api.apps_v2 import AppResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/discovery", tags=["discovery"])


def _templates_dir() -> Path:
    """Locate the `knowledge_base/examples` directory."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "knowledge_base" / "examples"
        if candidate.is_dir():
            return candidate
    # Fallback: assume the repo layout we ship today
    return here.parents[4] / "knowledge_base" / "examples"


# Dependencies


def _get_registry(request: Request):
    """Pull the live `ModuleRegistry` off `app.state`."""
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(
            status_code=503,
            detail="Module registry not initialized",
        )
    return registry


# /modules + /modules/{id}


def _module_summary(module_id: str, manifest: Any) -> dict[str, Any]:
    actions_attr = getattr(manifest, "actions", None) or []
    if isinstance(actions_attr, dict):
        actions_list = list(actions_attr.values())
    else:
        actions_list = list(actions_attr)
    return {
        "id": module_id,
        "name": getattr(manifest, "name", module_id),
        "version": getattr(manifest, "version", ""),
        "description": getattr(manifest, "description", "") or "",
        "category": getattr(manifest, "category", "") or "",
        "action_count": sum(
            1 for a in actions_list
            if not getattr(a, "internal", False)
        ),
    }


def _action_detail(spec: Any) -> dict[str, Any]:
    params = []
    for p in (getattr(spec, "params", None) or []):
        params.append({
            "name": getattr(p, "name", ""),
            "type": getattr(p, "type", "") or "",
            "required": bool(getattr(p, "required", False)),
            "default": getattr(p, "default", None),
            "description": getattr(p, "description", "") or "",
            "enum": list(getattr(p, "enum", []) or []) or None,
        })
    return {
        "name": getattr(spec, "name", ""),
        "description": getattr(spec, "description", "") or "",
        "tool_prompt": getattr(spec, "tool_prompt", "") or "",
        "params": params,
        "permissions": list(getattr(spec, "permissions", []) or []),
        "risk_level": getattr(spec, "risk_level", "") or "unknown",
        "irreversible": bool(getattr(spec, "irreversible", False)),
        "require_approval": bool(getattr(spec, "require_approval", False)),
        "tags": list(getattr(spec, "tags", []) or []),
        "aliases": list(getattr(spec, "aliases", []) or []),
        "internal": bool(getattr(spec, "internal", False)),
    }


@router.get("/modules", response_model=AppResponse)
async def list_modules(request: Request) -> AppResponse:
    """List every loaded module with a short summary."""
    registry = _get_registry(request)

    list_available = (
        getattr(registry, "list_available", None) or registry.list_modules
    )
    summaries: list[dict[str, Any]] = []
    for module_id in sorted(list_available()):
        try:
            manifest = registry.get_manifest(module_id)
        except Exception as exc:
            logger.debug("discovery: skip %s (manifest failed: %s)", module_id, exc)
            continue
        if manifest is None:
            continue
        summaries.append(_module_summary(module_id, manifest))

    return AppResponse(
        success=True,
        data={"modules": summaries, "count": len(summaries)},
    )


@router.get("/modules/{module_id}", response_model=AppResponse)
async def get_module(request: Request, module_id: str) -> AppResponse:
    """Return the full action surface of one module."""
    registry = _get_registry(request)
    try:
        manifest = registry.get_manifest(module_id)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Module '{module_id}' not found: {exc}",
        )
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"Module '{module_id}' not found")

    actions_attr = getattr(manifest, "actions", None) or []
    if isinstance(actions_attr, dict):
        iterable = list(actions_attr.values())
    else:
        iterable = list(actions_attr)

    actions = [
        _action_detail(spec)
        for spec in iterable
        if not bool(getattr(spec, "internal", False))
    ]

    return AppResponse(
        success=True,
        data={
            "id": module_id,
            "name": getattr(manifest, "name", module_id),
            "version": getattr(manifest, "version", ""),
            "description": getattr(manifest, "description", "") or "",
            "category": getattr(manifest, "category", "") or "",
            "actions": actions,
            "action_count": len(actions),
        },
    )


# /triggers


_TRIGGER_TYPES: list[dict[str, Any]] = [
    {
        "name": "cron",
        "category": "scheduler",
        "io": "inbound",
        "description": "Fires the agent on a recurring schedule using standard 5-field cron syntax (croniter).",
        "key_fields": ["schedule", "routing", "message"],
        "example": (
            "triggers:\n"
            "  - id: hourly\n"
            "    type: cron\n"
            "    schedule: \"0 * * * *\"\n"
            "    routing: broadcast\n"
        ),
    },
    {
        "name": "watch",
        "category": "scheduler",
        "io": "inbound",
        "description": "Monitors a glob pattern on the filesystem and fires when a matching file is created or modified. (Modern equivalent: file_watcher channel adapter.)",
        "key_fields": ["pattern", "poll_interval"],
        "example": (
            "triggers:\n"
            "  - id: new-files\n"
            "    type: watch\n"
            "    pattern: \"./inbox/*.pdf\"\n"
        ),
    },
    {
        "name": "http",
        "category": "http",
        "io": "inbound",
        "description": "Lightweight HTTP endpoint (legacy). Prefer the webhook channel adapter for new apps.",
        "key_fields": ["path", "port", "method"],
        "example": (
            "triggers:\n"
            "  - id: hook\n"
            "    type: http\n"
            "    path: /hooks/incoming\n"
            "    port: 9100\n"
        ),
    },
    {
        "name": "rss",
        "category": "scheduler",
        "io": "inbound",
        "description": "Polls an RSS feed and fires when a new item appears.",
        "key_fields": ["url", "poll_interval"],
        "example": "",
    },
    {
        "name": "webhook",
        "category": "channel",
        "io": "bidirectional",
        "description": "HTTP webhook adapter with HMAC or API-key auth + outbound delivery. Used via modules.channels.config.providers.",
        "key_fields": ["inbound_path", "auth", "signature_secret"],
        "example": (
            "modules:\n"
            "  channels:\n"
            "    config:\n"
            "      providers:\n"
            "        my_hook:\n"
            "          adapter: webhook\n"
            "          config:\n"
            "            inbound_path: /hook/incoming\n"
            "            auth: signature\n"
            "            signature_secret: \"{{secret.WEBHOOK_SECRET}}\"\n"
        ),
    },
    {
        "name": "telegram",
        "category": "channel",
        "io": "bidirectional",
        "description": "Telegram bot - receives messages from chats and replies via the same bot token.",
        "key_fields": ["token", "poll_timeout", "allowed_chat_ids"],
        "example": (
            "modules:\n"
            "  channels:\n"
            "    config:\n"
            "      providers:\n"
            "        my_bot:\n"
            "          adapter: telegram\n"
            "          config:\n"
            "            token: \"{{secret.TELEGRAM_BOT_TOKEN}}\"\n"
        ),
    },
    {
        "name": "discord",
        "category": "channel",
        "io": "bidirectional",
        "description": "Discord bot - server messages and replies.",
        "key_fields": ["token", "guild_id"],
        "example": "",
    },
    {
        "name": "slack",
        "category": "channel",
        "io": "bidirectional",
        "description": "Slack bot - workspace channel messages and replies.",
        "key_fields": ["bot_token", "app_token"],
        "example": "",
    },
    {
        "name": "email",
        "category": "channel",
        "io": "bidirectional",
        "description": "IMAP polling for inbound mail + SMTP for outbound replies.",
        "key_fields": ["imap_host", "smtp_host", "user", "password"],
        "example": "",
    },
    {
        "name": "voice",
        "category": "channel",
        "io": "bidirectional",
        "description": "Telephony via Twilio with STT/TTS over WebSocket. Use for phone-call agents.",
        "key_fields": ["twilio_account_sid", "twilio_auth_token", "phone_number"],
        "example": "",
    },
    {
        "name": "queue",
        "category": "system",
        "io": "bidirectional",
        "description": "In-memory job queue. Useful for app-internal pipelines and background jobs.",
        "key_fields": ["queue_name"],
        "example": "",
    },
    {
        "name": "log",
        "category": "system",
        "io": "outbound",
        "description": "Writes agent output to the Python logger. Useful for audit trails and debug.",
        "key_fields": ["level"],
        "example": "",
    },
    {
        "name": "file_watcher",
        "category": "channel",
        "io": "inbound",
        "description": "Modern file-watcher channel adapter - replaces the legacy `type: watch` trigger.",
        "key_fields": ["paths", "poll_interval"],
        "example": (
            "modules:\n"
            "  channels:\n"
            "    config:\n"
            "      providers:\n"
            "        inbox:\n"
            "          adapter: file_watcher\n"
            "          config:\n"
            "            paths: [\"./inbox/*.pdf\"]\n"
            "            poll_interval: 5\n"
        ),
    },
]


@router.get("/triggers", response_model=AppResponse)
async def list_triggers(request: Request) -> AppResponse:
    """Return every supported trigger type with its category and key fields."""
    return AppResponse(
        success=True,
        data={
            "triggers": _TRIGGER_TYPES,
            "count": len(_TRIGGER_TYPES),
            "categories": sorted({t["category"] for t in _TRIGGER_TYPES}),
        },
    )


@router.get("/triggers/configured", response_model=AppResponse)
async def list_configured_triggers(request: Request) -> AppResponse:
    """Return every trigger declared by a currently-deployed app."""
    manager = getattr(request.app.state, "manager", None)
    if manager is None:
        return AppResponse(success=True, data={"triggers": [], "count": 0})

    uid = getattr(request.state, "user_id", "") or ""
    perms = getattr(request.state, "permissions", []) or []
    include_system = "*" in perms

    out: list[dict[str, Any]] = []
    try:
        deployed_map = getattr(manager, "_deployed", {}) or {}
    except Exception:
        deployed_map = {}

    for key, deployed in list(deployed_map.items()):
        if deployed is None:
            continue
        scope = "system" if key.startswith("system:") else "user"
        owner_uid = ""
        if key.startswith("user:"):
            parts = key.split(":", 2)
            owner_uid = parts[1] if len(parts) >= 2 else ""
        if scope == "system" and not include_system:
            continue
        if scope == "user" and uid and owner_uid and owner_uid != uid:
            continue
        compiled = getattr(deployed, "compiled", None)
        app_id = getattr(deployed, "app_id", "") or (
            getattr(compiled, "app_id", "") if compiled else ""
        )
        triggers = getattr(getattr(compiled, "execution", None), "triggers", None) or []
        for t in triggers:
            out.append({
                "app_id": app_id,
                "scope": scope,
                "trigger_id": getattr(t, "id", ""),
                "type": getattr(t, "type", ""),
                "schedule": getattr(t, "schedule", "") or "",
                "path": getattr(t, "path", "") or "",
                "method": getattr(t, "method", "") or "",
                "enabled": getattr(t, "enabled", True),
            })
    return AppResponse(
        success=True,
        data={"triggers": out, "count": len(out)},
    )


# /templates + /templates/{id}


def _parse_template_metadata(yaml_text: str) -> dict[str, str]:
    """Best-effort extraction of `app:` metadata from a raw YAML string."""
    import re

    fields: dict[str, str] = {}
    for key in ("app_id", "name", "description", "version", "icon", "color", "category"):
        m = re.search(rf'^\s*{key}:\s*"?([^"\n]+)"?\s*$', yaml_text, re.MULTILINE)
        if m:
            fields[key] = m.group(1).strip()
    # Tags are a list - extract on a single-line form like `tags: [a, b]`
    m = re.search(r'^\s*tags:\s*\[([^\]]*)\]', yaml_text, re.MULTILINE)
    if m:
        fields["tags"] = m.group(1)
    return fields


@router.get("/templates", response_model=AppResponse)
async def list_templates(request: Request) -> AppResponse:
    """List every starter template available in `knowledge_base/examples`."""
    tdir = _templates_dir()
    if not tdir.is_dir():
        return AppResponse(
            success=True,
            data={"templates": [], "count": 0},
        )

    def _read_all_templates() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in sorted(tdir.glob("*.yaml")):
            try:
                text = path.read_text(encoding="utf-8")
            except Exception as exc:
                logger.debug("discovery: skip template %s: %s", path.name, exc)
                continue
            meta = _parse_template_metadata(text)
            out.append({
                "id": path.stem,
                "filename": path.name,
                "path": str(path.relative_to(tdir.parent.parent)),
                "size_bytes": len(text.encode("utf-8")),
                "app_id": meta.get("app_id", ""),
                "name": meta.get("name", path.stem),
                "description": meta.get("description", ""),
                "version": meta.get("version", ""),
                "icon": meta.get("icon", ""),
                "color": meta.get("color", ""),
                "category": meta.get("category", ""),
                "tags": [t.strip() for t in meta.get("tags", "").split(",") if t.strip()],
            })
        return out

    templates = await asyncio.to_thread(_read_all_templates)

    return AppResponse(
        success=True,
        data={"templates": templates, "count": len(templates)},
    )


@router.get("/templates/{template_id}", response_model=AppResponse)
async def get_template(request: Request, template_id: str) -> AppResponse:
    """Return the raw YAML of one template by id."""
    # Sanitise - only allow filename-safe characters
    if not template_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid template id")

    tdir = _templates_dir()
    candidate = tdir / f"{template_id}.yaml"
    if not candidate.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Template '{template_id}' not found",
        )
    try:
        text = await asyncio.to_thread(
            candidate.read_text, encoding="utf-8",
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read template: {exc}")

    meta = _parse_template_metadata(text)
    return AppResponse(
        success=True,
        data={
            "id": template_id,
            "filename": candidate.name,
            "yaml": text,
            "size_bytes": len(text.encode("utf-8")),
            "metadata": meta,
        },
    )


# /compile  ←  THE feedback-loop tool for the builder agent


class CompileRequest(BaseModel):
    """Request body for POST /api/discovery/compile."""

    yaml: str = Field(..., description="The raw YAML text to compile.")
    source_path: str = Field(
        default="<inline>",
        description="Optional path label for error messages.",
    )


class GeneratePackageManifestRequest(BaseModel):
    """Request body for POST /api/discovery/generate-package-manifest."""

    yaml: str = Field(
        ...,
        description="The raw YAML text of an app to wrap into a package.",
    )
    publisher: str = Field(
        default="",
        description="Optional publisher id (used when publishing to the hub later).",
    )
    license: str = Field(
        default="",
        description="Optional SPDX license identifier.",
    )
    homepage: str = Field(
        default="",
        description="Optional homepage URL.",
    )


def _extract_graph(compiled: Any) -> dict[str, Any]:
    """Walk a `CompiledApp` and return `{nodes, edges}` for the canvas."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    try:
        execution = compiled.execution

        trigger_ids: list[str] = []
        for t in (getattr(execution, "triggers", []) or []):
            tid = getattr(t, "id", "") or "trigger"
            ttype = getattr(t, "type", "") or getattr(t, "trigger_type", "")
            schedule = getattr(t, "schedule", "") or getattr(t, "pattern", "") or ""
            label = f"{ttype} {schedule}".strip() if schedule else (ttype or tid)
            node_id = f"trigger:{tid}"
            nodes.append({
                "id": node_id,
                "type": "trigger",
                "subtype": ttype or "unknown",
                "label": label,
            })
            trigger_ids.append(node_id)

        # Channel providers (modern channel adapters) - read from the
        # compiled module config so we don't miss telegram/webhook/etc.
        try:
            channels_cfg = (
                (getattr(compiled, "modules", {}) or {})
                .get("channels", {}) or {}
            )
            ch_config = getattr(channels_cfg, "config", None) or channels_cfg
            providers = (
                ch_config.get("providers", {}) if isinstance(ch_config, dict) else {}
            )
            for prov_name, prov_cfg in (providers or {}).items():
                adapter = (prov_cfg or {}).get("adapter", "channel")
                node_id = f"trigger:channel-{prov_name}"
                nodes.append({
                    "id": node_id,
                    "type": "trigger",
                    "subtype": adapter,
                    "label": f"{adapter} ({prov_name})",
                })
                trigger_ids.append(node_id)
        except Exception as exc:
            logger.debug("discovery best-effort block failed: %s", exc)

        agent_ids: list[str] = []
        for a in (getattr(compiled, "agents", []) or []):
            aid = getattr(a, "agent_id", None) or getattr(a, "id", None) or "agent"
            role = getattr(a, "role", "") or ""
            label = aid + (f" ({role})" if role and role != aid else "")
            node_id = f"agent:{aid}"
            nodes.append({
                "id": node_id,
                "type": "agent",
                "subtype": role or "agent",
                "label": label,
            })
            agent_ids.append(node_id)

        module_ids_in_app = list((getattr(compiled, "modules", {}) or {}).keys())
        # Filter out the always-loaded plumbing modules - they exist
        # for every app and just create visual noise.
        _SILENT_MODULES = {"llm_provider", "index"}
        for mid in module_ids_in_app:
            if mid in _SILENT_MODULES:
                continue
            nodes.append({
                "id": f"module:{mid}",
                "type": "module",
                "subtype": mid,
                "label": mid,
            })

        # 1. trigger → entry_agent
        entry_agent = getattr(execution, "entry_agent", "") or ""
        entry_node = f"agent:{entry_agent}" if entry_agent else (
            agent_ids[0] if agent_ids else None
        )
        if entry_node:
            for tnode in trigger_ids:
                edges.append({
                    "from": tnode,
                    "to": entry_node,
                    "label": "fires",
                })

        for agent_node in agent_ids:
            for mid in module_ids_in_app:
                if mid in _SILENT_MODULES:
                    continue
                edges.append({
                    "from": agent_node,
                    "to": f"module:{mid}",
                    "label": "uses",
                })

        if "agent_spawn" in module_ids_in_app and entry_node and len(agent_ids) > 1:
            for other in agent_ids:
                if other == entry_node:
                    continue
                edges.append({
                    "from": entry_node,
                    "to": other,
                    "label": "spawns",
                })

    except Exception as exc:
        logger.debug("graph extraction failed: %s", exc)
        return {"nodes": nodes, "edges": edges, "error": str(exc)}

    return {"nodes": nodes, "edges": edges}


@router.post("/compile", response_model=AppResponse)
async def compile_yaml(request: Request, body: CompileRequest) -> AppResponse:
    """Compile a YAML app definition without deploying it."""
    registry = _get_registry(request)

    # Local import - keeps module-load overhead off the daemon's hot
    # paths when nobody is using the discovery API.
    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.core.app.errors import AppCompilationError

    compiler = AppYAMLCompiler(registry)

    errors: list[str] = []
    warnings: list[str] = []
    compiled = None
    try:
        compiled = compiler.compile_string(body.yaml, source=body.source_path)
    except AppCompilationError as exc:
        # AppCompilationError stringifies as
        # "App compilation failed (N error(s)): err1; err2; ..."
        msg = str(exc)
        if ":" in msg:
            tail = msg.split(":", 1)[1]
            errors = [e.strip() for e in tail.split(";") if e.strip()]
        else:
            errors = [msg]
    except Exception as exc:
        errors = [f"{type(exc).__name__}: {exc}"]

    valid = compiled is not None and not errors
    summary: dict[str, Any] | None = None
    graph: dict[str, Any] | None = None
    if valid and compiled is not None:
        try:
            execution = compiled.execution
            payload_schema = getattr(execution, "payload_schema", None)
            summary = {
                "app_id": compiled.meta.app_id,
                "name": compiled.meta.name,
                "mode": getattr(execution, "mode", ""),
                "agents": len(getattr(compiled, "agents", []) or []),
                "modules": list(getattr(compiled, "modules", {}).keys()),
                "trigger_types": [
                    getattr(t, "type", "") for t in getattr(execution, "triggers", []) or []
                ],
                "session_mode": getattr(execution, "session_mode", ""),
                "has_payload_schema": payload_schema is not None,
                "payload_schema_required": (
                    payload_schema.get("required") if isinstance(payload_schema, dict) else None
                ),
            }
        except Exception as exc:
            logger.debug("discovery.compile: summary build failed: %s", exc)
            summary = None

        # Extract the canvas graph in a separate try block so a graph
        # failure never masks the (already valid) compilation.
        try:
            graph = _extract_graph(compiled)
        except Exception as exc:
            logger.debug("discovery.compile: graph extraction failed: %s", exc)
            graph = None

    return AppResponse(
        success=True,  # the HTTP request itself succeeded
        data={
            "valid": valid,
            "errors": errors,
            "warnings": warnings,
            "summary": summary,
            "graph": graph,
        },
    )


# /generate-package-manifest - auto-generate package.toml from a YAML


class PromptPreviewRequest(BaseModel):
    """POST /api/discovery/prompt-preview body."""

    bundle_dir: str | None = Field(
        default=None,
        description="Absolute path to an app bundle directory.",
    )
    prompt_name: str | None = Field(
        default=None,
        description="Name of the prompt file without extension.",
    )
    content: str | None = Field(
        default=None,
        description="Inline prompt content to preview.",
    )
    variables: dict[str, Any] = Field(
        default_factory=dict,
        description="Variables to substitute during resolution.",
    )
    app_id: str = Field(
        default="_preview",
        description="Fake app_id used to build asset URLs.",
    )
    locale: str = Field(
        default="en",
        description="Locale suffix to prefer (en, fr, ...).",
    )


@router.post("/prompt-preview", response_model=AppResponse)
async def prompt_preview(
    request: Request, body: PromptPreviewRequest,
) -> AppResponse:
    """Compile a single prompt without deploying an app."""
    from pathlib import Path
    from digitorn.core.app.variables import (
        bundle_context,
        collected_prompt_metadata,
        resolve_variables,
    )

    if not body.content and not (body.bundle_dir and body.prompt_name):
        raise HTTPException(
            status_code=400,
            detail=(
                "Either 'content' or ('bundle_dir' + 'prompt_name') "
                "must be provided"
            ),
        )

    bundle_dir = None
    if body.bundle_dir:
        bundle_path = Path(body.bundle_dir).resolve()
        if not bundle_path.is_dir():
            raise HTTPException(
                status_code=404,
                detail=f"bundle_dir not found: {body.bundle_dir}",
            )
        bundle_dir = bundle_path

    frontmatter: dict[str, Any] = {}
    try:
        with bundle_context(
            bundle_dir=bundle_dir,
            app_id=body.app_id,
            locale=body.locale,
        ):
            # Determine the template to resolve
            if body.content is not None:
                template = body.content
            else:
                template = "{{prompt." + (body.prompt_name or "") + "}}"

            compiled_text = resolve_variables(
                template,
                variables=body.variables,
            )
            frontmatter = collected_prompt_metadata()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    _model_hint = body.model if hasattr(body, "model") and body.model else None
    token_estimate = max(1, len(compiled_text) // 4)
    if _model_hint:
        # Off-loop: tokenizer load can take seconds on first call.
        import asyncio as _asyncio
        def _count() -> int:
            try:
                from litellm import token_counter
                return int(token_counter(model=_model_hint, text=compiled_text))
            except Exception:
                return token_estimate
        token_estimate = await _asyncio.to_thread(_count)

    # Scan for outgoing references to assets/prompts/skills.
    import re as _re
    asset_urls = _re.findall(
        r"/api/apps/[^/]+/assets/[^\s\)\"']+",
        compiled_text,
    )

    return AppResponse(
        success=True,
        data={
            "compiled_text": compiled_text,
            "token_estimate": token_estimate,
            "referenced_assets": sorted(set(asset_urls)),
            "referenced_variables": sorted(body.variables.keys()),
            "frontmatter": frontmatter,
            "warnings": [],
        },
    )


@router.post("/generate-package-manifest", response_model=AppResponse)
async def generate_package_manifest_route(
    request: Request, body: GeneratePackageManifestRequest,
) -> AppResponse:
    """Compile a YAML and emit a best-effort `package.toml`."""
    registry = _get_registry(request)

    # Local import to avoid circular dep at module load time
    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.core.app.errors import AppCompilationError
    from digitorn.core.packages import generate_package_manifest

    compiler = AppYAMLCompiler(registry)

    errors: list[str] = []
    warnings: list[str] = []
    compiled = None
    try:
        compiled = compiler.compile_string(body.yaml, source="<package-init>")
    except AppCompilationError as exc:
        msg = str(exc)
        if ":" in msg:
            tail = msg.split(":", 1)[1]
            errors = [e.strip() for e in tail.split(";") if e.strip()]
        else:
            errors = [msg]
    except Exception as exc:
        errors = [f"{type(exc).__name__}: {exc}"]

    if compiled is None or errors:
        return AppResponse(
            success=False,
            data={"valid": False, "errors": errors, "warnings": warnings},
            error="YAML did not compile - fix it before generating a manifest.",
        )

    try:
        manifest = generate_package_manifest(
            compiled,
            source_type="local",
            publisher=body.publisher,
            license=body.license,
            homepage=body.homepage,
        )
    except Exception as exc:
        logger.exception("generate_package_manifest failed")
        return AppResponse(
            success=False,
            data=None,
            error=f"Manifest generation failed: {exc}",
        )

    # Surface friendly warnings for missing fields a publisher would want
    if not manifest.package.license:
        warnings.append(
            "no license declared - required when publishing to the hub"
        )
    if not manifest.package.author:
        warnings.append("no author declared")
    if not manifest.package.description:
        warnings.append("no description declared")
    if not manifest.permissions.filesystem_access and not manifest.permissions.network_access:
        warnings.append(
            "package declares no network or filesystem access - is the app fully self-contained?"
        )

    return AppResponse(
        success=True,
        data={
            "valid": True,
            "warnings": warnings,
            "manifest": manifest.to_dict(),
            "toml": manifest.to_toml(),
            "summary": {
                "package_id": manifest.id,
                "version": manifest.version,
                "risk_level": manifest.permissions.risk_level,
                "modules": manifest.requirements.modules,
                "required_credentials": manifest.credentials.required,
            },
        },
    )

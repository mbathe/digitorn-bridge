"""Dev Tools Module - 3 tools for testing & building Digitorn apps."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import BaseModel, Field

from digitorn.modules.base import ActionResult, BaseModule
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ModuleManifest

logger = logging.getLogger(__name__)

_HIDDEN = {"hidden": True}

class AppParams(BaseModel):
    """App lifecycle + discovery + packages + MCP + drafts + security."""

    yaml_path: str = Field("", description="Path to app YAML (deploy/validate).")
    app_id: str = Field("", description="App ID (status/undeploy/secrets/tools).")

    yaml_content: str = Field("", json_schema_extra=_HIDDEN, description="Inline YAML content (alternative to yaml_path).")
    validate_only: bool = Field(False, json_schema_extra=_HIDDEN, description="Validate YAML without deploying.")
    compile_yaml: bool = Field(False, json_schema_extra=_HIDDEN, description="Compile YAML and return resolved config.")
    prompt_preview: bool = Field(False, json_schema_extra=_HIDDEN, description="Preview the resolved system prompt for an agent.")
    generate_manifest: bool = Field(False, json_schema_extra=_HIDDEN, description="Generate a package manifest from YAML.")
    agent_id: str = Field("", json_schema_extra=_HIDDEN, description="Agent ID for prompt_preview.")

    undeploy: bool = Field(False, json_schema_extra=_HIDDEN, description="Undeploy the app.")
    list_apps: bool = Field(False, json_schema_extra=_HIDDEN, description="List all deployed apps.")
    list_modules: bool = Field(False, json_schema_extra=_HIDDEN, description="List all available modules (discovery).")
    list_templates: bool = Field(False, json_schema_extra=_HIDDEN, description="List all app templates.")
    list_triggers: bool = Field(False, json_schema_extra=_HIDDEN, description="List available trigger types (discovery).")

    secret_key: str = Field("", json_schema_extra=_HIDDEN, description="Set a secret: key name.")
    secret_value: str = Field("", json_schema_extra=_HIDDEN, description="Set a secret: value.")

    credential_provider: str = Field("", json_schema_extra=_HIDDEN, description="User-level credential provider (e.g. deepseek).")
    credential_fields: dict[str, Any] = Field(default_factory=dict, json_schema_extra=_HIDDEN, description="Credential fields (e.g. {api_key: sk-...}).")
    list_credentials: bool = Field(False, json_schema_extra=_HIDDEN, description="List user credentials.")
    delete_credential_id: str = Field("", json_schema_extra=_HIDDEN, description="Delete a user credential by id.")

    search_tools: str = Field("", json_schema_extra=_HIDDEN, description="Search tools in the app. Empty = list categories.")
    get_tool: str = Field("", json_schema_extra=_HIDDEN, description="Get full schema of a tool by name.")

    package_source: str = Field("", json_schema_extra=_HIDDEN, description="Install package from source (git url / path / registry id).")
    list_packages: bool = Field(False, json_schema_extra=_HIDDEN, description="List installed packages.")
    uninstall_package: str = Field("", json_schema_extra=_HIDDEN, description="Uninstall a package by id.")
    upgrade_package: str = Field("", json_schema_extra=_HIDDEN, description="Upgrade a package by id.")

    mcp_catalog: bool = Field(False, json_schema_extra=_HIDDEN, description="List MCP server catalog.")
    mcp_install: dict[str, Any] = Field(default_factory=dict, json_schema_extra=_HIDDEN, description="Install an MCP server (body).")
    mcp_list: bool = Field(False, json_schema_extra=_HIDDEN, description="List installed MCP servers.")
    mcp_delete_id: str = Field("", json_schema_extra=_HIDDEN, description="Delete an MCP server by id.")
    mcp_test_id: str = Field("", json_schema_extra=_HIDDEN, description="Test an MCP server connection by id.")

    list_drafts: bool = Field(False, json_schema_extra=_HIDDEN, description="List builder drafts.")
    create_draft_yaml: str = Field("", json_schema_extra=_HIDDEN, description="Create a draft with this YAML.")
    draft_name: str = Field("", json_schema_extra=_HIDDEN, description="Draft name.")
    update_draft_id: str = Field("", json_schema_extra=_HIDDEN, description="Update draft by id (with yaml_content).")
    deploy_draft_id: str = Field("", json_schema_extra=_HIDDEN, description="Deploy a draft by id.")
    delete_draft_id: str = Field("", json_schema_extra=_HIDDEN, description="Delete a draft by id.")

    security_profile: bool = Field(False, json_schema_extra=_HIDDEN, description="Get security profile for app_id.")

    health: bool = Field(False, json_schema_extra=_HIDDEN, description="Daemon health.")
    diagnostics: bool = Field(False, json_schema_extra=_HIDDEN, description="App diagnostics for app_id.")

class ChatParams(BaseModel):
    """Session-based conversation + inspection + queue + approvals + live events."""

    app_id: str = Field("", description="App ID (required for first message).")
    message: str = Field("", description="Message to send.")
    workspace: str = Field("", description="Workspace directory path.")

    session_id: str = Field("", json_schema_extra=_HIDDEN, description="Session ID (follow-ups, inspect).")
    client_message_id: str = Field("", json_schema_extra=_HIDDEN, description="Optional idempotency key for this send.")
    queue_mode: str = Field("", json_schema_extra=_HIDDEN, description="'async' | 'wait' | 'replace_last'.")
    image_paths: list[str] = Field(default_factory=list, json_schema_extra=_HIDDEN, description="Paths to images to attach.")

    inspect: bool = Field(False, json_schema_extra=_HIDDEN, description="Inspect session - turns, tools, violations.")
    memory: bool = Field(False, json_schema_extra=_HIDDEN, description="Get session memory (goal, facts, entities).")
    tasks: bool = Field(False, json_schema_extra=_HIDDEN, description="Get session task list.")
    get_workspace: bool = Field(False, json_schema_extra=_HIDDEN, description="Get workspace snapshot (files + state).")
    preview_snapshot: bool = Field(False, json_schema_extra=_HIDDEN, description="Get preview snapshot (UI state).")
    code_snapshot: bool = Field(False, json_schema_extra=_HIDDEN, description="Get code snapshot (file tree without content).")
    file_path: str = Field("", json_schema_extra=_HIDDEN, description="Read a specific workspace file.")
    approve_file: str = Field("", json_schema_extra=_HIDDEN, description="Approve a workspace file by path.")
    reject_file: str = Field("", json_schema_extra=_HIDDEN, description="Reject a workspace file by path.")

    history: bool = Field(False, json_schema_extra=_HIDDEN, description="Get session message history.")
    persistent_events: bool = Field(False, json_schema_extra=_HIDDEN, description="Get persistent event log (DB).")
    since_seq: int = Field(0, json_schema_extra=_HIDDEN, description="Replay events since this seq.")
    context_breakdown: bool = Field(False, json_schema_extra=_HIDDEN, description="Get per-source token breakdown.")

    queue: bool = Field(False, json_schema_extra=_HIDDEN, description="Get queue entries for the session.")
    clear_queue: bool = Field(False, json_schema_extra=_HIDDEN, description="Clear all queued messages.")
    cancel_entry_id: str = Field("", json_schema_extra=_HIDDEN, description="Cancel a queue entry by id.")

    abort: bool = Field(False, json_schema_extra=_HIDDEN, description="Abort the current turn.")
    purge_queue_on_abort: bool = Field(False, json_schema_extra=_HIDDEN, description="Purge queue on abort.")
    resume: bool = Field(False, json_schema_extra=_HIDDEN, description="Resume an interrupted session.")
    fork: bool = Field(False, json_schema_extra=_HIDDEN, description="Fork the session.")
    compact: bool = Field(False, json_schema_extra=_HIDDEN, description="Compact context.")
    export_session: bool = Field(False, json_schema_extra=_HIDDEN, description="Export session as JSON.")
    delete_session: bool = Field(False, json_schema_extra=_HIDDEN, description="Delete the session permanently.")

    respond: str = Field("", json_schema_extra=_HIDDEN, description="Respond to an ask_user question.")
    approve_id: str = Field("", json_schema_extra=_HIDDEN, description="Approve a pending tool call by request_id.")
    deny_id: str = Field("", json_schema_extra=_HIDDEN, description="Deny a pending request.")
    pending: bool = Field(False, json_schema_extra=_HIDDEN, description="List pending approvals/questions.")

    search: str = Field("", json_schema_extra=_HIDDEN, description="Search sessions of app_id by query.")
    list_sessions: bool = Field(False, json_schema_extra=_HIDDEN, description="List all sessions of app_id.")

    watch: bool = Field(False, json_schema_extra=_HIDDEN, description="Live-stream the turn: receive events in real time, return early on approval/ask_user/error.")
    watch_include_tokens: bool = Field(False, json_schema_extra=_HIDDEN, description="Include per-token events in the timeline (verbose).")
    watch_max_events: int = Field(200, json_schema_extra=_HIDDEN, description="Max events returned in the timeline.")

    timeout: float = Field(3600.0, json_schema_extra=_HIDDEN, description="Max wait time.")

class RunParams(BaseModel):
    """Non-conversational: one-shot, pipeline, triggers, background sessions + tasks."""

    app_id: str = Field(..., description="App ID.")
    input_text: str = Field("", description="Input for one-shot apps.")

    pipeline: bool = Field(False, json_schema_extra=_HIDDEN, description="Run as pipeline (structured input).")
    pipeline_input: Any = Field(None, json_schema_extra=_HIDDEN, description="Pipeline structured input.")

    trigger_id: str = Field("", json_schema_extra=_HIDDEN, description="Fire a trigger by ID.")
    test_trigger: bool = Field(False, json_schema_extra=_HIDDEN, description="Test-fire (dry run) instead of fire.")
    trigger_payload: dict[str, Any] = Field(default_factory=dict, json_schema_extra=_HIDDEN, description="Payload for fire_trigger.")

    background_message: str = Field("", json_schema_extra=_HIDDEN, description="Create a background session with this message.")
    background_payload: dict[str, Any] = Field(default_factory=dict, json_schema_extra=_HIDDEN, description="Background session payload.")
    list_bg_sessions: bool = Field(False, json_schema_extra=_HIDDEN, description="List background sessions.")
    bg_session_id: str = Field("", json_schema_extra=_HIDDEN, description="Inspect a specific background session.")
    bg_pause_id: str = Field("", json_schema_extra=_HIDDEN, description="Pause a bg session.")
    bg_resume_id: str = Field("", json_schema_extra=_HIDDEN, description="Resume a bg session.")

    create_bg_task: dict[str, Any] = Field(default_factory=dict, json_schema_extra=_HIDDEN, description="Create a background task (body).")
    list_bg_tasks: bool = Field(False, json_schema_extra=_HIDDEN, description="List background tasks.")
    bg_task_id: str = Field("", json_schema_extra=_HIDDEN, description="Inspect / wait on a bg task.")
    wait_bg_task: bool = Field(False, json_schema_extra=_HIDDEN, description="Wait for bg task completion.")
    cancel_bg_task_id: str = Field("", json_schema_extra=_HIDDEN, description="Cancel a bg task by id.")

    list_triggers: bool = Field(False, json_schema_extra=_HIDDEN, description="List app triggers.")
    list_sessions: bool = Field(False, json_schema_extra=_HIDDEN, description="List app sessions.")
    list_watchers: bool = Field(False, json_schema_extra=_HIDDEN, description="List active watchers.")
    create_watcher: dict[str, Any] = Field(default_factory=dict, json_schema_extra=_HIDDEN, description="Create a watcher (body).")

    activations: bool = Field(False, json_schema_extra=_HIDDEN, description="List activation history.")
    errors: bool = Field(False, json_schema_extra=_HIDDEN, description="List app errors.")

    timeout: float = Field(3600.0, json_schema_extra=_HIDDEN)

class DevToolsModule(BaseModule):
    """Dev tools for testing + building Digitorn apps - 3 tools."""

    MODULE_ID = "dev_tools"
    VERSION = "3.0.0"

    def __init__(self) -> None:
        super().__init__()
        self._client = None
        self._sessions: dict[str, Any] = {}

    def get_prompt_sections(self) -> list[dict[str, Any]]:
        return [{
            "title": "App Testing & Building - Why and How",
            "content": (
                "You have 3 tools (App, Chat, Run) backed by a full live client. "
                "They let you do literally everything a chat user can do, plus "
                "everything the Builder backend needs to craft and validate apps.\n"
                "\n"
                "## Rule of thumb\n"
                "- App   - lifecycle + discovery + packages + MCP + drafts + security\n"
                "- Chat  - sessions + queue + approvals + memory + workspace + live events\n"
                "- Run   - one-shot, pipeline, triggers, background sessions/tasks\n"
                "\n"
                "## Testing workflow (always)\n"
                "1. App(yaml_path=..., validate_only=true)  - catch syntax errors\n"
                "2. App(yaml_path=...)                       - deploy, read required_secrets\n"
                "3. App(app_id=..., secret_key=..., secret_value=...)  - configure missing secrets\n"
                "4. Chat(app_id=..., message=realistic task) - smoke test with LLM\n"
                "5. Chat(session_id=..., message=...)        - multi-turn memory check\n"
                "6. Chat(session_id=..., inspect=true)       - tools used, violations\n"
                "7. If any step fails → read error, fix YAML, redeploy, retest\n"
                "\n"
                "## Builder workflow (crafting apps)\n"
                "- App(yaml_content=..., compile_yaml=true)         - resolve & inspect\n"
                "- App(yaml_content=..., prompt_preview=true, agent_id=...) - see final system prompt\n"
                "- App(create_draft_yaml=..., draft_name=...)       - save as draft\n"
                "- App(deploy_draft_id=...)                         - deploy a draft\n"
                "- App(list_modules=true)                           - what modules exist\n"
                "- App(list_templates=true)                         - starter templates\n"
                "\n"
                "## Live testing (never mock)\n"
                "Tests must hit the real daemon with a real LLM. If you find yourself wanting to\n"
                "fake anything, stop - live tests prove real behavior.\n"
                "\n"
                "## Common failures\n"
                "- 'credential missing' → App with secret_key/secret_value\n"
                "- Agent blocks → Chat(session_id, pending=true) to see ask_user / approvals\n"
                "- Wrong tools used → tune tool_prompts in YAML\n"
                "- 'old_string not found' → the agent didn't Read before Edit - stricter prompt\n"
                "- Timeout → LLM too slow or prompt too long, simplify\n"
            ),
            "priority": 35,
        }]

    def _get_client(self):
        if self._client is None:
            from digitorn.testing.client import DevClient
            self._client = DevClient()
        return self._client

    def _get_session(self, session_id: str):
        s = self._sessions.get(session_id)
        if s is None:
            from digitorn.testing.models import SessionHandle
            s = SessionHandle(
                session_id=session_id,
                app_id="",
                daemon_url=self._get_client().daemon_url,
                workspace="",
            )
            self._sessions[session_id] = s
        return s

    def _bind_session(self, session) -> None:
        self._sessions[session.session_id] = session

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": "3 tools for testing & building Digitorn apps.",
            "author": "Digitorn Team",
        })

    async def on_start(self) -> None:
        pass

    async def on_stop(self) -> None:
        self._sessions.clear()
        self._client = None

    @action(
        description="App lifecycle + discovery + packages + MCP + drafts + security.",
        tool_prompt=(
            "Manage apps on the live daemon: validate, deploy, undeploy, configure, "
            "and the full builder surface (compile, prompt_preview, drafts, MCP, packages).\n"
            "\n"
            "## Lifecycle\n"
            "  App(yaml_path='app.yaml', validate_only=true)\n"
            "  App(yaml_path='app.yaml')                    - deploy (from file)\n"
            "  App(yaml_content='<yaml string>')            - deploy (inline, builder-friendly)\n"
            "  App(app_id='my-app')                         - status + required secrets\n"
            "  App(app_id='my-app', undeploy=true)\n"
            "  App(list_apps=true)\n"
            "\n"
            "## Secrets & credentials\n"
            "  App(app_id='my-app', secret_key='X', secret_value='Y')\n"
            "  App(credential_provider='deepseek', credential_fields={'api_key': 'sk-...'})\n"
            "  App(list_credentials=true)\n"
            "  App(delete_credential_id='<uuid>')\n"
            "\n"
            "## Discovery & builder\n"
            "  App(yaml_content=..., compile_yaml=true)\n"
            "  App(yaml_content=..., prompt_preview=true, agent_id='main')\n"
            "  App(yaml_content=..., generate_manifest=true)\n"
            "  App(list_modules=true) / list_templates=true / list_triggers=true\n"
            "\n"
            "## Drafts (builder iteration loop)\n"
            "  App(create_draft_yaml=..., draft_name=...)\n"
            "  App(list_drafts=true)\n"
            "  App(update_draft_id=..., yaml_content=...)\n"
            "  App(deploy_draft_id=...)\n"
            "  App(delete_draft_id=...)\n"
            "\n"
            "## Packages & MCP\n"
            "  App(list_packages=true) / package_source='<git url>' / uninstall_package=...\n"
            "  App(mcp_catalog=true) / mcp_list=true / mcp_install={...} / mcp_test_id=...\n"
            "\n"
            "## Tool discovery (what the agent can call inside an app)\n"
            "  App(app_id='my-app', search_tools='read')    - filter by keyword\n"
            "  App(app_id='my-app', get_tool='Write')       - full schema\n"
            "\n"
            "## Observability\n"
            "  App(health=true)\n"
            "  App(app_id='my-app', diagnostics=true)\n"
            "  App(app_id='my-app', security_profile=true)\n"
            "\n"
            "## Rules\n"
            "- ALWAYS validate before deploying\n"
            "- ALWAYS check required_secrets after deploy - the app won't work without them\n"
            "- Prefer yaml_content for ephemeral tests; yaml_path for real artifacts"
        ),
        params_model=AppParams,
        risk_level="medium",
        tags=["dev"],
        cli_label="App",
        cli_param="yaml_path",
    )
    async def app(self, params: AppParams) -> ActionResult:
        # Off-load: the DaemonClient is sync httpx and each hop would
        # stall the loop for seconds.
        import asyncio as _asyncio
        return await _asyncio.to_thread(self._app_sync, params)

    def _app_sync(self, params: AppParams) -> ActionResult:
        try:
            client = self._get_client()

            if params.health:
                return ActionResult(success=True, data=client.get_health())

            if params.list_apps:
                apps = client.list_apps()
                return ActionResult(success=True, data={"apps": apps, "count": len(apps)})

            if params.list_modules:
                mods = client.discovery_modules()
                return ActionResult(success=True, data={"modules": mods, "count": len(mods)})

            if params.list_templates:
                tpl = client.discovery_templates()
                return ActionResult(success=True, data={"templates": tpl, "count": len(tpl)})

            if params.list_triggers:
                trs = client.discovery_triggers()
                return ActionResult(success=True, data={"triggers": trs, "count": len(trs)})

            # User credentials
            if params.list_credentials:
                creds = client.list_user_credentials()
                return ActionResult(success=True, data={"credentials": creds, "count": len(creds)})
            if params.delete_credential_id:
                ok = client.delete_user_credential(params.delete_credential_id)
                return ActionResult(success=ok, data={"deleted": params.delete_credential_id})
            if params.credential_provider and params.credential_fields:
                r = client.create_user_credential(params.credential_provider, params.credential_fields)
                return ActionResult(success=bool(r.get("id")), data=r)

            # Packages
            if params.list_packages:
                pkgs = client.list_packages()
                return ActionResult(success=True, data={"packages": pkgs, "count": len(pkgs)})
            if params.package_source:
                r = client.install_package(params.package_source, force=False)
                return ActionResult(success=bool(r), data=r)
            if params.uninstall_package:
                ok = client.uninstall_package(params.uninstall_package)
                return ActionResult(success=ok, data={"uninstalled": params.uninstall_package})
            if params.upgrade_package:
                r = client.upgrade_package(params.upgrade_package)
                return ActionResult(success=bool(r), data=r)

            # MCP
            if params.mcp_catalog:
                cat = client.mcp_catalog()
                return ActionResult(success=True, data={"servers": cat, "count": len(cat)})
            if params.mcp_list:
                srvs = client.mcp_list_servers()
                return ActionResult(success=True, data={"servers": srvs, "count": len(srvs)})
            if params.mcp_install:
                r = client.mcp_install_server(params.mcp_install)
                return ActionResult(success=bool(r), data=r)
            if params.mcp_delete_id:
                ok = client.mcp_delete_server(params.mcp_delete_id)
                return ActionResult(success=ok, data={"deleted": params.mcp_delete_id})
            if params.mcp_test_id:
                r = client.mcp_test_server(params.mcp_test_id)
                return ActionResult(success=True, data=r)

            # Drafts
            if params.list_drafts:
                ds = client.list_drafts()
                return ActionResult(success=True, data={"drafts": ds, "count": len(ds)})
            if params.create_draft_yaml:
                r = client.create_draft(params.create_draft_yaml, name=params.draft_name)
                return ActionResult(success=bool(r.get("id") or r.get("draft_id")), data=r)
            if params.update_draft_id and params.yaml_content:
                r = client.update_draft(params.update_draft_id, params.yaml_content, name=params.draft_name or None)
                return ActionResult(success=True, data=r)
            if params.deploy_draft_id:
                r = client.deploy_draft(params.deploy_draft_id, force=True)
                return ActionResult(success=bool(r), data=r)
            if params.delete_draft_id:
                ok = client.delete_draft(params.delete_draft_id)
                return ActionResult(success=ok, data={"deleted": params.delete_draft_id})

            # Builder: compile / prompt_preview / manifest
            if params.yaml_content and params.compile_yaml:
                r = client.compile_yaml(params.yaml_content)
                return ActionResult(success=not bool(r.get("error")), data=r)
            if params.yaml_content and params.prompt_preview:
                r = client.prompt_preview(params.yaml_content, agent_id=params.agent_id)
                return ActionResult(success=True, data=r)
            if params.yaml_content and params.generate_manifest:
                r = client.generate_package_manifest(params.yaml_content)
                return ActionResult(success=True, data=r)

            if params.yaml_path:
                import os as _os
                from pathlib import Path as _Path
                _p = _Path(params.yaml_path)
                if not _p.is_absolute():
                    ctx = self._context_var.get()
                    ws = getattr(ctx, "workspace", "") if ctx else ""
                    if ws:
                        candidate = _Path(ws) / params.yaml_path
                        if candidate.is_file():
                            params.yaml_path = str(candidate)

            # Validate
            if params.yaml_path and params.validate_only:
                r = client.validate_yaml(params.yaml_path)
                return ActionResult(success=r.get("valid", False), data=r)

            # Deploy (file path or inline content). Also auto-persist a builder
            # draft so `GET /api/builder/drafts` reflects coordinator writes.
            def _persist_draft(yaml_content: str, app_id: str, deployed: bool) -> None:
                try:
                    import asyncio as _ai
                    from digitorn.core.app.build_draft_store import BuildDraftStore
                    from digitorn.core.database import get_session_factory
                    ctx = self._context_var.get()
                    uid = getattr(ctx, "user_id", "") if ctx else ""
                    if not uid:
                        return
                    store = BuildDraftStore(get_session_factory())
                    async def _run():
                        await store.create(
                            uid,
                            name=app_id or "Untitled",
                            initial_yaml=yaml_content,
                            builder_state={
                                "app_id": app_id,
                                "status": "deployed" if deployed else "compiled",
                                "auto_persisted": True,
                            },
                        )
                    _ai.run(_run())
                except Exception as exc:
                    logger.debug("draft_auto_persist_failed: %s", exc)

            if params.yaml_path and not params.validate_only:
                app = client.deploy(params.yaml_path, force=True)
                secrets = client.get_required_secrets(app.app_id)
                try:
                    yaml_content = Path(params.yaml_path).read_text(encoding="utf-8")
                except Exception:
                    yaml_content = ""
                _persist_draft(yaml_content, app.app_id, deployed=True)
                return ActionResult(success=True, data={
                    "app_id": app.app_id, "mode": app.mode, "agents": app.agents,
                    "total_tools": app.total_tools,
                    "required_secrets": [{"key": s["key"], "is_set": s.get("is_set")} for s in secrets],
                })
            if params.yaml_content and not params.validate_only:
                app = client.deploy_yaml_content(params.yaml_content, force=True)
                secrets = client.get_required_secrets(app.app_id)
                _persist_draft(params.yaml_content, app.app_id, deployed=True)
                return ActionResult(success=True, data={
                    "app_id": app.app_id, "mode": app.mode, "agents": app.agents,
                    "total_tools": app.total_tools,
                    "required_secrets": [{"key": s["key"], "is_set": s.get("is_set")} for s in secrets],
                })

            if not params.app_id:
                return ActionResult(success=False, error="Provide yaml_path, yaml_content, or app_id.")

            # app_id-scoped
            if params.undeploy:
                ok = client.undeploy(params.app_id)
                return ActionResult(success=ok, data={"app_id": params.app_id, "undeployed": ok})

            if params.secret_key and params.secret_value:
                ok = client.set_secret(params.app_id, params.secret_key, params.secret_value)
                return ActionResult(success=ok, data={"key": params.secret_key, "set": ok})

            if params.get_tool:
                return ActionResult(success=True, data=client.get_tool(params.app_id, params.get_tool))
            if params.search_tools:
                results = client.search_tools(params.app_id, params.search_tools)
                return ActionResult(success=True, data={"results": results, "count": len(results)})
            if params.diagnostics:
                return ActionResult(success=True, data=client.get_app_diagnostics(params.app_id))
            if params.security_profile:
                return ActionResult(success=True, data=client.get_security_profile(params.app_id))

            # Default: status + required secrets + tool categories
            app = client.get_app(params.app_id)
            secrets = client.get_required_secrets(params.app_id)
            cats = client.get_tool_categories(params.app_id)
            return ActionResult(success=True, data={
                "app_id": app.app_id, "mode": app.mode, "agents": app.agents,
                "total_tools": app.total_tools,
                "required_secrets": [{"key": s["key"], "is_set": s.get("is_set")} for s in secrets],
                "categories": cats,
            })

        except Exception as e:
            return ActionResult(success=False, error=str(e))

    @action(
        description="Chat with a deployed app - sessions, queue, approvals, workspace, live events.",
        tool_prompt=(
            "Exercise conversational apps like a human user would, plus everything the "
            "The client shows: live events, queue state, preview snapshot, code snapshot, "
            "workspace files, memory, tasks, history, approvals, ask_user, abort/resume/fork.\n"
            "\n"
            "## Send messages\n"
            "  Chat(app_id='my-app', message='...', workspace='/path')  - new session, return session_id\n"
            "  Chat(session_id='s', message='...')                     - follow-up\n"
            "  Chat(session_id='s', message='...', queue_mode='async') - send while turn running (queue)\n"
            "  Chat(session_id='s', image_paths=['a.png','b.png'], message='describe')  - multimodal\n"
            "\n"
            "## Watch mode (PREFERRED for testing - avoid timeouts)\n"
            "  Chat(app_id='x', message='...', watch=true)\n"
            "  Returns a compact seq-ordered timeline (tool_calls, text chunks, thinking,\n"
            "  approvals, errors) and an explicit status: 'completed' | 'pending_approval' |\n"
            "  'pending_ask_user' | 'error' | 'timeout'. Returns EARLY on blockers - no waste.\n"
            "  If pending_ask_user: follow up with respond='<answer>'.\n"
            "  If pending_approval: follow up with approve_id=<rid>.\n"
            "\n"
            "## Inspect\n"
            "  Chat(session_id='s', inspect=true)          - turns + tools + violations\n"
            "  Chat(session_id='s', memory=true)           - goal, todos, facts\n"
            "  Chat(session_id='s', tasks=true)            - task list\n"
            "  Chat(session_id='s', history=true)          - full message history\n"
            "  Chat(session_id='s', persistent_events=true, since_seq=N)  - durable event log\n"
            "  Chat(session_id='s', context_breakdown=true)  - token breakdown\n"
            "\n"
            "## Workspace / preview\n"
            "  Chat(session_id='s', get_workspace=true)    - workspace metadata\n"
            "  Chat(session_id='s', preview_snapshot=true) - UI state\n"
            "  Chat(session_id='s', code_snapshot=true)    - file tree (no content)\n"
            "  Chat(session_id='s', file_path='src/x.py')  - specific file content\n"
            "  Chat(session_id='s', approve_file='src/x.py') / reject_file=...\n"
            "\n"
            "## Queue / control\n"
            "  Chat(session_id='s', queue=true)            - list queue\n"
            "  Chat(session_id='s', clear_queue=true) / cancel_entry_id=...\n"
            "  Chat(session_id='s', abort=true, purge_queue_on_abort=true)\n"
            "  Chat(session_id='s', resume=true)           - after crash/interrupt\n"
            "  Chat(session_id='s', fork=true) / compact=true / export_session=true / delete_session=true\n"
            "\n"
            "## Approvals / ask_user\n"
            "  Chat(session_id='s', pending=true)          - what's blocking\n"
            "  Chat(session_id='s', respond='my answer')   - answer ask_user\n"
            "  Chat(session_id='s', approve_id='<rid>') / deny_id='<rid>'\n"
            "\n"
            "## Find sessions\n"
            "  Chat(app_id='my-app', list_sessions=true)\n"
            "  Chat(app_id='my-app', search='<query>')\n"
            "\n"
            "## Rules\n"
            "- Use realistic messages (not 'test')\n"
            "- At least 2-3 turns to validate multi-turn memory\n"
            "- If the agent blocks: pending=true first, then respond= or approve_id=\n"
            "- Always inspect after a test - tools_used, used_bash_for_files, violations"
        ),
        params_model=ChatParams,
        risk_level="low",
        tags=["dev"],
        cli_label="Chat",
        cli_param="message",
    )
    async def chat(self, params: ChatParams) -> ActionResult:
        # `watch=true` is natively async; the sync branch off-loads to
        # avoid deadlocking the daemon's own turns through sync httpx.
        if params.watch and params.message:
            try:
                client = self._get_client()
                images = [client.encode_image(p) for p in (params.image_paths or [])] or None
                return await self._watched_send(client, params, images)
            except Exception as e:
                return ActionResult(success=False, error=str(e))
        import asyncio as _asyncio
        return await _asyncio.to_thread(self._chat_sync, params)

    def _chat_sync(self, params: ChatParams) -> ActionResult:
        try:
            client = self._get_client()

            # Find sessions
            if params.app_id and params.list_sessions:
                sessions = client.list_sessions(params.app_id)
                return ActionResult(success=True, data={"sessions": sessions, "count": len(sessions)})
            if params.app_id and params.search:
                results = client.search_sessions(params.app_id, params.search)
                return ActionResult(success=True, data={"results": results, "count": len(results)})

            # session_id-scoped read-only ops
            sess = None
            if params.session_id:
                sess = self._get_session(params.session_id)

            if sess is not None:
                if params.pending:
                    app_id = sess.app_id or params.app_id
                    return ActionResult(success=True, data={"pending": client.get_pending(app_id)})
                if params.approve_id:
                    app_id = sess.app_id or params.app_id
                    return ActionResult(success=client.approve(app_id, params.approve_id),
                                        data={"approved": params.approve_id})
                if params.deny_id:
                    app_id = sess.app_id or params.app_id
                    return ActionResult(success=client.deny(app_id, params.deny_id),
                                        data={"denied": params.deny_id})
                if params.respond:
                    app_id = sess.app_id or params.app_id
                    pending = client.get_pending(app_id)
                    for req in pending:
                        rid = req.get("request_id", "")
                        if rid and ("ask" in req.get("type", "") or "ask" in req.get("tool_name", "")):
                            ok = client.respond_to_ask(app_id, rid, params.respond)
                            return ActionResult(success=ok, data={"request_id": rid, "resolved": ok})
                    if pending:
                        rid = pending[0].get("request_id", "")
                        ok = client.approve(app_id, rid, response=params.respond)
                        return ActionResult(success=ok, data={"request_id": rid, "resolved": ok})
                    return ActionResult(success=False, error="No pending question found.")

                if params.abort:
                    data = client.abort_session(sess, purge_queue=params.purge_queue_on_abort)
                    return ActionResult(success=True, data=data)
                if params.resume:
                    r = client.resume(sess)
                    return self._format_turn_result(sess.session_id, r)
                if params.fork:
                    return ActionResult(success=True, data=client.fork_session(sess))
                if params.compact:
                    return ActionResult(success=True, data=client.compact_session(sess))
                if params.export_session:
                    return ActionResult(success=True, data=client.export_session(sess))
                if params.delete_session:
                    return ActionResult(success=client.delete_session(sess), data={"deleted": sess.session_id})

                if params.inspect:
                    turns = [{
                        "turn": t.turn_number, "success": t.success,
                        "text_preview": t.text[:200], "tools": t.tools_used,
                        "files_read": t.files_read, "files_edited": t.files_edited,
                        "duration": t.duration_seconds, "violations": t.behavior_violations,
                        "live_events": [repr(e) for e in t.live_events[:10]],
                    } for t in sess.turns]
                    return ActionResult(success=True, data={
                        "session_id": sess.session_id, "total_turns": sess.turn_count,
                        "all_tools": sess.all_tools_used, "turns": turns,
                    })
                if params.memory:
                    return ActionResult(success=True, data=client.get_memory(sess))
                if params.tasks:
                    return ActionResult(success=True, data={"tasks": client.get_tasks(sess)})
                if params.history:
                    return ActionResult(success=True, data={"messages": client.get_history(sess)})
                if params.persistent_events:
                    return ActionResult(success=True, data={
                        "events": client.get_persistent_events(sess, since_seq=params.since_seq),
                    })
                if params.context_breakdown:
                    return ActionResult(success=True, data=client.get_context_breakdown(sess))

                if params.get_workspace:
                    return ActionResult(success=True, data=client.get_workspace(sess))
                if params.preview_snapshot:
                    return ActionResult(success=True, data=client.get_preview_snapshot(sess))
                if params.code_snapshot:
                    return ActionResult(success=True, data=client.get_code_snapshot(sess))
                if params.file_path:
                    return ActionResult(success=True, data=client.get_workspace_file_content(sess, params.file_path))
                if params.approve_file:
                    return ActionResult(success=client.approve_workspace_file(sess, params.approve_file),
                                        data={"approved": params.approve_file})
                if params.reject_file:
                    return ActionResult(success=client.reject_workspace_file(sess, params.reject_file),
                                        data={"rejected": params.reject_file})

                if params.queue:
                    return ActionResult(success=True, data={"entries": client.get_queue(sess)})
                if params.clear_queue:
                    return ActionResult(success=True, data={"cancelled": client.clear_queue(sess)})
                if params.cancel_entry_id:
                    ok = client.cancel_queue_entry(sess, params.cancel_entry_id)
                    return ActionResult(success=ok, data={"cancelled": params.cancel_entry_id})

            # Send message (needs either app_id new OR session_id follow-up).
            # The watch=true path is handled in the async wrapper above -
            # this sync path never sees watch requests.
            images = [client.encode_image(p) for p in (params.image_paths or [])] or None

            if params.session_id and params.message:
                result = client.send(
                    self._get_session(params.session_id), params.message,
                    timeout=params.timeout,
                )
                return self._format_turn_result(params.session_id, result)

            if params.app_id and params.message:
                if params.queue_mode or images:
                    sess = client.create_session(params.app_id, params.workspace)
                    self._bind_session(sess)
                    r = client.post_message_raw(
                        sess, params.message,
                        queue_mode=params.queue_mode or None,
                        client_message_id=params.client_message_id or None,
                        images=images,
                    )
                    return ActionResult(success=r.get("status_code") in (200, 202), data={
                        "session_id": sess.session_id,
                        "status_code": r.get("status_code"),
                        **((r.get("body") or {}).get("data") or {}),
                    })
                sess = client.chat(
                    app_id=params.app_id, message=params.message,
                    workspace=params.workspace, timeout=params.timeout,
                )
                self._bind_session(sess)
                return self._format_turn_result(sess.session_id, sess.last)

            return ActionResult(success=False, error=(
                "Provide (app_id+message), (session_id+message), or (session_id+flag)."
            ))

        except Exception as e:
            return ActionResult(success=False, error=str(e))

    async def _watched_send(self, client, params, images):
        from digitorn.testing.assertions import sort_by_seq

        if params.session_id:
            sess = self._get_session(params.session_id)
            if not sess.app_id and params.app_id:
                sess.app_id = params.app_id
        elif params.app_id:
            sess = await asyncio.to_thread(client.create_session, params.app_id, params.workspace)
            self._bind_session(sess)
        else:
            return ActionResult(success=False, error="watch requires app_id or session_id")

        post = await asyncio.to_thread(
            client.post_message_raw,
            sess, params.message,
            queue_mode=params.queue_mode or None,
            client_message_id=params.client_message_id or None,
            images=images,
        )
        if post.get("status_code") not in (200, 202):
            return ActionResult(success=False, error=f"POST failed: {post}")
        correlation_id = (post.get("body") or {}).get("data", {}).get("correlation_id") or ""

        stream = await asyncio.to_thread(client.open_event_stream, sess)
        try:
            done = await asyncio.to_thread(
                stream.wait_for,
                "message_done",
                params.timeout,
                lambda e: (e.get("payload") or {}).get("correlation_id") == correlation_id,
            )

            pending_approvals = (
                await asyncio.to_thread(client.get_pending, sess.app_id) if sess.app_id else []
            )
            blocker = None
            if done is None:
                for ev in stream.events():
                    t = ev.get("type")
                    if t == "approval_request":
                        blocker = "pending_approval"; break
                    if t == "error":
                        blocker = "error"; break
                if blocker is None and pending_approvals:
                    blocker = "pending_ask_user" if any(
                        "ask" in (r.get("type") or r.get("tool_name") or "")
                        for r in pending_approvals
                    ) else "pending_approval"
                if blocker is None:
                    blocker = "timeout"

            events = sort_by_seq(stream.events())
            timeline = self._compact_timeline(
                events, include_tokens=params.watch_include_tokens,
                max_events=params.watch_max_events,
            )
            assistant_text = "".join(
                (e.get("payload") or {}).get("content") or
                (e.get("payload") or {}).get("text") or ""
                for e in events if e.get("type") == "token"
            )

            tool_calls = [
                {
                    "name": (e.get("payload") or {}).get("name") or (e.get("payload") or {}).get("label"),
                    "params": (e.get("payload") or {}).get("params") or (e.get("payload") or {}).get("arguments"),
                    "result_preview": str((e.get("payload") or {}).get("result") or "")[:200],
                }
                for e in events if e.get("type") == "tool_call"
            ]
            status = "completed" if done else blocker

            return ActionResult(success=(status == "completed"), data={
                "session_id": sess.session_id,
                "correlation_id": correlation_id,
                "status": status,
                "text": assistant_text[:4000],
                "tool_calls": tool_calls,
                "pending_approvals": pending_approvals,
                "timeline": timeline,
                "event_count": len(events),
                "last_seq": stream.last_seq(),
            })
        finally:
            try:
                await asyncio.to_thread(stream.stop, 2.0)
            except Exception as exc:
                logger.debug("module best-effort block failed: %s", exc)

    def _compact_timeline(self, events, *, include_tokens: bool, max_events: int):
        out = []
        token_buf: list[str] = []

        def flush_tokens():
            if token_buf:
                out.append({"type": "text", "content": "".join(token_buf)[:500]})
                token_buf.clear()

        for e in events:
            t = e.get("type", "")
            pl = e.get("payload") or {}
            if t == "token":
                if include_tokens:
                    out.append({"type": "token", "seq": e.get("seq"), "content": pl.get("content") or pl.get("text") or ""})
                else:
                    token_buf.append(pl.get("content") or pl.get("text") or "")
                continue
            flush_tokens()
            entry = {"type": t, "seq": e.get("seq")}
            if t in ("user_message",):
                entry["content"] = (pl.get("content") or "")[:200]
                entry["correlation_id"] = pl.get("correlation_id")
            elif t in ("message_started", "message_done"):
                entry["correlation_id"] = pl.get("correlation_id")
                entry["fast_path"] = pl.get("fast_path")
            elif t in ("tool_call", "tool_start", "tool_end"):
                entry["name"] = pl.get("name") or pl.get("label")
                if pl.get("result") is not None:
                    entry["result_preview"] = str(pl.get("result"))[:200]
            elif t == "approval_request":
                entry["tool"] = pl.get("tool_name") or pl.get("name")
                entry["request_id"] = pl.get("request_id")
            elif t == "thinking":
                entry["preview"] = (pl.get("content") or pl.get("text") or "")[:200]
            elif t == "error":
                entry["error"] = pl.get("error") or pl
                entry["code"] = pl.get("code")
            elif t == "stream_done":
                entry["finish_reason"] = pl.get("finish_reason")
            elif t in ("preview:resource_set", "preview:resource_patched", "preview:resource_deleted"):
                entry["channel"] = pl.get("channel")
                entry["id"] = pl.get("id")
            out.append(entry)
            if len(out) >= max_events:
                break
        flush_tokens()
        return out

    def _format_turn_result(self, session_id: str, result) -> ActionResult:
        if result is None:
            return ActionResult(success=False, error="No result")
        return ActionResult(success=True, data={
            "session_id": session_id,
            "success": result.success,
            "text": result.text[:2000],
            "tools_used": result.tools_used,
            "files_read": result.files_read,
            "files_edited": result.files_edited,
            "files_written": result.files_written,
            "bash_commands": result.bash_commands,
            "duration_seconds": result.duration_seconds,
            "used_glob": result.used_glob,
            "used_grep": result.used_grep,
            "used_bash_for_files": result.used_bash_for_files,
            "has_behavior_violations": result.has_behavior_violations,
            "behavior_violations": result.behavior_violations,
            "live_events": [repr(e) for e in result.live_events[:15]],
            "error": result.error,
        })

    @action(
        description="Run non-conversational apps - one-shot, pipeline, triggers, background, watchers.",
        tool_prompt=(
            "Non-conversational execution: one-shot, pipeline, triggers, background sessions, "
            "background tasks, watchers, activation history.\n"
            "\n"
            "## One-shot (mode: one_shot)\n"
            "  Run(app_id='research', input_text='Compare React vs Vue')\n"
            "\n"
            "## Pipeline (mode: pipeline)\n"
            "  Run(app_id='pipe', pipeline=true, pipeline_input={'urls': [...]})\n"
            "\n"
            "## Triggers (mode: background)\n"
            "  Run(app_id='bg', list_triggers=true)\n"
            "  Run(app_id='bg', trigger_id='webhook', trigger_payload={...})\n"
            "  Run(app_id='bg', test_trigger=true, trigger_id='cron')\n"
            "\n"
            "## Background sessions (mode: background)\n"
            "  Run(app_id='bg', background_message='hello', background_payload={...})\n"
            "  Run(app_id='bg', list_bg_sessions=true) / bg_session_id=... / bg_pause_id=... / bg_resume_id=...\n"
            "\n"
            "## Background tasks (long-running jobs)\n"
            "  Run(app_id='x', create_bg_task={...}) / list_bg_tasks=true\n"
            "  Run(app_id='x', bg_task_id='tid') / wait_bg_task=true\n"
            "  Run(app_id='x', cancel_bg_task_id='tid')\n"
            "\n"
            "## Watchers\n"
            "  Run(app_id='x', list_watchers=true) / create_watcher={...}\n"
            "\n"
            "## Activations / errors\n"
            "  Run(app_id='x', activations=true) / errors=true\n"
            "\n"
            "## When to use which\n"
            "- Chat: mode: conversation apps (multi-turn, interactive)\n"
            "- Run:  all other modes (one_shot, pipeline, background)"
        ),
        params_model=RunParams,
        risk_level="low",
        tags=["dev"],
        cli_label="Run",
        cli_param="input_text",
    )
    async def run(self, params: RunParams) -> ActionResult:
        # All branches hit the daemon via sync `httpx`. Offload to
        # worker to avoid freezing the event loop during a chain of
        # trigger/bg/watcher calls.
        import asyncio as _asyncio
        return await _asyncio.to_thread(self._run_sync, params)

    def _run_sync(self, params: RunParams) -> ActionResult:
        try:
            client = self._get_client()

            if params.list_triggers:
                trs = client.get_triggers(params.app_id)
                return ActionResult(success=True, data={"triggers": trs, "count": len(trs)})
            if params.list_sessions:
                ss = client.list_sessions(params.app_id)
                return ActionResult(success=True, data={"sessions": ss, "count": len(ss)})
            if params.list_watchers:
                ws = client.list_watchers(params.app_id)
                return ActionResult(success=True, data={"watchers": ws, "count": len(ws)})
            if params.create_watcher:
                r = client.create_watcher(params.app_id, params.create_watcher)
                return ActionResult(success=bool(r), data=r)

            if params.activations:
                return ActionResult(success=True, data={"activations": client.get_activations(params.app_id)})
            if params.errors:
                return ActionResult(success=True, data={"errors": client.get_app_errors(params.app_id)})

            if params.trigger_id:
                if params.test_trigger:
                    r = client.test_trigger(params.app_id, params.trigger_id)
                else:
                    r = client.fire_trigger(params.app_id, params.trigger_id, payload=params.trigger_payload or None)
                return ActionResult(success=True, data=r)

            if params.list_bg_sessions:
                return ActionResult(success=True, data={"sessions": client.list_background_sessions(params.app_id)})
            if params.bg_session_id:
                if params.bg_pause_id:
                    return ActionResult(success=client.bg_session_pause(params.app_id, params.bg_pause_id), data={})
                if params.bg_resume_id:
                    return ActionResult(success=client.bg_session_resume(params.app_id, params.bg_resume_id), data={})
                return ActionResult(success=True, data=client.get_background_session(params.app_id, params.bg_session_id))
            if params.background_message:
                r = client.create_background_session(params.app_id, params.background_message, payload=params.background_payload or None)
                return ActionResult(success=True, data=r)

            if params.list_bg_tasks:
                return ActionResult(success=True, data={"tasks": client.list_background_tasks(params.app_id)})
            if params.create_bg_task:
                r = client.create_background_task(params.app_id, params.create_bg_task)
                return ActionResult(success=bool(r), data=r)
            if params.bg_task_id and params.wait_bg_task:
                return ActionResult(success=True, data=client.wait_background_task(params.app_id, params.bg_task_id, timeout=params.timeout))
            if params.bg_task_id:
                return ActionResult(success=True, data=client.get_background_task(params.app_id, params.bg_task_id))
            if params.cancel_bg_task_id:
                ok = client.cancel_background_task(params.app_id, params.cancel_bg_task_id)
                return ActionResult(success=ok, data={"cancelled": params.cancel_bg_task_id})

            if params.pipeline:
                r = client.run_pipeline(params.app_id, params.pipeline_input, timeout=params.timeout)
                return ActionResult(success=not bool(r.get("error")), data=r)

            if params.input_text:
                r = client.run_oneshot(params.app_id, params.input_text, timeout=params.timeout)
                return ActionResult(success=True, data=r)

            return ActionResult(success=False, error=(
                "Provide input_text (one-shot), pipeline_input + pipeline=true, trigger_id, "
                "background_message, or a bg_task/watcher/activations flag."
            ))

        except Exception as e:
            return ActionResult(success=False, error=str(e))

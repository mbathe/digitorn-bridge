"""Bootstrap - CompiledApp → RuntimeApp."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from digitorn.core.runtime.types import AgentContext, ContextWindowConfig

# warm-import litellm on the main thread so its pydantic-schema generation runs once and parallel deploys don't deadlock on the import lock.
try:
    import litellm  # noqa: F401
except Exception as _litellm_warmup_exc:  # pragma: no cover
    logging.getLogger(__name__).warning(
        "bootstrap: litellm warm-import failed (%s); token estimates "
        "fall back to char/4. Multi-thread import lock collisions in "
        "_estimate_tools_tokens are now possible.",
        _litellm_warmup_exc,
    )

if TYPE_CHECKING:
    from digitorn.core.app.compiler import CompiledApp
    from digitorn.modules.base import BaseModule
    from digitorn.modules.context_builder.module import ContextBuilderModule
    from digitorn.modules.registry import ModuleRegistry


def _resolve_workspace(compiled: "CompiledApp", *, standalone: bool = False) -> str:
    """Resolve the workspace directory for an app."""
    mode = getattr(compiled.execution, "workspace_mode", "auto")
    workspace = getattr(compiled.execution, "workspace", "")

    if mode == "none":
        return ""

    if mode == "fixed" and workspace:
        return workspace

    if workspace:
        return workspace

    if standalone:
        return os.getcwd()

    if compiled.source_path:
        return str(compiled.source_path.parent.resolve())

    from platformdirs import user_data_dir
    managed = Path(user_data_dir("digitorn")) / "workspaces" / compiled.app_id
    managed.mkdir(parents=True, exist_ok=True)
    return str(managed)

logger = logging.getLogger(__name__)


async def bootstrap(
    compiled: CompiledApp,
    registry: ModuleRegistry,
    skip_embeddings: bool = False,
) -> dict[str, Any]:
    """Bootstrap a compiled app into live runtime objects."""
    modules, service_bus = await _init_modules(compiled, registry)
    setup_summary = await _run_setup_phase(compiled, modules)
    context_builder, index = await _build_context_layer(
        compiled, modules, service_bus, skip_embeddings=skip_embeddings,
    )
    approval_queue = _build_approval_queue(compiled, modules)
    contexts = await _build_agent_contexts(
        compiled, modules, context_builder, index,
        setup_summary, approval_queue,
    )
    _wire_agent_spawn(compiled, modules, contexts, context_builder, skip_embeddings=skip_embeddings)
    hook_runner = await _build_hooks(
        compiled, contexts, modules, context_builder,
    )

    if hook_runner is not None:
        setattr(context_builder, "hook_runner", hook_runner)

    if hook_runner is not None and approval_queue is not None:
        async def _approval_hook_callback(request: Any) -> None:
            try:
                from digitorn.core.runtime.hooks import TurnState
                from types import SimpleNamespace
                tool_ctx = SimpleNamespace(
                    tool_name=getattr(request, "tool_name", "") or "",
                    tool_params=dict(getattr(request, "tool_params", {}) or {}),
                    tool_result=None,
                    tool_ok=False,
                    tool_elapsed=0.0,
                )
                state = TurnState(
                    messages=[],
                    turn=0,
                    max_turns=0,
                    tool_calls_count=0,
                    agent_id=getattr(request, "agent_id", "") or "",
                )
                state.tool_context = tool_ctx  # type: ignore[attr-defined]
                state._approval_request = request  # type: ignore[attr-defined]
                await hook_runner.run("approval_request", state)
            except Exception as exc:
                logger.debug("approval_request hook failed: %s", exc)
        approval_queue.add_on_request(_approval_hook_callback)

    return {
        "modules": modules,
        "contexts": contexts,
        "context_builder": context_builder,
        "hook_runner": hook_runner,
        "approval_queue": approval_queue,
    }


async def _init_modules(
    compiled: "CompiledApp",
    registry: "ModuleRegistry",
) -> tuple[dict[str, "BaseModule"], Any]:
    modules: dict[str, BaseModule] = {}
    errors: list[str] = []

    from digitorn.workers.registry import (
        ensure_default_registry_from_settings,
    )
    _workers_registry = ensure_default_registry_from_settings()

    for module_id in compiled.module_ids:
        try:
            cls = registry._classes.get(module_id)
            if cls is not None and getattr(cls, "MODULE_SINGLETON", False):
                modules[module_id] = registry.get(module_id)
            else:
                modules[module_id] = registry.create(module_id)
        except Exception as exc:
            errors.append(f"Failed to create module '{module_id}': {exc}")

    # llm_provider is system-required; auto-inject for legacy bundles that pre-date the compiler change.
    if "llm_provider" not in modules:
        try:
            cls = registry._classes.get("llm_provider")
            if cls is not None:
                if getattr(cls, "MODULE_SINGLETON", False):
                    modules["llm_provider"] = registry.get("llm_provider")
                else:
                    modules["llm_provider"] = registry.create("llm_provider")
        except Exception as exc:
            logger.warning(
                "bootstrap: failed to auto-inject llm_provider: %s", exc,
            )

    if errors:
        raise RuntimeError(f"Bootstrap failed: {'; '.join(errors)}")

    # skip llm_provider in module wrap - provider-level proxy handles chat_stream off-loop; module wrap would break on_config_update.
    _WORKER_WRAP_SKIP = {"llm_provider"}
    if not _workers_registry.is_empty():
        from digitorn.workers.action_wrapper import (
            wrap_module_for_worker,
        )
        for module_id, module in list(modules.items()):
            if module_id in _WORKER_WRAP_SKIP:
                continue
            endpoint = _workers_registry.route(module_id)
            if endpoint is None:
                continue
            try:
                wrap_module_for_worker(module, endpoint)
            except Exception as exc:
                logger.warning(
                    "worker_wrap_failed module=%s endpoint=%s err=%s "
                    "-- falling back to in-process execution",
                    module_id, endpoint.worker_id, exc,
                )

    from digitorn.modules.service_bus import ServiceBus
    service_bus = ServiceBus()

    from digitorn.core.sidecar_pool import DaemonSidecarPool
    sidecar_pool = DaemonSidecarPool()
    await sidecar_pool.start()

    for module_id, module in modules.items():
        service_bus.register_service(module_id, module)
        module._service_bus = service_bus
        module._sidecar_pool = sidecar_pool

    failed_modules: list[str] = []
    for module_id, module in modules.items():
        mod_constraints = compiled.modules[module_id].constraints
        if mod_constraints:
            module._constraints = mod_constraints

        # workered modules skip on_start (worker owns lifecycle) but still need their per-app `module.config` pushed.
        if getattr(module, "_skip_on_start", False):
            module._started_ok = True  # type: ignore[attr-defined]
            cfg = dict(compiled.modules[module_id].config or {})
            workspace = _resolve_module_workspace(compiled, module_id)
            # WORKSPACE_PLACEHOLDER is substituted per session; shipping the literal string to a worker would break chdir.
            from digitorn.core.runtime.types import WORKSPACE_PLACEHOLDER
            if (
                workspace
                and workspace != WORKSPACE_PLACEHOLDER
                and "workspace" not in cfg
            ):
                cfg["workspace"] = workspace
            if cfg:
                try:
                    from digitorn.workers.action_wrapper import (
                        push_module_config,
                    )
                    await push_module_config(
                        module_id, cfg,
                        registry=_workers_registry,
                        app_id=getattr(compiled, "app_id", None),
                    )
                except Exception as exc:
                    logger.warning(
                        "workered_config_push_failed module=%s err=%s",
                        module_id, exc,
                    )
            logger.debug(
                "module_on_start_skipped module=%s reason=workered "
                "config_keys=%s",
                module_id, sorted(cfg.keys()),
            )
            continue

        try:
            await module.on_start()
            module._started_ok = True  # type: ignore[attr-defined]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Module '%s' on_start failed: %s", module_id, exc, exc_info=True)
            module._started_ok = False  # type: ignore[attr-defined]
            module._start_error = str(exc)  # type: ignore[attr-defined]
            failed_modules.append(module_id)
            continue

        config = dict(compiled.modules[module_id].config or {})
        workspace = _resolve_module_workspace(compiled, module_id)
        if workspace and "workspace" not in config:
            config["workspace"] = workspace
        if config:
            try:
                await module.on_config_update(config)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Module '%s' config update failed: %s", module_id, exc, exc_info=True)

    if failed_modules:
        logger.warning(
            "bootstrap_modules_failed count=%d modules=%s",
            len(failed_modules), failed_modules,
        )

        mw_config = compiled.modules[module_id].middleware
        if mw_config:
            from digitorn.core.middleware import build_module_pipeline
            pipeline = build_module_pipeline(mw_config)
            if pipeline:
                module._middleware_pipeline = pipeline
                logger.info(
                    "module_middleware module=%s middlewares=%d",
                    module_id, len(pipeline.middlewares),
                )

    return modules, service_bus


def _resolve_module_workspace(compiled: "CompiledApp", module_id: str) -> str:
    """Resolve workspace path for a specific module; returns WORKSPACE_PLACEHOLDER in auto mode so per-session substitution can run."""
    from digitorn.core.runtime.types import WORKSPACE_PLACEHOLDER

    mode = getattr(compiled.execution, "workspace_mode", "auto")
    if mode in ("required", "none"):
        return ""
    ws = getattr(compiled.execution, "workspace", "")
    if not ws:
        mod_constraints = getattr(compiled.modules[module_id], "constraints", None) or {}
        mod_paths = mod_constraints.get("paths")
        if mod_paths and len(mod_paths) >= 1:
            ws = str(Path(mod_paths[0]).resolve())
    if not ws:
        return WORKSPACE_PLACEHOLDER
    return ws


async def _run_setup_phase(
    compiled: "CompiledApp",
    modules: dict[str, Any],
) -> list[str]:
    setup_summary: list[str] = []

    for module_id in compiled.module_ids:
        module = modules[module_id]
        for step in compiled.modules[module_id].setup_steps:
            try:
                await module.execute(step.action, step.resolved_params)
                setup_summary.append(
                    _summarize_setup_step(module_id, step.action, step.resolved_params)
                )
            except Exception as exc:
                logger.warning(
                    "Setup step %s.%s failed: %s", module_id, step.action, exc, exc_info=True,
                )

    schema_context = await _auto_describe_databases(modules)
    if schema_context:
        setup_summary.append(schema_context)

    for module_id in compiled.module_ids:
        snippet = modules[module_id].get_context_snippet()
        if snippet:
            setup_summary.append(snippet)

    await _auto_index_workspace(compiled, modules)
    return setup_summary


async def _build_context_layer(
    compiled: "CompiledApp",
    modules: dict[str, Any],
    service_bus: Any,
    skip_embeddings: bool = False,
) -> tuple["ContextBuilderModule", Any]:
    from digitorn.modules.context_builder.module import ContextBuilderModule

    context_builder = ContextBuilderModule()
    await context_builder.on_start()
    context_builder._service_bus = service_bus
    service_bus.register_service("context_builder", context_builder)

    # include context_builder so its granted actions (e.g. ask_user) get indexed.
    build_modules = {**modules, "context_builder": context_builder}
    # build_and_set_index synchronously loads ~250 MB sentence-transformers; offload to keep the loop responsive on cold start.
    import asyncio as _aio
    index = await _aio.to_thread(
        context_builder.build_and_set_index,
        build_modules, compiled.security_profile,
        skip_embeddings=skip_embeddings,
    )
    await _probe_mcp_schemas(modules, index)

    for mod in modules.values():
        mod._bg_notify = context_builder.push_module_notification
        if compiled.security_profile is not None and hasattr(mod, "_has_security_profile"):
            mod._has_security_profile = True

    for module_id, mod in modules.items():
        if hasattr(mod, "_register_renderer") and callable(mod._register_renderer):
            try:
                mod._register_renderer()
            except Exception as exc:
                logger.debug("Module '%s' renderer registration failed: %s", module_id, exc, exc_info=True)

    _inject_app_id_overrides(compiled, modules, context_builder)

    return context_builder, index


def _inject_app_id_overrides(
    compiled: "CompiledApp",
    modules: dict[str, Any],
    context_builder: "ContextBuilderModule",
) -> None:
    for mod_id in ("cron_native", "cache", "vector", "workspace", "channels", "web_preview"):
        mod = modules.get(mod_id)
        if mod is not None:
            mod._app_id_override = compiled.app_id


def _build_approval_queue(
    compiled: "CompiledApp",
    modules: dict[str, Any],
) -> Any:
    from digitorn.core.runtime.approval import ApprovalQueue

    needs_queue = compiled.security_profile is not None or _any_require_approval(modules)
    if not needs_queue:
        return None

    try:
        from digitorn.core.config import get_settings
        timeout = float(get_settings().session.approval_timeout_s)
    except Exception:
        timeout = 300.0
    if compiled.security_profile is not None:
        timeout = compiled.security_profile.approval_timeout
    return ApprovalQueue(default_timeout=timeout)


async def _build_agent_contexts(
    compiled: "CompiledApp",
    modules: dict[str, Any],
    context_builder: "ContextBuilderModule",
    index: Any,
    setup_summary: list[str],
    approval_queue: Any,
) -> dict[str, AgentContext]:
    from digitorn.modules.context_builder.builder import build_direct_tools
    from digitorn.modules.context_builder.prompt import build_system_prompt

    meta_tools = _build_meta_tools_schema(context_builder)
    _tc_block = getattr(compiled, "chat_tool_calls", None)
    _inject_intent = bool(getattr(_tc_block, "inject_intent", False)) if _tc_block else False
    direct_tools = build_direct_tools(index, inject_intent=_inject_intent)

    ctx_cfg = compiled.execution.context
    default_context_config = _resolve_context_config(ctx_cfg, modules)

    watchers_enabled = compiled.execution.watchers
    scheduler_enabled = compiled.execution.scheduler
    context_builder._watchers_enabled = watchers_enabled
    context_builder._scheduler_enabled = scheduler_enabled
    channels_info = _build_channels_info(compiled)
    default_channel = compiled.execution.default_channel
    channels_enabled = bool(compiled.channels)

    contexts: dict[str, AgentContext] = {}

    for agent in compiled.agents:
        ctx = await _build_single_agent_context(
            agent=agent,
            compiled=compiled,
            modules=modules,
            context_builder=context_builder,
            index=index,
            meta_tools=meta_tools,
            direct_tools=direct_tools,
            default_context_config=default_context_config,
            ctx_cfg=ctx_cfg,
            setup_summary=setup_summary,
            channels_info=channels_info,
            default_channel=default_channel,
            watchers_enabled=watchers_enabled,
            scheduler_enabled=scheduler_enabled,
            channels_enabled=channels_enabled,
            approval_queue=approval_queue,
            build_system_prompt=build_system_prompt,
        )
        contexts[agent.agent_id] = ctx

    if not contexts:
        raise RuntimeError(
            f"No agent contexts could be built for app "
            f"'{compiled.app_id}'. Declared agents: "
            f"{[a.agent_id for a in compiled.agents]}. This usually "
            f"means every agent's brain config failed to load."
        )

    return contexts


async def _build_single_agent_context(
    *,
    agent: Any,
    compiled: "CompiledApp",
    modules: dict[str, Any],
    context_builder: "ContextBuilderModule",
    index: Any,
    meta_tools: list[dict[str, Any]],
    direct_tools: list[dict[str, Any]],
    default_context_config: ContextWindowConfig,
    ctx_cfg: Any,
    setup_summary: list[str],
    channels_info: list[dict[str, Any]],
    default_channel: str | None,
    watchers_enabled: bool,
    scheduler_enabled: bool,
    channels_enabled: bool,
    approval_queue: Any,
    build_system_prompt: Any,
) -> AgentContext:
    provider = _resolve_provider(agent, modules)

    try:
        from digitorn.workers.llm_wrap import maybe_wrap_provider
        provider = maybe_wrap_provider(provider, agent.brain)
    except Exception as exc:
        logger.warning(
            "llm_provider_wrap_skipped agent=%s err=%s -- "
            "running with in-process provider", agent.agent_id, exc,
        )

    if agent.brain.context is not None:
        base_context_config = _resolve_context_config(agent.brain.context, modules)
        yaml_max_tokens = agent.brain.context.max_tokens
    else:
        base_context_config = default_context_config
        yaml_max_tokens = ctx_cfg.max_tokens

    agent_context_config = _refine_context_config_for_provider(
        base_context_config, provider, yaml_max_tokens=yaml_max_tokens,
    )

    native_tool_use = _has_native_tool_use(provider)
    if agent.brain.native_tool_use is not None:
        native_tool_use = agent.brain.native_tool_use
        logger.info("native_tool_use override: agent=%s value=%s (YAML)", agent.agent_id, native_tool_use)

    forced = getattr(compiled.execution, "tool_injection", None)
    if forced in ("direct", "compact_direct", "discovery"):
        tool_injection = forced
        logger.info("tool_injection_forced mode=%s agent=%s", tool_injection, agent.agent_id)
    else:
        tool_injection = await _achoose_tool_injection(
            total_tools=index.total_tools,
            context_window=agent_context_config.max_tokens,
            direct_tools=direct_tools,
            model=getattr(provider, "model", None) if provider is not None else None,
        )

    agent_tools = _assemble_agent_tools(
        agent=agent,
        compiled=compiled,
        modules=modules,
        context_builder=context_builder,
        meta_tools=meta_tools,
        direct_tools=direct_tools,
        tool_injection=tool_injection,
        watchers_enabled=watchers_enabled,
        scheduler_enabled=scheduler_enabled,
        channels_enabled=channels_enabled,
    )

    system_prompt = _build_agent_prompt(
        agent=agent,
        compiled=compiled,
        modules=modules,
        context_builder=context_builder,
        index=index,
        agent_tools=agent_tools,
        native_tool_use=native_tool_use,
        tool_injection=tool_injection,
        setup_summary=setup_summary,
        channels_info=channels_info,
        default_channel=default_channel,
        build_system_prompt=build_system_prompt,
    )

    _log_agent_tools(agent.agent_id, tool_injection, agent_tools, native_tool_use)

    gen_params: dict[str, Any] = {}
    if agent.brain.temperature is not None:
        gen_params["temperature"] = agent.brain.temperature
    if agent.brain.max_tokens is not None:
        gen_params["max_tokens"] = agent.brain.max_tokens
    if agent.brain.top_p is not None:
        gen_params["top_p"] = agent.brain.top_p

    prompt_cache = None
    provider_name = getattr(provider, "provider_name", "") or ""
    if "anthropic" in provider_name.lower():
        prompt_cache = {"type": "ephemeral"}

    ctx = AgentContext(
        agent_id=agent.agent_id,
        role=agent.role,
        provider=provider,
        system_prompt=system_prompt,
        tools=agent_tools,
        native_tool_use=native_tool_use,
        tool_injection=tool_injection,
        plan_first=agent.plan_first,
        watchers_enabled=watchers_enabled,
        generation_params=gen_params,
        context_config=agent_context_config,
        approval_queue=approval_queue,
        security_profile=compiled.security_profile,
        prompt_cache_control=prompt_cache,
        setup_summary=setup_summary,
        channels_info=channels_info,
        default_channel=default_channel,
        workspace=_resolve_workspace(compiled),
        app_id=compiled.app_id,
    )
    ctx.context_builder = context_builder
    ctx._chat_tool_calls = getattr(compiled, "chat_tool_calls", None)

    # resolve the fallback AgentBrain through the same builder as the primary so a 402 can swap providers.
    ctx._fallback_brain = None
    if agent.brain.fallback is not None:
        try:
            fb = agent.brain.fallback
            llm_mod = modules.get("llm_provider")
            if llm_mod is None:
                raise RuntimeError("llm_provider module required for fallback brain")
            deployed_fb = await llm_mod.create_provider_from_brain(fb)
            # derived brains always route through the gateway so quota + cost accounting cover them.
            from digitorn.core.credentials.gateway_resolver import (
                route_derived_brain_through_gateway,
            )
            from digitorn.core.config import get_settings as _get_settings
            ctx._fallback_brain = await route_derived_brain_through_gateway(
                brain=fb,
                deployed_provider=deployed_fb,
                settings=_get_settings(),
            )
            via_gw = ctx._fallback_brain is not deployed_fb
            logger.info(
                "fallback_brain_wired agent=%s primary=%s fallback=%s/%s via_gateway=%s",
                agent.agent_id, agent.brain.model,
                getattr(fb, "provider", "?"), getattr(fb, "model", "?"),
                via_gw,
            )
        except Exception as exc:
            logger.warning(
                "fallback_brain_init_failed agent=%s: %s", agent.agent_id, exc,
            )
            ctx._fallback_brain = None

    ctx.compiled_constraints = {
        mid: compiled.modules[mid].constraints
        for mid in compiled.module_ids
        if compiled.modules[mid].constraints
    }

    _wire_direct_modules_map(ctx, modules, agent)
    _wire_skills(compiled, context_builder)
    _wire_memory_module(ctx, compiled, modules, tool_injection)
    ctx.lsp_module = modules.get("lsp")
    ctx.preview_module = modules.get("preview")
    ctx.widget_module = modules.get("widget")
    ctx.cron_native_module = modules.get("cron_native")
    ctx.workspace_module = modules.get("workspace")

    # Wire workspace module → preview module (delegate)
    workspace_mod = modules.get("workspace")
    if workspace_mod is not None and ctx.preview_module is not None:
        workspace_mod._preview = ctx.preview_module

    # web_preview is a daemon singleton; register the per-app shell so cross-module lookups stay correct under concurrent deploys.
    web_preview_mod = modules.get("web_preview")
    shell_mod = modules.get("shell")
    if web_preview_mod is not None and shell_mod is not None:
        web_preview_mod._shell = shell_mod
        try:
            app_id = compiled.app_id or ""
            if app_id:
                shells = getattr(web_preview_mod, "_shells_by_app", None)
                if isinstance(shells, dict):
                    shells[app_id] = shell_mod
        except Exception as exc:
            logger.debug("bootstrap best-effort block failed: %s", exc)

    # when ctx.workspace is missing, shell asks workspace for the canonical session sync dir so `cd <subdir>` matches WsWrite output.
    if shell_mod is not None and workspace_mod is not None:
        shell_mod._workspace_module = workspace_mod

    # Wire workspace module → lsp module (diagnostics on write/edit)
    lsp_mod = modules.get("lsp")
    if workspace_mod is not None and lsp_mod is not None:
        workspace_mod._lsp = lsp_mod

    # wire filesystem to lsp + preview so real-disk writes push diagnostics to the same channel as workspace writes.
    fs_mod = modules.get("filesystem")
    if fs_mod is not None:
        if lsp_mod is not None:
            fs_mod._lsp = lsp_mod
        if ctx.preview_module is not None:
            fs_mod._preview = ctx.preview_module

    # top-level `workspace:` block is the source of truth for render_mode/entry_file/title; inject into module fields.
    ws_block = getattr(compiled, "workspace", None)
    if ws_block is not None and workspace_mod is not None:
        if not workspace_mod._render_mode or workspace_mod._render_mode == "auto":
            workspace_mod._render_mode = getattr(ws_block, "render_mode", "auto")
        if not workspace_mod._entry_file:
            workspace_mod._entry_file = getattr(ws_block, "entry_file", None)
        if not workspace_mod._title:
            workspace_mod._title = getattr(ws_block, "title", None)
    _wire_app_middleware(ctx, compiled, agent)

    # Wire behavior module - runtime enforcement engine
    logger.info("PRE_WIRE_BEHAVIOR ctx.provider=%s compiled.behavior=%s", getattr(ctx, "provider", "NONE"), compiled.behavior is not None)
    await _wire_behavior_module(ctx, compiled, modules)

    if not native_tool_use:
        tool_names = [
            t.get("function", {}).get("name", "")
            for t in agent_tools if t.get("function", {}).get("name")
        ]
        provider._known_tool_names = tool_names

    return ctx


def _assemble_agent_tools(
    *,
    agent: Any,
    compiled: "CompiledApp",
    modules: dict[str, Any],
    context_builder: "ContextBuilderModule",
    meta_tools: list[dict[str, Any]],
    direct_tools: list[dict[str, Any]],
    tool_injection: str,
    watchers_enabled: bool,
    scheduler_enabled: bool,
    channels_enabled: bool,
) -> list[dict[str, Any]]:
    skills_enabled = bool(compiled.skills)

    if tool_injection in ("direct", "compact_direct"):
        primitive_tools = _build_primitive_tools_schema(
            context_builder,
            watchers_enabled=watchers_enabled,
            channels_enabled=channels_enabled,
            skills_enabled=skills_enabled,
        )
        agent_tools = direct_tools + primitive_tools
    else:
        agent_tools = [
            t for t in meta_tools
            if t.get("function", {}).get("name") in _DISCOVERY_META_ACTIONS
        ]

    extras = _build_operational_extras(
        agent=agent,
        compiled=compiled,
        modules=modules,
        context_builder=context_builder,
        tool_injection=tool_injection,
        watchers_enabled=watchers_enabled,
        scheduler_enabled=scheduler_enabled,
        channels_enabled=channels_enabled,
        skills_enabled=skills_enabled,
    )

    if extras:
        agent_tools = agent_tools + extras
        logger.info(
            "Injected %d operational tools as direct for agent %s",
            len(extras), agent.agent_id,
        )

    # Safety net: deduplicate by tool name (LLM APIs reject duplicate names).
    seen_names: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for t in agent_tools:
        name = t.get("function", {}).get("name", "")
        if not name or name not in seen_names:
            seen_names.add(name)
            deduped.append(t)
        else:
            logger.warning("duplicate_tool_filtered name=%s agent=%s", name, agent.agent_id)
    return deduped


def _build_operational_extras(
    *,
    agent: Any,
    compiled: "CompiledApp",
    modules: dict[str, Any],
    context_builder: "ContextBuilderModule",
    tool_injection: str,
    watchers_enabled: bool,
    scheduler_enabled: bool,
    channels_enabled: bool,
    skills_enabled: bool,
) -> list[dict[str, Any]]:
    extras: list[dict[str, Any]] = []

    if tool_injection == "discovery":
        extras.extend(_build_primitive_tools_schema(
            context_builder,
            watchers_enabled=watchers_enabled,
            channels_enabled=channels_enabled,
            skills_enabled=skills_enabled,
        ))

    # in direct/compact_direct mode, memory + agent_spawn already live in direct_tools; only inject explicitly for discovery mode.
    if tool_injection == "discovery":
        memory_module = modules.get("memory")
        if memory_module is not None:
            extras.extend(_build_module_tools_schema(memory_module))

        spawn_module = modules.get("agent_spawn") if agent.role == "coordinator" else None
        if spawn_module is not None:
            extras.extend(_build_module_tools_schema(spawn_module))

    direct_module_ids = set(compiled.execution.direct_modules)
    # memory and agent_spawn are already injected above - skip to avoid duplicates
    _already_injected = {"memory", "agent_spawn"}
    if direct_module_ids and tool_injection == "discovery":
        for mod_id in direct_module_ids - _already_injected:
            mod = modules.get(mod_id)
            if mod is not None:
                dm_tools = _build_module_tools_schema(mod, prefix=mod_id, use_short_names=True)
                extras.extend(dm_tools)
                logger.info(
                    "direct_module_injected module=%s actions=%d agent=%s",
                    mod_id, len(dm_tools), agent.agent_id,
                )

    hidden = _collect_hidden_actions(compiled)
    if hidden:
        extras = [
            t for t in extras
            if t.get("function", {}).get("name", "") not in hidden
        ]

    return extras


def _collect_hidden_actions(compiled: "CompiledApp") -> set[str]:
    hidden: set[str] = set()
    for ha in compiled.hidden_actions:
        mod = ha.get("module", "")
        for a in ha.get("actions", []):
            hidden.add(f"{mod}__{a}")
            hidden.add(f"{mod}.{a}")
            hidden.add(a)
    return hidden


def _build_agent_prompt(
    *,
    agent: Any,
    compiled: "CompiledApp",
    modules: dict[str, Any],
    context_builder: "ContextBuilderModule",
    index: Any,
    agent_tools: list[dict[str, Any]],
    native_tool_use: bool,
    tool_injection: str,
    setup_summary: list[str],
    channels_info: list[dict[str, Any]],
    default_channel: str | None,
    build_system_prompt: Any,
) -> str:
    project_memory = _load_project_memory(compiled)
    user_prompt = agent.system_prompt
    if project_memory:
        user_prompt = f"# Project Memory\n\n{project_memory}\n\n{user_prompt}"

    memory_module = modules.get("memory")
    spawn_module = modules.get("agent_spawn") if agent.role == "coordinator" else None

    prompt_modules = {**modules, "context_builder": context_builder}
    if memory_module is not None:
        prompt_modules["memory"] = memory_module
    if spawn_module is not None:
        prompt_modules["agent_spawn"] = spawn_module

    return build_system_prompt(
        agent_id=agent.agent_id,
        role=agent.role,
        user_prompt=user_prompt,
        index=index,
        native_tool_use=native_tool_use,
        tool_injection=tool_injection,
        tools=agent_tools,
        plan_first=agent.plan_first,
        setup_summary=setup_summary,
        channels_info=channels_info,
        default_channel=default_channel,
        skills=compiled.skills if compiled.skills else None,
        modules=prompt_modules,
    )


def _log_agent_tools(
    agent_id: str,
    tool_injection: str,
    agent_tools: list[dict[str, Any]],
    native_tool_use: bool,
) -> None:
    tool_names = [t.get("function", {}).get("name", "?") for t in agent_tools]
    logger.info(
        "Agent '%s': %s tool injection (%d tools), %s tool use | tools: %s",
        agent_id, tool_injection, len(agent_tools),
        "native" if native_tool_use else "text-based",
        ", ".join(sorted(tool_names)),
    )


def _wire_direct_modules_map(
    ctx: AgentContext,
    modules: dict[str, Any],
    agent: Any,
) -> None:
    dm_map: dict[str, str] = {}
    memory = modules.get("memory")
    if memory is not None:
        for name in memory._action_registry:
            dm_map[name] = f"memory.{name}"
    spawn = modules.get("agent_spawn") if agent.role == "coordinator" else None
    if spawn is not None:
        for name in spawn._action_registry:
            dm_map[name] = f"agent_spawn.{name}"
    if dm_map:
        ctx.direct_modules_map = dm_map


def _wire_skills(compiled: "CompiledApp", context_builder: "ContextBuilderModule") -> None:
    all_skills = list(compiled.skills) if compiled.skills else []
    try:
        from digitorn.core.workspace import WorkspaceLayout
        ws = getattr(compiled.execution, "workspace", "") or str(Path.cwd())
        layout = WorkspaceLayout(ws, compiled.app_id)
        fs_skills = layout.load_skills()
        yaml_commands = {s["command"] for s in all_skills}
        for name, content in fs_skills.items():
            cmd = f"/{name}"
            if cmd not in yaml_commands:
                all_skills.append({"command": cmd, "description": f"Skill: {name}", "content": content})
    except (OSError, ValueError, ImportError) as exc:
        logger.warning("Failed to load filesystem skills: %s", exc, exc_info=True)
    if all_skills:
        context_builder._skills = all_skills


def _wire_memory_module(
    ctx: AgentContext,
    compiled: "CompiledApp",
    modules: dict[str, Any],
    tool_injection: str,
) -> None:
    memory = modules.get("memory")
    if memory is None:
        return
    ctx.memory_module = memory
    memory._tool_injection = tool_injection
    memory._app_id = compiled.app_id
    try:
        from digitorn.core.kv import create_backend
        memory._kv_backend = create_backend(None)
    except Exception as exc:
        logger.warning("Failed to create memory KV backend: %s", exc, exc_info=True)


async def _wire_behavior_module(
    ctx: AgentContext,
    compiled: "CompiledApp",
    modules: dict[str, Any],
) -> None:
    """Configure and attach the behavior enforcement module."""
    behavior_config = getattr(compiled, "behavior", None)
    if behavior_config is None:
        return
    try:
        await _wire_behavior_module_inner(ctx, compiled, modules, behavior_config)
    except Exception as exc:
        logger.warning("behavior_module_wire_failed: %s", exc, exc_info=True)


async def _wire_behavior_module_inner(
    ctx: AgentContext,
    compiled: "CompiledApp",
    modules: dict[str, Any],
    behavior_config: Any,
) -> None:

    behavior_mod = modules.get("behavior")
    if behavior_mod is None:
        # Auto-create the module if behavior: is defined but module not listed
        try:
            from digitorn.modules.behavior import BehaviorModule
            behavior_mod = BehaviorModule()
            modules["behavior"] = behavior_mod
        except ImportError:
            logger.warning("behavior module not available")
            return

    # Convert Pydantic model to dict for on_config_update (sync - just sets internal state)
    config = behavior_config.model_dump() if hasattr(behavior_config, "model_dump") else dict(behavior_config)
    from digitorn.modules.behavior.engine import BehaviorEngine
    behavior_mod._engine = BehaviorEngine(config)
    behavior_mod._classify_enabled = config.get("classify_turns", False)
    behavior_mod._profile_name = config.get("profile", "")

    # Store classifier config (Pydantic model → dict)
    raw_cls = config.get("classifier", {})
    if hasattr(raw_cls, "model_dump"):
        behavior_mod._classifier_config = raw_cls.model_dump()
    elif isinstance(raw_cls, dict):
        behavior_mod._classifier_config = raw_cls
    else:
        behavior_mod._classifier_config = {}

    ctx.behavior_module = behavior_mod

    # Wire the classifier LLM provider
    if behavior_config.classify_turns:
        classifier_provider = None

        # Option 1: dedicated brain in behavior config
        brain_config = getattr(behavior_config, "brain", None)
        if brain_config is not None and brain_config.model:
            try:
                classifier_provider = await _resolve_provider_from_brain(brain_config, modules)
                # route the deploy-time fallback through the gateway too, mirroring fallback_brain / summary_provider.
                if classifier_provider is not None:
                    from digitorn.core.credentials.gateway_resolver import (
                        route_derived_brain_through_gateway,
                    )
                    from digitorn.core.config import get_settings as _gs
                    classifier_provider = await route_derived_brain_through_gateway(
                        brain=brain_config,
                        deployed_provider=classifier_provider,
                        settings=_gs(),
                    )
            except Exception as exc:
                logger.warning("behavior_classifier: failed to create provider from brain: %s", exc)

        # Option 2: reuse the coordinator's provider (use_agent_brain=true, default)
        if classifier_provider is None and getattr(behavior_config, "use_agent_brain", True):
            classifier_provider = getattr(ctx, "provider", None)

        if classifier_provider is not None:
            behavior_mod.set_classifier_provider(classifier_provider)
        else:
            logger.warning("behavior_classifier: no provider available, classification disabled")

    logger.info("behavior_module wired profile=%s classify=%s", config.get("profile", "none"), behavior_config.classify_turns)


def _wire_app_middleware(
    ctx: AgentContext,
    compiled: "CompiledApp",
    agent: Any,
) -> None:
    if not compiled.middleware:
        return
    from digitorn.core.middleware import build_app_pipeline
    source_dir = str(compiled.source_path.parent) if compiled.source_path else None
    pipeline = build_app_pipeline(compiled.middleware, custom_base_path=source_dir)
    if pipeline:
        ctx.app_middleware = pipeline
        logger.info(
            "app_middleware agent=%s middlewares=%d",
            agent.agent_id, len(pipeline.middlewares),
        )


def _wire_agent_spawn(
    compiled: "CompiledApp",
    modules: dict[str, Any],
    contexts: dict[str, AgentContext],
    context_builder: "ContextBuilderModule",
    skip_embeddings: bool = False,
) -> None:
    spawn_module = modules.get("agent_spawn")
    if spawn_module is None:
        return

    coordinator = next(
        (a for a in compiled.agents if a.role == "coordinator"), None,
    )
    if coordinator and coordinator.agent_id in contexts:
        coord_ctx = contexts[coordinator.agent_id]
        spawn_module._coordinator_provider = coord_ctx.provider
        spawn_module._coordinator_tools = coord_ctx.tools
        spawn_module._coordinator_modules = modules
        spawn_module._coordinator_native_tool_use = coord_ctx.native_tool_use
        spawn_module._coordinator_tool_injection = coord_ctx.tool_injection
        spawn_module._max_workers = coordinator.pool_max_workers
        spawn_module._relay_progress = coordinator.pool_progress
        spawn_module._auto_retry = coordinator.pool_auto_retry
        spawn_module._notify_fn = context_builder.push_module_notification

    for agent in compiled.agents:
        if agent.role != "specialist":
            continue
        _register_specialist(agent, compiled, modules, spawn_module, context_builder, skip_embeddings=skip_embeddings)


def _register_specialist(
    agent: Any,
    compiled: "CompiledApp",
    modules: dict[str, Any],
    spawn_module: Any,
    context_builder: "ContextBuilderModule",
    skip_embeddings: bool = False,
) -> None:
    spec_provider = _resolve_provider(agent, modules)

    spec_prompt = agent.system_prompt
    if agent.skills_content:
        spec_prompt += "\n\n# Skills\n\n" + agent.skills_content

    action_filter: dict[str, list[str]] = {}  # module_id → allowed actions (empty = all)
    allowed_module_ids: set[str] = set()

    if agent.modules:
        for entry in agent.modules:
            if isinstance(entry, str):
                allowed_module_ids.add(entry)
            elif isinstance(entry, dict):
                for mid, actions in entry.items():
                    allowed_module_ids.add(mid)
                    if isinstance(actions, list):
                        action_filter[mid] = [str(a) for a in actions]
        spec_modules = {
            mid: mod for mid, mod in modules.items()
            if mid in allowed_module_ids or mid == "context_builder"
        }
    else:
        spec_modules = modules

    from digitorn.modules.context_builder.builder import build_index
    spec_index = build_index(
        spec_modules, compiled.security_profile,
        skip_embeddings=skip_embeddings,
        action_filter=action_filter if action_filter else None,
    )

    spec_context = getattr(agent.brain, "context", None)
    _rt = getattr(compiled, "runtime_config", None)
    spec_context_window = getattr(
        _rt, "specialist_context_window", 50_000,
    )
    if spec_context and hasattr(spec_context, "max_tokens"):
        spec_context_window = spec_context.max_tokens

    spec_injection = _choose_tool_injection(
        spec_index.total_tools, spec_context_window,
        model=getattr(spec_provider, "model", None) if spec_provider is not None else None,
    )

    if spec_injection == "direct":
        from digitorn.modules.context_builder.builder import build_direct_tools
        _spec_tc_block = getattr(compiled, "chat_tool_calls", None)
        _spec_inject_intent = (
            bool(getattr(_spec_tc_block, "inject_intent", False)) if _spec_tc_block else False
        )
        spec_tools = build_direct_tools(spec_index, inject_intent=_spec_inject_intent)
    else:
        spec_tools = _build_meta_tools_schema(context_builder)

    spawn_module._specialists[agent.agent_id] = {
        "provider": spec_provider,
        # stash the brain so the spawn path can re-run resolve_session_provider per session (gateway-routed for authed users, KEPT for anon CLI).
        "brain": agent.brain,
        "system_prompt": spec_prompt,
        "tools": spec_tools,
        "modules": spec_modules,
        # spec_modules excludes llm_provider (not a user tool); cache the app's instance so _resolve_specialist_provider can inject it into the resolver.
        "llm_module": modules.get("llm_provider"),
        "native_tool_use": True,
        "tool_injection": spec_injection,
        "specialty": agent.specialty,
    }
    logger.info(
        "agent_spawn: registered specialist %s (%s) with %d tools",
        agent.agent_id, agent.specialty, spec_index.total_tools,
    )


async def _build_hooks(
    compiled: "CompiledApp",
    contexts: dict[str, AgentContext],
    modules: dict[str, Any],
    context_builder: "ContextBuilderModule",
) -> Any:
    from digitorn.core.runtime.hooks import build_hook_runner

    ctx_cfg = compiled.execution.context
    default_context_config = _resolve_context_config(ctx_cfg, modules)

    entry_agent_id = compiled.execution.entry_agent or (
        compiled.agents[0].agent_id if compiled.agents else None
    )

    entry_provider = None
    if entry_agent_id and entry_agent_id in contexts:
        entry_provider = contexts[entry_agent_id].provider

    hook_context_config = (
        contexts[entry_agent_id].context_config
        if entry_agent_id and entry_agent_id in contexts
        else default_context_config
    )

    hooks_list = list(compiled.execution.hooks)
    # single runner keeps priority/cooldown/max_fires globally coherent; per-agent hooks carry agent_id for runtime filtering.
    for agent_def in compiled.agents:
        extra = getattr(agent_def, "hooks", None) or []
        if extra:
            hooks_list.extend(extra)
            logger.info(
                "Registered %d per-agent hook(s) for '%s'",
                len(extra), agent_def.agent_id,
            )

    if hook_context_config.auto_compact:
        has_compact = any(h.action_type == "compact_context" for h in hooks_list)
        if not has_compact:
            from digitorn.core.app.compiler import CompiledHook
            hooks_list.append(CompiledHook(
                id="_auto_compact",
                on="turn_start",
                condition_type="context_pressure",
                condition_params={"threshold": hook_context_config.compression_trigger},
                action_type="compact_context",
                action_params={
                    "strategy": hook_context_config.strategy,
                    "keep_recent": hook_context_config.keep_recent,
                    "summary_max_tokens": hook_context_config.summary_max_tokens,
                },
                cooldown=30.0,
                # 180s timeout - compaction's summarisation LLM call needs headroom for slow providers without stalling every turn.
                timeout=180.0,
            ))
            logger.info(
                "Auto-compact hook injected (trigger=%.0f%%, strategy=%s, keep=%d)",
                hook_context_config.compression_trigger * 100,
                hook_context_config.strategy,
                hook_context_config.keep_recent,
            )

    summary_provider = await _resolve_summary_provider(
        compiled, contexts, modules, default_context_config,
    )

    return build_hook_runner(
        hooks_list,
        provider=entry_provider,
        summary_provider=summary_provider,
        context_builder=context_builder,
    )


async def _resolve_summary_provider(
    compiled: Any,
    contexts: dict[str, Any],
    modules: dict[str, Any],
    default_context_config: Any,
) -> Any | None:
    """Resolve the summary_brain to a provider, if configured."""
    entry_agent_id = compiled.execution.entry_agent or (
        compiled.agents[0].agent_id if compiled.agents else None
    )

    summary_brain = None

    if entry_agent_id:
        for agent in compiled.agents:
            if agent.agent_id == entry_agent_id and agent.brain.context:
                summary_brain = agent.brain.context.summary_brain
                break

    if summary_brain is None and compiled.execution.context:
        summary_brain = compiled.execution.context.summary_brain

    if summary_brain is None:
        return None

    llm_module = modules.get("llm_provider")
    if llm_module is None:
        logger.warning("summary_brain: llm_provider module not available")
        return None

    try:
        provider_id = f"_summary_{summary_brain.provider or 'default'}"

        config: dict[str, Any] = {
            "provider_id": provider_id,
            "backend": summary_brain.backend,
            "model": summary_brain.model,
        }
        if summary_brain.provider:
            config["provider_hint"] = summary_brain.provider
        if summary_brain.timeout:
            config["timeout"] = summary_brain.timeout
        config.update(summary_brain.config)

        await llm_module.execute("configure", config)

        providers = getattr(llm_module, "_providers", {})
        provider = providers.get(provider_id)
        if provider:
            # route auto-compact summarisation through the gateway so quota + cost accounting cover it.
            from digitorn.core.credentials.gateway_resolver import (
                route_derived_brain_through_gateway,
            )
            from digitorn.core.config import get_settings as _get_settings
            routed = await route_derived_brain_through_gateway(
                brain=summary_brain,
                deployed_provider=provider,
                settings=_get_settings(),
            )
            via_gw = routed is not provider
            logger.info(
                "summary_brain: resolved provider '%s' (model=%s) via_gateway=%s",
                provider_id, summary_brain.model, via_gw,
            )
            return routed
    except Exception as exc:
        logger.warning("summary_brain: failed to resolve provider: %s", exc, exc_info=True)

    return None


async def _resolve_provider_from_brain(brain: Any, modules: dict[str, Any]) -> Any:
    """Resolve an AgentBrain config to a live LLM provider (no agent required)."""
    import inspect as _inspect
    llm_module = modules.get("llm_provider")
    if llm_module is None:
        raise RuntimeError("llm_provider module required for behavior brain")
    create = getattr(llm_module, "create_provider_from_brain", None)
    if create and callable(create):
        result = create(brain)
        if _inspect.iscoroutine(result):
            result = await result
        return result
    # Fallback: register inline provider
    register = getattr(llm_module, "_register_inline_provider", None)
    if register and callable(register):
        pid = f"behavior_classifier_{id(brain)}"
        reg_result = register(pid, brain)
        if _inspect.iscoroutine(reg_result):
            await reg_result
        providers = getattr(llm_module, "_providers", {})
        return providers.get(pid)
    raise RuntimeError("Cannot create provider from behavior brain config")


def _resolve_provider(agent: Any, modules: dict[str, Any]) -> Any:
    """Resolve an agent's brain to a live LLM provider instance."""
    llm_module = modules.get("llm_provider")
    if llm_module is None:
        raise RuntimeError(
            f"Agent '{agent.agent_id}' requires llm_provider module"
        )

    providers = getattr(llm_module, "_providers", {})
    provider = providers.get(agent.brain.provider_id)
    if provider is None:
        available = list(providers.keys())
        raise RuntimeError(
            f"Agent '{agent.agent_id}': provider '{agent.brain.provider_id}' "
            f"not found (available: {available})"
        )
    return provider


def _has_native_tool_use(provider: Any) -> bool:
    """Check if a provider supports native tool calling via API."""
    try:
        info = provider.get_info()
        return info.capabilities.tool_use
    except Exception as exc:
        logger.debug("Could not check tool_use capability: %s", exc, exc_info=True)
        return True


def _resolve_context_config(
    ctx_cfg: Any,
    modules: dict[str, Any],
) -> ContextWindowConfig:
    return ContextWindowConfig(
        max_tokens=ctx_cfg.max_tokens if ctx_cfg.max_tokens > 0 else 128_000,
        output_reserved=ctx_cfg.output_reserved,
        strategy=ctx_cfg.strategy,
        keep_recent=ctx_cfg.keep_recent,
        compression_trigger=ctx_cfg.compression_trigger,
        summary_max_tokens=ctx_cfg.summary_max_tokens,
        auto_compact=ctx_cfg.auto_compact,
    )


def _refine_context_config_for_provider(
    base_config: ContextWindowConfig,
    provider: Any,
    *,
    yaml_max_tokens: int = 0,
) -> ContextWindowConfig:
    """Refine context config with provider-reported capabilities."""
    if yaml_max_tokens > 0:
        return base_config

    try:
        info = provider.get_info()
        cap = info.capabilities
        if cap.max_context_window > 0:
            return ContextWindowConfig(
                max_tokens=cap.max_context_window,
                output_reserved=base_config.output_reserved,
                strategy=base_config.strategy,
                keep_recent=base_config.keep_recent,
                compression_trigger=base_config.compression_trigger,
                summary_max_tokens=base_config.summary_max_tokens,
                auto_compact=base_config.auto_compact,
            )
    except Exception as exc:
        logger.debug("Context config resolution fallback: %s", exc, exc_info=True)

    return base_config


#
_FALLBACK_TOKENS_PER_TOOL = 200
_MAX_CONTEXT_RATIO = 0.20
_CHARS_PER_TOKEN = 4


def _estimate_tools_tokens(
    direct_tools: list[dict] | None,
    *,
    model: str | None = None,
) -> int:
    """Real token cost for all tool schemas."""
    if not direct_tools:
        return 0

    import json
    payload = json.dumps(direct_tools, ensure_ascii=False)

    if model:
        try:
            from litellm import token_counter
            return int(token_counter(model=model, text=payload))
        except Exception as exc:
            logger.debug(
                "_estimate_tools_tokens: litellm fallback (%s); using char/4",
                exc,
            )

    return len(payload) // _CHARS_PER_TOKEN


async def _achoose_tool_injection(
    total_tools: int,
    context_window: int,
    direct_tools: list[dict] | None = None,
    *,
    model: str | None = None,
) -> str:
    """Async wrapper around :func:`_choose_tool_injection`."""
    import asyncio as _asyncio
    return await _asyncio.to_thread(
        _choose_tool_injection,
        total_tools,
        context_window,
        direct_tools,
        model=model,
    )


def _choose_tool_injection(
    total_tools: int,
    context_window: int,
    direct_tools: list[dict] | None = None,
    *,
    model: str | None = None,
) -> str:
    """Choose between direct, compact_direct, and discovery mode."""
    if direct_tools:
        tool_tokens = _estimate_tools_tokens(direct_tools, model=model)
    else:
        tool_tokens = total_tools * _FALLBACK_TOKENS_PER_TOOL

    budget = int(context_window * _MAX_CONTEXT_RATIO)

    logger.debug(
        "tool_injection_decision tools=%d estimated_tokens=%d budget=%d",
        total_tools, tool_tokens, budget,
    )

    if tool_tokens <= budget:
        return "direct"
    # Compact direct: tool names + one-liners fit, but full schemas don't
    # Estimate ~30 tokens per tool for compact format (name + description)
    compact_tokens = total_tools * 30
    if compact_tokens <= budget:
        return "compact_direct"
    return "discovery"


def _build_meta_tools_schema(
    context_builder: "ContextBuilderModule",
) -> list[dict[str, Any]]:
    """Build meta-tools schema by introspecting context_builder's @action registry."""
    from digitorn.modules.context_builder.tool_schema import action_entry_to_json_schema

    registry = getattr(context_builder, "_action_registry", {})
    tools: list[dict[str, Any]] = []

    for name, entry in registry.items():
        # Skip actions explicitly marked as internal - they remain callable
        # via bus.call() but are hidden from the LLM tool list.
        if entry.spec and getattr(entry.spec, "internal", False):
            continue
        schema = action_entry_to_json_schema(entry)
        description = entry.spec.description if entry.spec else ""
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": schema,
            },
        })

    return tools


def _build_module_tools_schema(
    module: Any,
    prefix: str | None = None,
    use_short_names: bool = False,
) -> list[dict[str, Any]]:
    """Build tool schemas from a module's @action registry."""
    from digitorn.modules.context_builder.tool_schema import action_entry_to_json_schema

    registry = getattr(module, "_action_registry", {})
    tools: list[dict[str, Any]] = []

    if use_short_names and prefix:
        from digitorn.core.runtime.tool_names import to_short

    for name, entry in registry.items():
        # Skip actions explicitly marked as internal - they remain callable
        # via bus.call() but are hidden from the LLM tool list.
        if entry.spec and getattr(entry.spec, "internal", False):
            continue
        schema = action_entry_to_json_schema(entry)
        description = entry.spec.description if entry.spec else ""
        if use_short_names and prefix:
            tool_name = to_short(f"{prefix}.{name}")
        elif prefix:
            tool_name = f"{prefix}__{name}"
        else:
            tool_name = name
        tools.append({
            "type": "function",
            "function": {
                "name": tool_name,
                "description": description,
                "parameters": schema,
            },
        })

    return tools


_DISCOVERY_META_ACTIONS = frozenset({
    "search_tools",
    "get_tool",
    "execute_tool",
    "list_categories",
    "browse_category",
})

_BASE_PRIMITIVE_ACTIONS = frozenset({
    "run_parallel",
    "background_run",
})

_WATCHER_ACTIONS = frozenset({
    "watch_start",
    "watch_stop",
    "watch_pause",
    "watch_resume",
    "watch_status",
    "watch_list",
    "watch_history",
})

_CHANNEL_ACTIONS = frozenset({
    "send_notification",
})

_SKILL_ACTIONS = frozenset({
    "use_skill",
})

def _build_primitive_tools_schema(
    context_builder: "ContextBuilderModule",
    *,
    watchers_enabled: bool = False,
    channels_enabled: bool = False,
    skills_enabled: bool = False,
) -> list[dict[str, Any]]:
    """Build schemas for strategic/primitive tools."""
    allowed = _BASE_PRIMITIVE_ACTIONS
    if watchers_enabled:
        allowed = allowed | _WATCHER_ACTIONS
    if channels_enabled:
        allowed = allowed | _CHANNEL_ACTIONS
    if skills_enabled:
        allowed = allowed | _SKILL_ACTIONS
    all_meta = _build_meta_tools_schema(context_builder)
    return [
        t for t in all_meta
        if t.get("function", {}).get("name") in allowed
    ]


_PROJECT_MEMORY_MAX_CHARS = 4000  # ~1000 tokens - keeps system prompt lean


def _truncate_project_memory(content: str, max_chars: int = _PROJECT_MEMORY_MAX_CHARS) -> str:
    if len(content) <= max_chars:
        return content
    # Cut at the last newline before the limit so we don't split mid-sentence.
    cut = content[:max_chars].rfind("\n")
    if cut <= 0:
        cut = max_chars
    return content[:cut].rstrip() + "\n\n[truncated - full file has {:,} chars]".format(len(content))


def _load_project_memory(compiled: "CompiledApp") -> str | None:
    """Load project memory file from workspace."""
    setting = getattr(compiled.execution, "project_memory", "auto")
    if not setting:
        return None

    workspace = _resolve_workspace(compiled)

    from digitorn.core.workspace import WorkspaceLayout
    app_id = compiled.app_id
    layout = WorkspaceLayout(workspace, app_id)

    if setting == "auto":
        # App-specific memory is always safe to auto-load - it lives
        # under .digitorn/apps/{app_id}/ and is user-owned.
        if layout.app_memory_file.exists():
            try:
                content = layout.app_memory_file.read_text(encoding="utf-8", errors="replace").strip()
                if content:
                    content = _truncate_project_memory(content)
                    logger.info("project_memory_loaded path=%s size=%d", layout.app_memory_file, len(content))
                    return content
            except OSError as exc:
                logger.warning("project_memory_read_failed path=%s: %s", layout.app_memory_file, exc)

        # SECURITY: auto path only reads `.digitorn.md`. Auto-loading CLAUDE.md / README.md from the workspace risks cross-user leaks.
        for name in [".digitorn.md"]:
            path = Path(workspace) / name
            if path.exists() and path.is_file():
                try:
                    content = path.read_text(encoding="utf-8", errors="replace").strip()
                    if content:
                        content = _truncate_project_memory(content)
                        logger.info("project_memory_loaded path=%s size=%d", path, len(content))
                        return content
                except OSError as exc:
                    logger.warning("project_memory_read_failed path=%s: %s", path, exc)
        return None

    candidates = [setting]

    for name in candidates:
        path = Path(workspace) / name
        if path.exists() and path.is_file():
            try:
                content = path.read_text(encoding="utf-8", errors="replace").strip()
                if content:
                    content = _truncate_project_memory(content)
                    logger.info("project_memory_loaded path=%s size=%d", path, len(content))
                    return content
            except OSError as exc:
                logger.warning("project_memory_failed path=%s error=%s", path, exc)
    return None


async def _auto_index_workspace(
    compiled: "CompiledApp",
    modules: dict[str, Any],
) -> None:
    """Auto-register and scan the workspace in the index module."""
    index_module = modules.get("index")
    fs_module = modules.get("filesystem")

    if index_module is None or fs_module is None:
        return

    import os

    workspace = _resolve_workspace(compiled)

    workspace_path = Path(workspace)
    if not workspace_path.is_dir():
        logger.warning(
            "auto_index: workspace '%s' is not a directory, skipping", workspace
        )
        return

    store = getattr(index_module, "store", None)
    if store and store.get_source("workspace"):
        logger.debug("auto_index: workspace source already registered, skipping")
        return

    source_id = "workspace"

    try:
        await index_module.execute("register_source", {
            "source_id": source_id,
            "module_id": "filesystem",
            "root": workspace,
            "extractor": "auto",
            "watch": True,
            "watch_mode": "ephemeral",
        })

        result = await index_module.execute("scan", {
            "source_id": source_id,
            "force": False,
        })

        if hasattr(result, "data") and isinstance(result.data, dict):
            data = result.data
            logger.info(
                "auto_index: workspace '%s' indexed - %d files, %d entries",
                workspace,
                data.get("files_scanned", 0),
                data.get("total_entries", 0),
            )
        else:
            logger.info("auto_index: workspace '%s' indexed", workspace)

    except Exception as exc:
        logger.warning("auto_index: workspace indexing failed: %s", exc, exc_info=True)
        return

    fs_module._index_store = index_module.store  # type: ignore[attr-defined]


def _build_channels_info(compiled: "CompiledApp") -> list[dict[str, Any]]:
    """Build channel info for prompt injection."""
    if not compiled.channels:
        return []

    from digitorn.core.app.channels import ChannelRegistry

    temp_registry = ChannelRegistry()
    from digitorn.core.app.channels.llm import LLMNotificationChannel
    from digitorn.core.app.channels.webhook import WebhookChannel
    from digitorn.core.app.channels.log import LogChannel

    for cls in (LLMNotificationChannel, WebhookChannel, LogChannel):
        temp_registry.register_type(cls)
    temp_registry.discover_plugins()

    result: list[dict[str, Any]] = []
    for name, ch in compiled.channels.items():
        info: dict[str, Any] = {
            "name": name,
            "type": ch.channel_type,
        }
        cls = temp_registry.get_type(ch.channel_type)
        if cls is not None:
            try:
                instance = cls()
                info["per_delivery_config"] = instance.per_delivery_config_schema()
            except Exception as exc:
                logger.debug("Channel schema introspection failed for %s: %s", ch.channel_type, exc, exc_info=True)
        info["has_user_resolver"] = ch.user_resolver is not None
        result.append(info)

    return result


def _any_require_approval(modules: dict[str, Any]) -> bool:
    for module in modules.values():
        registry = getattr(module, "_action_registry", {})
        for entry in registry.values():
            if entry.spec and entry.spec.require_approval:
                return True
    return False


async def _auto_describe_databases(
    modules: dict[str, Any],
) -> str:
    """Auto-introspect connected databases and build a schema summary."""
    db_module = modules.get("database")
    if db_module is None:
        return ""

    pool = getattr(db_module, "pool", None)
    if pool is None:
        return ""

    connections = pool.list_connections()
    if not connections:
        return ""

    annotations = getattr(db_module, "_annotations", {})
    lines: list[str] = ["DATABASE SCHEMA:"]

    for conn_info in connections:
        conn_id = conn_info.get("connection_id", "") if isinstance(conn_info, dict) else getattr(conn_info, "connection_id", "")
        driver = conn_info.get("driver", "") if isinstance(conn_info, dict) else getattr(conn_info, "driver", "")

        try:
            adapter = pool.get_adapter(conn_id)
            schema = await adapter.introspect()
        except Exception as exc:
            logger.warning("auto_describe: introspect %s failed: %s", conn_id, exc, exc_info=True)
            continue

        lines.append(f"\n[{conn_id}] ({driver})")
        conn_ann = annotations.get(conn_id, {})

        for table in schema.tables:
            table_name = table.name
            table_ann = conn_ann.get(table_name, {}).get("__table__", {})
            table_desc = table_ann.get("description", "") or table.comment or ""
            desc_suffix = f" - {table_desc}" if table_desc else ""
            lines.append(f"  {table_name}{desc_suffix}")

            for col in table.columns:
                col_ann = conn_ann.get(table_name, {}).get(col.name, {})
                col_desc = col_ann.get("description", "") or col.comment or ""

                parts = [f"    - {col.name} {col.type}"]
                if col.primary_key:
                    parts.append("PK")
                if not col.nullable:
                    parts.append("NOT NULL")
                col_line = " ".join(parts)
                if col_desc:
                    col_line += f" - {col_desc}"
                lines.append(col_line)

            if table.foreign_keys:
                for fk in table.foreign_keys:
                    lines.append(f"    FK: {fk.column} → {fk.referred_table}.{fk.referred_column}")

    if len(lines) <= 1:
        return ""

    return "\n".join(lines)


async def _probe_mcp_schemas(
    modules: dict[str, Any],
    index: Any,
) -> None:
    """Probe MCP servers to discover API response structures."""
    mcp_module = modules.get("mcp")
    if mcp_module is None:
        return

    pool = getattr(mcp_module, "_pool", None)
    if pool is None:
        return

    from digitorn.modules.mcp.schema_probe import probe_mcp_server

    servers = getattr(pool, "_servers", {})
    for server_id, entry in servers.items():
        if entry.status != "connected":
            continue

        virtual_module_id = f"mcp_{server_id}"
        try:
            hints = await probe_mcp_server(pool, server_id)
            if hints:
                index.mcp_structural_hints[virtual_module_id] = hints
                logger.info(
                    "schema_probe: %s - %d structures discovered",
                    virtual_module_id, len(hints),
                )
        except Exception as exc:
            logger.debug(
                "schema_probe: %s failed: %s", virtual_module_id, exc, exc_info=True,
            )


def _summarize_setup_step(
    module_id: str, action: str, params: dict[str, Any],
) -> str:
    """Build a human-readable one-liner for a completed setup step."""
    _SENSITIVE = {
        "password", "password_env", "api_key", "secret", "token",
        "api_secret", "client_secret", "jwt_secret", "auth_token",
        "access_token", "refresh_token", "private_key",
    }

    def _mask(k: str, v: Any) -> Any:
        if k in _SENSITIVE:
            return "***"
        if isinstance(v, str) and "@" in v and "://" in v:
            parts = v.split("@", 1)
            scheme_user = parts[0].rsplit(":", 1)[0]
            return f"{scheme_user}:****@{parts[1]}"
        return v

    safe_params = {k: _mask(k, v) for k, v in params.items()}
    parts = [f"{module_id}.{action}"]
    for k, v in safe_params.items():
        parts.append(f"{k}={v}")
    return " | ".join(parts)

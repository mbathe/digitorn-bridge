"""Bootstrap - CompiledApp → RuntimeApp.

Transforms the compiled IR into live, ready-to-execute objects:
1. Instantiate and start modules
2. Push configs, run setup steps
3. Build tool index via context_builder
4. Build AgentContext for each agent
5. Assemble RuntimeApp
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from digitorn.core.runtime.types import AgentContext, ContextWindowConfig

if TYPE_CHECKING:
    from digitorn.core.app.compiler import CompiledApp
    from digitorn.modules.base import BaseModule
    from digitorn.modules.context_builder.module import ContextBuilderModule
    from digitorn.modules.registry import ModuleRegistry


def _resolve_workspace(compiled: "CompiledApp", *, standalone: bool = False) -> str:
    """Resolve the workspace directory for an app.

    Respects workspace_mode:
    - none: returns empty string
    - fixed: YAML workspace only
    - required/auto: YAML > cwd (standalone) > managed dir (daemon)
    """
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
    """Bootstrap a compiled app into live runtime objects.

    Each app gets fresh module instances via ``registry.create()``
    so that concurrent deployments are fully isolated.

    Args:
        skip_embeddings: Skip loading the semantic search model (~900MB).
            Used by sandbox workers that only execute tools by name.
    """
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

    # Attach the hook runner onto the context_builder so modules / hook
    # actions that want to fire cross-event hooks (workspace, filesystem,
    # preview, approval_queue, …) can reach it without threading the
    # runner through every signature.
    if hook_runner is not None:
        setattr(context_builder, "hook_runner", hook_runner)

    # Post-wire: fire `approval_request` hook every time a tool goes up
    # for approval. The ApprovalQueue exposes a callback list; we just
    # register a lightweight async callback that constructs a minimal
    # TurnState and forwards to the runner. Skip entirely when the app
    # declared no capabilities block - _build_approval_queue returns
    # None in that case and there's nothing to wire.
    if hook_runner is not None and approval_queue is not None:
        async def _approval_hook_callback(request: Any) -> None:
            try:
                from digitorn.core.runtime.hooks import TurnState
                from types import SimpleNamespace
                # Build a synthetic tool_context so {{tool.*}} templates
                # in hook actions can reference the awaiting tool.
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
    """Create, wire, start, and configure all modules."""
    modules: dict[str, BaseModule] = {}
    errors: list[str] = []

    # Workers routing: lazy-populate the worker registry from current
    # Settings. Empty registry = no routing = legacy in-process flow
    # (the default; ``workers.enabled`` is False unless the operator
    # opts in). Idempotent across app deploys.
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

    # ``llm_provider`` is a system module: every agent's brain needs it
    # at session-start (gateway resolver, fallback brain, summary
    # provider, classifier...). The compiler now auto-injects it into
    # compiled.modules, but we keep this bootstrap-side injection too so
    # legacy bundles that pre-date the compiler change still get the
    # module wired. MODULE_SINGLETON = the daemon's one instance is
    # reused, no per-app cost. Hidden from the LLM tool catalogue by
    # ``context_builder._HIDDEN_MODULES``.
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

    # Worker routing: for every module hosted by a worker (per
    # ``settings.workers``), replace its action handlers with HTTP
    # forwarders. The instance stays the same class with the same
    # manifest / specs / params_models / constraints -- only the
    # action implementations move to the worker process. When the
    # registry is empty (default), this loop is a no-op and every
    # module runs in-process exactly as before.
    #
    # ``llm_provider`` is **excluded** from the module-level wrap.
    # Reason: the agent loop bypasses ``llm_module.execute("chat",
    # ...)`` and calls ``ctx.provider.chat_stream(...)`` directly on
    # the provider instance. The provider-level proxy
    # (``maybe_wrap_provider`` at line 564 below) is what intercepts
    # that path. Wrapping the MODULE here would set ``_skip_on_start``
    # = True on the llm_provider instance, which makes the bootstrap
    # lifecycle loop skip ``on_config_update`` -- leaving
    # ``_providers`` empty and crashing ``_resolve_provider``
    # immediately after. We get all the benefit (chat_stream off the
    # main loop) via the provider proxy, with none of the breakage.
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
        # Inject constraints before on_start (modules read them during init)
        mod_constraints = compiled.modules[module_id].constraints
        if mod_constraints:
            module._constraints = mod_constraints

        # Workered modules: the worker process runs the real
        # ``on_start`` -- the daemon-side instance is only a routing
        # handle, so we skip the lifecycle hook to avoid double-start
        # (e.g. cron sweepers spawned in both processes, MCP stdio
        # subprocess opened twice). The wrap stamps ``_skip_on_start``
        # in action_wrapper.py.
        if getattr(module, "_skip_on_start", False):
            module._started_ok = True  # type: ignore[attr-defined]
            logger.debug(
                "module_on_start_skipped module=%s reason=workered",
                module_id,
            )
            continue

        try:
            await module.on_start()
            # Mark module as successfully started - usable by the runtime
            module._started_ok = True  # type: ignore[attr-defined]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Module '%s' on_start failed: %s", module_id, exc, exc_info=True)
            # Mark the module as half-loaded so callers can detect it
            module._started_ok = False  # type: ignore[attr-defined]
            module._start_error = str(exc)  # type: ignore[attr-defined]
            failed_modules.append(module_id)
            # Skip config update for failed modules - would likely also crash
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
    """Resolve workspace path for a specific module.

    For ``workspace_mode == "auto"`` without an explicit yaml workspace
    we return ``WORKSPACE_PLACEHOLDER`` ("{WORKSPACE}") instead of
    ``Path.cwd()``. Reason: ``build_system_prompt`` runs at bootstrap,
    before any session exists, and whatever module-level workspace we
    set here gets baked into the system prompt AS A LITERAL STRING.
    Using the daemon's cwd here previously meant every session's agent
    saw "Session workspace: <daemon cwd>" (typically the repo root),
    even though the per-session dir was wired correctly at turn time -
    manager.chat's late ``prompt.replace({WORKSPACE}, ...)`` had
    nothing to substitute because the placeholder had already been
    resolved to the wrong path. Keeping the placeholder lets the
    per-session substitution in ``manager._chat_locked`` do its job.
    """
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


# ── Phase 2: Setup steps ────────────────────────────────────────────


async def _run_setup_phase(
    compiled: "CompiledApp",
    modules: dict[str, Any],
) -> list[str]:
    """Execute setup steps, introspect databases, collect context snippets."""
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


# ── Phase 3: Context layer ──────────────────────────────────────────


async def _build_context_layer(
    compiled: "CompiledApp",
    modules: dict[str, Any],
    service_bus: Any,
    skip_embeddings: bool = False,
) -> tuple["ContextBuilderModule", Any]:
    """Create context_builder, build index, wire notifications."""
    from digitorn.modules.context_builder.module import ContextBuilderModule

    context_builder = ContextBuilderModule()
    await context_builder.on_start()
    context_builder._service_bus = service_bus
    service_bus.register_service("context_builder", context_builder)

    # Include context_builder in modules dict so its granted actions (e.g. ask_user) get indexed
    build_modules = {**modules, "context_builder": context_builder}
    # ``build_and_set_index`` does a SYNCHRONOUS load of the
    # ~250 MB sentence-transformers embedding model on first call
    # (downloads from HF cache + initialises ONNX runtime). On the
    # main asyncio loop that's a 30-60s stall on cold start. Punt to
    # a thread so the daemon stays responsive during deploy.
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
    """Inject app_id into modules that need app-scoped identity.

    ``workspace`` is in this list because its ``_resolve_sync_dir``
    auto-isolates files under ``~/.digitorn/workspaces/{app_id}/{sid}/``
    - without the override, the shared singleton uses its default
    ``_app_id='default'`` and ALL apps collide in the same bucket.
    """
    for mod_id in ("cron_native", "cache", "vector", "workspace", "channels", "web_preview"):
        mod = modules.get(mod_id)
        if mod is not None:
            mod._app_id_override = compiled.app_id


# ── Phase 4: Approval queue ─────────────────────────────────────────


def _build_approval_queue(
    compiled: "CompiledApp",
    modules: dict[str, Any],
) -> Any:
    """Build an ApprovalQueue if the app has security or approval-requiring actions.

    Timeout resolution:
      1. ``compiled.security_profile.approval_timeout`` if the app's
         YAML sets a security profile (per-app override).
      2. Otherwise ``settings.session.approval_timeout_s`` from the
         daemon config - tune in ``~/.digitorn/config.yaml`` or via
         ``DIGITORN_SESSION__APPROVAL_TIMEOUT_S=...``.
    """
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


# ── Phase 5: Agent contexts ─────────────────────────────────────────


async def _build_agent_contexts(
    compiled: "CompiledApp",
    modules: dict[str, Any],
    context_builder: "ContextBuilderModule",
    index: Any,
    setup_summary: list[str],
    approval_queue: Any,
) -> dict[str, AgentContext]:
    """Build an AgentContext for each agent in the compiled app."""
    from digitorn.modules.context_builder.builder import build_direct_tools
    from digitorn.modules.context_builder.prompt import build_system_prompt

    meta_tools = _build_meta_tools_schema(context_builder)
    # Read the ``inject_intent`` flag from the compiled app config so
    # the per-tool schemas get the ``intent`` first-property when the
    # app opts in. Falls back to False (current behaviour) when the
    # block is absent or the flag is unset.
    # NOTE: ``CompiledApp`` flattened the old ``compiled.ui.*`` namespace
    # into top-level ``compiled.chat_*`` attributes — read directly.
    _tc_block = getattr(compiled, "chat_tool_calls", None)
    _inject_intent = bool(getattr(_tc_block, "inject_intent", False)) if _tc_block else False
    direct_tools = build_direct_tools(index, inject_intent=_inject_intent)

    ctx_cfg = compiled.execution.context
    default_context_config = _resolve_context_config(ctx_cfg, modules)

    watchers_enabled = compiled.execution.watchers
    scheduler_enabled = compiled.execution.scheduler
    # Set feature flags on context_builder so mixin prompt sections can gate themselves
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

    # Refuse to return an empty contexts dict. Upstream code relies on
    # at least one executable agent being present; without this guard
    # the DeployedApp registers in `_deployed` but `entry_context`
    # raises StopIteration on first use → `GET /api/apps` says
    # deployed, `POST /messages` silently fails ("ghost app").
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
    """Build a single AgentContext for one agent definition."""
    provider = _resolve_provider(agent, modules)

    # Workers: when ``llm_provider`` is hosted by an out-of-process
    # worker (``workers.workers[].modules`` includes ``llm_provider``),
    # transparently replace the daemon-side provider instance with an
    # ``LLMProviderProxy`` that streams ``chat_stream`` / ``chat``
    # calls over HTTP/NDJSON to the worker. The proxy duck-types the
    # full ``BaseLLMProvider`` surface (``chat_stream``, ``chat``,
    # ``count_tokens``, ``count_message_tokens``, ``get_info``,
    # ``close``, ``clone``, plus ``model`` / ``provider_id`` /
    # ``provider_name`` attributes and dynamic ``_known_tool_names``
    # marker), so every downstream caller (agent_loop, streaming.py,
    # hooks, agent_spawn, behavior classifier) sees the same shape.
    #
    # No-op (returns the original provider) when ``llm_provider`` is
    # not workered, when the workers registry is empty, or when the
    # wrap helper hits an error -- legacy behaviour preserved.
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

    # Wire fallback brain for billing/credit exhaustion. The YAML shape is
    # the same AgentBrain schema as the primary - not a "provider_id
    # reference" with an ``inline_config`` field (that older idea never
    # made it into the schema). We unconditionally resolve the fallback
    # AgentBrain into a provider via the same builder used for the main
    # brain so a 402 on primary can swap to Claude / Haiku seamlessly.
    ctx._fallback_brain = None
    if agent.brain.fallback is not None:
        try:
            fb = agent.brain.fallback
            llm_mod = modules.get("llm_provider")
            if llm_mod is None:
                raise RuntimeError("llm_provider module required for fallback brain")
            deployed_fb = await llm_mod.create_provider_from_brain(fb)
            # Default-via-gateway: swap the YAML-declared direct provider for a
            # gateway-routed one unless the brain is local (ollama, vllm, …) or
            # the operator killed the gateway. The BYOK toggle is per-(user,
            # app) and lives on the MAIN brain only; derived brains always go
            # through the gateway so quota + cost accounting cover them.
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

    # Wire web_preview → shell. Lets the idle reaper kill the agent's
    # bash tasks when an attachment is reaped for inactivity AND lets
    # the proxy() action verify the bash task is alive before
    # attaching (catches port-collision / silent-spawn-fail cases).
    # web_preview is a daemon-wide singleton; shell is per-app, so we
    # also register the shell under the current app_id in the
    # _shells_by_app dict so cross-module lookups stay correct when
    # multiple apps are deployed at once.
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
        except Exception:
            pass

    # Wire shell → workspace. When `ctx.workspace` is missing on a
    # message (some UI flows omit it after the initial session create),
    # shell asks the workspace module for the canonical session sync
    # dir so `cd <subdir>` lines up with where WsWrite actually wrote.
    if shell_mod is not None and workspace_mod is not None:
        shell_mod._workspace_module = workspace_mod

    # Wire workspace module → lsp module (diagnostics on write/edit)
    lsp_mod = modules.get("lsp")
    if workspace_mod is not None and lsp_mod is not None:
        workspace_mod._lsp = lsp_mod

    # Wire filesystem module → lsp + preview (real-disk writes also get
    # diagnostics pushed to the `diagnostics` channel so Flutter / web
    # clients show markers even when the agent uses the real filesystem
    # toolset instead of the virtual workspace).
    fs_mod = modules.get("filesystem")
    if fs_mod is not None:
        if lsp_mod is not None:
            fs_mod._lsp = lsp_mod
        if ctx.preview_module is not None:
            fs_mod._preview = ctx.preview_module

    # Inject top-level workspace: block into the workspace module's fields.
    # The top-level block is the source of truth for render_mode/entry_file/title;
    # the module reads these when emitting the metadata SSE event on first write.
    # We set the fields directly (no await) because this runs in a sync function.
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
    """Assemble the final tool list for an agent."""
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
    """Build operational/strategic tools that are always directly available."""
    extras: list[dict[str, Any]] = []

    if tool_injection == "discovery":
        extras.extend(_build_primitive_tools_schema(
            context_builder,
            watchers_enabled=watchers_enabled,
            channels_enabled=channels_enabled,
            skills_enabled=skills_enabled,
        ))

    # In direct/compact_direct mode, memory and agent_spawn tools are already
    # in direct_tools (via build_direct_tools → index) with short API names
    # (SetGoal, Remember, Agent, etc.).  Only inject them explicitly for
    # discovery mode where the agent has meta-tools only.
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
    """Collect all hidden action names from compiled config."""
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
    """Build the system prompt for a single agent."""
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
    """Log the final tool configuration for an agent."""
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
    """Build and attach the direct modules routing map."""
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
    """Load and attach skills from YAML and filesystem."""
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
    """Configure and attach the memory module to context."""
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
    """Configure and attach the behavior enforcement module.

    Wrapped in a top-level try/except so a behavior config error
    never prevents the app from deploying.
    """
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
                # Default-via-gateway: same rule as fallback_brain /
                # summary_provider. The session-time swap in _chat.py
                # already routes per-user, but if that path fails the
                # behavior module would fall back to THIS deploy-time
                # instance. Make sure even the fallback honours the
                # "tout via la gateway" rule.
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
    """Build and attach app-level middleware pipeline."""
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


# ── Phase 6: Agent spawn + hooks ────────────────────────────────────


def _wire_agent_spawn(
    compiled: "CompiledApp",
    modules: dict[str, Any],
    contexts: dict[str, AgentContext],
    context_builder: "ContextBuilderModule",
    skip_embeddings: bool = False,
) -> None:
    """Wire coordinator and specialist configs into the agent_spawn module."""
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
    """Register a single specialist agent with the spawn module."""
    spec_provider = _resolve_provider(agent, modules)

    spec_prompt = agent.system_prompt
    if agent.skills_content:
        spec_prompt += "\n\n# Skills\n\n" + agent.skills_content

    # Parse modules list - supports simple strings and granular dicts:
    #   modules: [filesystem, shell]                       → full module access
    #   modules: [{filesystem: [read, grep, glob]}, shell] → restrict actions
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
        # Same source as the main-agent direct_tools call above —
        # respect the per-app ``inject_intent`` flag for sub-agent
        # tool schemas too. Reads the flattened top-level
        # ``compiled.chat_tool_calls`` (the old ``compiled.ui.*`` namespace
        # was removed when CompiledApp was flattened).
        _spec_tc_block = getattr(compiled, "chat_tool_calls", None)
        _spec_inject_intent = (
            bool(getattr(_spec_tc_block, "inject_intent", False)) if _spec_tc_block else False
        )
        spec_tools = build_direct_tools(spec_index, inject_intent=_spec_inject_intent)
    else:
        spec_tools = _build_meta_tools_schema(context_builder)

    spawn_module._specialists[agent.agent_id] = {
        "provider": spec_provider,
        # Stash the brain so the spawn path can re-run
        # ``resolve_session_provider`` per session: a specialist that
        # declared ``provider: github_copilot`` in YAML will still get
        # gateway-routed for authenticated users, BYOK-honoured for
        # users who opted out, and KEPT for anonymous CLI calls. Without
        # this, every specialist hit upstream directly with the YAML
        # credential -- bypassing quota tracking and the JWT auth gate.
        "brain": agent.brain,
        "system_prompt": spec_prompt,
        "tools": spec_tools,
        "modules": spec_modules,
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
    """Build the hook runner with auto-compact injection."""
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
    # Merge per-agent hooks into the same runner. Each per-agent hook
    # carries its own ``agent_id`` so the runtime filter fires it only
    # during that agent's turns. The single-runner design keeps
    # priority / cooldown / max_fires globally coherent - no risk of
    # two parallel runners drifting.
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
                # Compaction can run a summarization LLM call on a long
                # transcript - 30s default is too tight, 180s gives
                # headroom for slow providers without letting it run
                # forever and stall every turn.
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
    """Resolve the summary_brain to a provider, if configured.

    Checks the entry agent's brain.context.summary_brain first,
    then execution.context.summary_brain.
    Returns None if no summary_brain is configured (use main provider).
    """
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
            # Default-via-gateway: same rule as fallback_brain. Derived
            # brains route through the gateway by default so quota +
            # cost accounting cover the auto-compact summarisation,
            # unless the model is local-only or the gateway is disabled.
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
    """Resolve an AgentBrain config to a live LLM provider (no agent required).

    Used by the behavior module to create its classifier provider from
    a standalone brain config.
    """
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
    """Resolve an agent's brain to a live LLM provider instance.

    Since each app gets its own module instances (via ``registry.create()``),
    providers registered via ``on_config_update`` are already per-app isolated.
    """
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
    """Check if a provider supports native tool calling via API.

    Falls back to True (optimistic) if capabilities can't be read.
    """
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
    """Build a ContextWindowConfig from compiled context config."""
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
    """Refine context config with provider-reported capabilities.

    Only uses the provider's max_context_window if the YAML didn't
    explicitly set max_tokens (yaml_max_tokens == 0 means "not set").
    """
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
    """Real token cost for all tool schemas.

    Resolution order:
    1. ``litellm.token_counter`` with the model's actual tokenizer
       (tiktoken for OpenAI, Anthropic offline tokenizer, etc.).
    2. Crude ``len // 4`` last resort when litellm fails.

    The injection-mode decision below (direct / compact_direct /
    discovery) is sized against this number - using a fake estimate
    ships the wrong mode for the model and either wastes context
    (under-estimate → direct mode, schemas blow the budget) or over-
    triggers discovery (over-estimate → unnecessary meta-tool round
    trips for small toolsets).
    """
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
    """Async wrapper around :func:`_choose_tool_injection`.

    The internal ``_estimate_tools_tokens`` calls litellm which loads
    the model's tokenizer (multi-MB on first hit, cached afterwards).
    Off-loaded so the bootstrap path doesn't stall the loop.
    """
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
    """Choose between direct, compact_direct, and discovery mode.

    Uses the actual serialized size of ``direct_tools`` when available
    for an accurate token estimate.  Falls back to a conservative
    per-tool average otherwise.

    Returns:
        ``"direct"`` - all tool schemas fit comfortably (≤50% of budget).
        ``"compact_direct"`` - tools listed by name+one-liner, no full schemas.
            The LLM calls tools directly but with less parameter detail.
        ``"discovery"`` - tools discovered via meta-tools (large toolsets).
    """
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
    """Build meta-tools schema by introspecting context_builder's @action registry.

    This is the single source of truth: the context_builder module defines
    its actions via ``@action`` decorators with Pydantic params models.
    We introspect the registry to generate OpenAI-compatible tool schemas.

    If the context_builder adds a new action tomorrow, it will automatically
    appear in the agent's tool list - no other file needs to change.
    """
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
    """Build tool schemas from a module's @action registry.

    Used to inject a specific module's actions as direct tools
    (e.g. agent_spawn for coordinators, filesystem for direct_modules).

    If *prefix* is given and *use_short_names* is False, tool names become
    ``prefix__action_name`` (e.g. ``filesystem__read``).

    If *use_short_names* is True, names are resolved via to_short() to get
    Claude Code-style names (e.g. ``Read``, ``Bash``).  This produces much
    better LLM performance because the model recognises these names.
    """
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
    """Build schemas for strategic/primitive tools.

    These are context_builder actions that guide execution strategy
    (parallelism, background tasks, watchers, notifications,
    skills). They are always injected as direct tools so the agent can
    reason about HOW to execute before searching for domain tools.

    Scheduling lives in the dedicated cron_native module and is not
    gated here.

    Each group is conditionally included based on YAML config flags.
    """
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
    """Truncate project memory to *max_chars*, breaking at a line boundary."""
    if len(content) <= max_chars:
        return content
    # Cut at the last newline before the limit so we don't split mid-sentence.
    cut = content[:max_chars].rfind("\n")
    if cut <= 0:
        cut = max_chars
    return content[:cut].rstrip() + "\n\n[truncated - full file has {:,} chars]".format(len(content))


def _load_project_memory(compiled: "CompiledApp") -> str | None:
    """Load project memory file from workspace.

    Search order:
    1. .digitorn/apps/{app_id}/.digitorn.md (app-specific)
    2. .digitorn.md in workspace root (global)
    3. CLAUDE.md in workspace root (compatibility)
    4. README.md in workspace root (fallback)
    5. Custom path from execution.project_memory

    Content is capped at ~4 000 chars to avoid bloating the context window.

    Returns the file content or None.
    """
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

        # SECURITY: do NOT auto-fall-back to CLAUDE.md / README.md in the
        # workspace root under `auto`. When the daemon was launched from
        # a developer's repo, that repo's CLAUDE.md (containing internal
        # architecture notes, paths, OAuth credentials, etc.) was being
        # silently injected into EVERY session's system prompt for
        # generic apps like `digitorn-chat`. Cross-user leak.
        # Only `.digitorn.md` stays in the auto path (it's explicitly
        # namespaced for the framework).
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
    """Auto-register and scan the workspace in the index module.

    If the app has both ``index`` and ``filesystem`` modules, and a
    workspace is configured (or defaults to cwd), register it as a
    source and scan it so that ``filesystem.find``/``grep`` can use
    the index for faster lookups.

    This runs once at bootstrap - the watcher keeps the index fresh
    after that.
    """
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
    """Build channel info for prompt injection.

    Extracts the channel names, types, and per-delivery config schemas
    from compiled channels so the LLM knows what output channels are
    available and what per-delivery targeting each one needs.
    """
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
    """Return True if any module action declares require_approval=True."""
    for module in modules.values():
        registry = getattr(module, "_action_registry", {})
        for entry in registry.values():
            if entry.spec and entry.spec.require_approval:
                return True
    return False


async def _auto_describe_databases(
    modules: dict[str, Any],
) -> str:
    """Auto-introspect connected databases and build a schema summary.

    After setup steps connect to databases, this function introspects all
    active connections and builds a compact schema description including:
    - Table names, columns, types, constraints
    - DB-native comments (PostgreSQL COMMENT ON, MySQL column comments)
    - Business annotations (from YAML annotate setup steps)

    Returns a formatted string for injection into the system prompt,
    or empty string if no database module or no connections.
    """
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
    """Probe MCP servers to discover API response structures.

    For each connected MCP server that has writer tools (JSON string
    params), calls getter tools on a sample resource to capture real
    API responses.  The structural templates are stored on the ToolIndex
    for injection into the system prompt.

    This runs once at bootstrap and is best-effort - failures are
    silently ignored (the agent falls back to workflow hints).
    """
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
    """Build a human-readable one-liner for a completed setup step.

    Sensitive fields (passwords, api_key, etc.) are redacted.
    """
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

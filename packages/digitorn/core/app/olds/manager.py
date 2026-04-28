"""AppManager - central orchestrator for the full app lifecycle.

Manages the complete flow: deploy, run, undeploy, reload.

    deploy(yaml_path)  → compile → bootstrap → agent contexts → DB sync → ready
    run(app_id, input)  → route to the deployed app's runtime
    chat(app_id, session_id, message)  → stateful conversation turn
    undeploy(app_id)    → graceful shutdown → remove from store
    reload()            → re-deploy apps from DB at daemon startup

This is the only entry point for app management in the daemon.
The standalone ``digitorn run`` bypasses this for dev convenience.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from digitorn.core.app.compiler import AppYAMLCompiler, CompiledApp
from digitorn.core.app.errors import AppCompilationError
# SessionEventBus replaced by SocketIOBus - injected by the daemon,
# falls back to a local instance when AppManager is used standalone.
from digitorn.core.app.job_store import JobStore
from digitorn.core.app.channels import ChannelRegistry
from digitorn.core.app.channels.llm import LLMNotificationChannel
from digitorn.core.app.channels.gmail import GmailChannel
from digitorn.core.app.channels.log import LogChannel
from digitorn.core.app.channels.webhook import WebhookChannel
from digitorn.core.app.scheduler import SchedulerService
from digitorn.core.app.sessions import ConversationSession, SessionStore
from digitorn.core.app.users import UserStore
from digitorn.core.runtime.types import AgentContext, TurnResult

if TYPE_CHECKING:
    from digitorn.core.app.bootstrapper import AppBootstrapper, BootstrapResult
    from digitorn.core.app.runtime import AppRuntimeStore
    from digitorn.modules.context_builder.module import ContextBuilderModule
    from digitorn.modules.registry import ModuleRegistry
    from digitorn.modules.service_bus import ServiceBus

logger = logging.getLogger(__name__)


# ── Multi-tenant scoping helpers ──────────────────────────────────
# An app install is uniquely identified by (app_id, scope, owner_user_id):
#   - scope="system", owner=""     → install visible to everyone
#   - scope="user",   owner="<uid>"→ Alice's private install
#
# These helpers normalise the two tuple-ish values that callers pass
# around (user_id + scope) into canonical pair, and derive a single
# "slug" used as the BundleStore/disk key so user and system bundles
# never overwrite each other.
def _normalize_scope(
    user_id: str | None = None,
    scope: str | None = None,
) -> tuple[str, str]:
    """Return (scope, owner_user_id) from the caller's args.

    Rules:
      - Explicit ``scope="user"`` requires a non-empty user_id.
      - Explicit ``scope="system"`` always wins (admin path) - owner
        is coerced to "".
      - When ``scope`` is None: user_id present → ("user", user_id);
        user_id absent → ("system", "").
    """
    if scope == "user":
        if not user_id:
            raise ValueError("scope='user' requires a user_id")
        return "user", user_id
    if scope == "system":
        return "system", ""
    if user_id:
        return "user", user_id
    return "system", ""


def _scoped_slug(app_id: str, scope: str, owner_user_id: str) -> str:
    """Disk/bundle key for a scoped install.

    System scope returns the bare app_id so legacy deployments
    (~/.digitorn/apps/{app_id}/) keep their existing location unchanged.
    User scope prefixes with ``_@<uid>__`` - a pattern that is invalid
    as a real app_id (app_ids are [a-z0-9_-]) so there can never be a
    collision with a genuine system app.
    """
    if scope == "user" and owner_user_id:
        safe_owner = owner_user_id.replace("/", "_").replace("\\", "_")
        return f"_@{safe_owner}__{app_id}"
    return app_id


def _resolve_tool_display(
    deployed: Any, name: str, params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the ``display`` dict for a tool_call / tool_start SSE event.

    Looks up the action's ``ActionSpec`` via the deployed app's
    module registry when possible, then delegates to
    ``build_display`` which handles the full resolution cascade
    (ActionSpec → legacy labels → regex fallbacks → defaults).
    Never raises - a failure returns the final-defaults display.
    """
    try:
        from digitorn.core.runtime.tool_display import build_display
        from digitorn.core.runtime.tool_names import to_fqn

        spec = None
        if deployed is not None and getattr(deployed, "modules", None):
            inner_name = (
                (params or {}).get("name", name)
                if name == "execute_tool" else name
            )
            try:
                fqn = to_fqn(inner_name)
            except Exception:
                fqn = inner_name
            if "." in fqn:
                module_id, action = fqn.split(".", 1)
                module = deployed.modules.get(module_id)
                if module is not None:
                    getter = getattr(module, "_get_action_spec", None)
                    if callable(getter):
                        try:
                            spec = getter(action)
                        except Exception:
                            spec = None
        return build_display(name, params or {}, action_spec=spec)
    except Exception as exc:
        logger.debug("tool_display resolve failed for %s: %s", name, exc)
        return {
            "verb": name or "Tool",
            "detail": "",
            "icon": "tool",
            "channel": "chat",
            "hidden": False,
            "category": "action",
            "group": "",
        }


@dataclass
class DeployedApp:
    """A fully deployed, ready-to-execute app in the daemon."""

    app_id: str
    compiled: CompiledApp
    contexts: dict[str, AgentContext]
    modules: dict[str, Any]
    context_builder: Any
    bootstrap_result: Any
    hook_runner: Any = None
    approval_queue: Any = None
    deployed_at: float = field(default_factory=time.time)
    sandbox_worker: Any = None  # SandboxWorker (standard level)
    sandbox_pool: Any = None  # WorkerPool (strict/maximum level)
    hot_reloader: Any = None  # BundleHotReloader (dev mode)
    preview_manager: Any = None  # PreviewManager (dev server supervisor)
    # Scoping - "system" deploys are visible to every user,
    # "user" deploys only to their owner.
    scope: str = "system"
    owner_user_id: str | None = None

    @property
    def mode(self) -> str:
        return self.compiled.execution.mode

    @property
    def entry_context(self) -> AgentContext:
        # Ghost-app guard: a DeployedApp with no contexts is a broken
        # installation that slipped past bootstrap. The old code did
        # `next(iter({}))` here, which raised StopIteration mid-request
        # and silently dropped POSTs. Explicit RuntimeError is caught
        # by the API layer, which can return 503 Degraded.
        if not self.contexts:
            raise RuntimeError(
                f"App '{self.app_id}' is registered but has no executable "
                f"agents (ghost state). Re-deploy or check the server "
                f"logs for the original bootstrap failure."
            )
        agent_id = self.compiled.execution.entry_agent
        if agent_id not in self.contexts:
            agent_id = next(iter(self.contexts))
        return self.contexts[agent_id]

    @property
    def index(self) -> Any:
        if self.context_builder is None:
            return None
        return self.context_builder.index

    def summary(self) -> dict[str, Any]:
        meta = self.compiled.meta
        data: dict[str, Any] = {
            "app_id": self.app_id,
            "name": meta.name,
            "version": meta.version,
            "description": meta.description,
            "mode": self.mode,
            "agents": list(self.contexts.keys()),
            "modules": self.compiled.module_ids,
            "total_tools": self.index.total_tools if self.index else 0,
            "total_categories": self.index.total_categories if self.index else 0,
            "deployed_at": self.deployed_at,
            "greeting": getattr(self.compiled.execution, "greeting", None),
            "workspace_mode": getattr(self.compiled.execution, "workspace_mode", "auto"),
            # Visual metadata for client UI
            "icon": getattr(meta, "icon", ""),
            "color": getattr(meta, "color", ""),
            "category": getattr(meta, "category", "general"),
            "author": getattr(meta, "author", ""),
            "tags": getattr(meta, "tags", []),
            "quick_prompts": getattr(meta, "quick_prompts", []),
            "builtin": getattr(self, "builtin", False),
        }

        # ── Client manifest extensions ────────────────────────
        # The Flutter / web client reads these three blocks to tailor
        # the UI (hide panels, override theme, surface /commands).
        # They're always present in the response (empty by default) so
        # the client can rely on a stable shape.
        # features / theme can live top-level on the YAML OR nested
        # under `app:` - the compiler merges both locations, top-level
        # wins on conflict.
        top_features = dict(getattr(self.compiled, "features", {}) or {})
        nested_features = dict(getattr(meta, "features", {}) or {})
        merged_features = {**nested_features, **top_features}
        data["features"] = merged_features

        top_theme = dict(getattr(self.compiled, "theme", {}) or {})
        nested_theme = dict(getattr(meta, "theme", {}) or {})
        merged_theme = {**nested_theme, **top_theme}
        data["theme"] = merged_theme

        data["slash_commands"] = list(
            getattr(self.compiled, "slash_commands", []) or []
        )
        # Background-mode metadata the Flutter dashboard needs to know
        # *before* it opens an app: which trigger types are wired, which
        # session mode applies, and the optional declarative payload
        # schema (so the form can be built without a second round-trip).
        execution = self.compiled.execution
        trigger_types: list[str] = []
        try:
            for t in getattr(execution, "triggers", []) or []:
                ttype = getattr(t, "type", None) or getattr(t, "trigger_type", None)
                if ttype:
                    trigger_types.append(str(ttype))
        except Exception:
            pass
        # Channels module providers count as triggers too. Without this,
        # a telegram-only app would look "trigger-less" in the listing.
        try:
            channels_mod = self.modules.get("channels") if hasattr(self, "modules") else None
            providers = getattr(channels_mod, "_providers", None) or {}
            for prov in providers.values():
                ttype = getattr(getattr(prov, "config", None), "type", None) or getattr(prov, "type", None)
                if ttype and str(ttype) not in trigger_types:
                    trigger_types.append(str(ttype))
        except Exception:
            pass

        # Workspace block - the client uses this to know the app has
        # a virtual file workspace and which renderer to use.
        ws_block = getattr(self.compiled, "workspace", None)
        if ws_block is not None:
            data["workspace"] = {
                "render_mode": getattr(ws_block, "render_mode", "auto"),
                "entry_file": getattr(ws_block, "entry_file", None),
                "title": getattr(ws_block, "title", None),
            }

        data["trigger_types"] = trigger_types
        data["session_mode"] = getattr(execution, "session_mode", "mono")
        data["max_sessions_per_user"] = getattr(execution, "max_sessions_per_user", 10)
        data["payload_schema"] = getattr(execution, "payload_schema", None)
        mcp_module = self.modules.get("mcp")
        if mcp_module is not None:
            pool = getattr(mcp_module, "_pool", None)
            if pool is not None:
                servers = []
                for sid, entry in pool._servers.items():
                    needs_auth = entry.auth_config is not None and not entry.tools
                    servers.append({
                        "server_id": sid,
                        "status": entry.status,
                        "tools_count": len(entry.tools),
                        "needs_auth": needs_auth,
                        "provider": getattr(entry.auth_config, "provider", None),
                    })
                data["mcp_servers"] = servers
        return data


def _recover_interrupted_session(messages: list[dict[str, Any]]) -> int:
    """Recover an interrupted session by handling orphaned tool_calls.

    When a session crashes mid-turn, the last assistant message may have
    tool_calls with no corresponding tool results. Instead of deleting
    those tool_calls (losing info), we inject synthetic "interrupted"
    results for each. The LLM sees these and can re-execute the tools.

    Also handles orphaned sub-agent spawn calls by injecting a result
    telling the LLM to re-spawn them.

    Returns the number of recovered tool_calls.
    """
    if not messages:
        return 0

    # Find the last assistant message with tool_calls
    _assistant_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant" and messages[i].get("tool_calls"):
            _assistant_idx = i
            break

    if _assistant_idx < 0:
        # No orphaned tool_calls - just inject a resume note
        messages.append({
            "role": "system",
            "content": (
                "[Session resumed after interruption. "
                "Continue working on the task from where you left off. "
                "Do NOT restart or repeat completed work.]"
            ),
        })
        return 0

    # Collect tool_call IDs that have results vs those that don't
    tool_calls = messages[_assistant_idx].get("tool_calls", [])
    expected_ids = set()
    for tc in tool_calls:
        tc_id = tc.get("id", "")
        if tc_id:
            expected_ids.add(tc_id)

    # Check which tool results exist after the assistant message
    found_ids = set()
    for m in messages[_assistant_idx + 1:]:
        if m.get("role") == "tool" and m.get("tool_call_id"):
            found_ids.add(m["tool_call_id"])

    orphaned_ids = expected_ids - found_ids
    if not orphaned_ids:
        # All tool results present - just add resume note
        messages.append({
            "role": "system",
            "content": (
                "[Session resumed after interruption. "
                "All previous tool calls completed. "
                "Continue working on the task.]"
            ),
        })
        return 0

    # Inject synthetic "interrupted" results for orphaned tool_calls
    recovered = 0
    for tc in tool_calls:
        tc_id = tc.get("id", "")
        if tc_id not in orphaned_ids:
            continue
        fn = tc.get("function", {})
        tool_name = fn.get("name", "unknown")

        # Generic interrupted result - works for any tool type
        result_content = (
            f'{{"success": false, "error": "Session interrupted before this tool completed. '
            f'Re-execute this tool if the result is still needed.", '
            f'"interrupted": true, "tool": "{tool_name}"}}'
        )
        messages.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": result_content,
        })
        recovered += 1
        logger.info("Recovered interrupted tool_call: %s (id=%s)", tool_name, tc_id[:12])

    # System note AFTER the synthetic results
    messages.append({
        "role": "system",
        "content": (
            f"[Session resumed after interruption. "
            f"{recovered} tool call(s) were interrupted and returned errors above. "
            f"Re-execute any that are still needed to continue the task. "
            f"Do NOT apologize or restart - continue from where you left off.]"
        ),
    })
    return recovered


@dataclass
class TurnState:
    """Per-session in-flight turn state - the single source of truth the
    state envelope reports to the client.

    The client's UI (animated send button, progress bar, queue chip) is
    derived exclusively from this dataclass at snapshot time. An event
    stream merely carries deltas that the server applies to this
    dataclass, and the client mirrors them. If the client is ever
    unsure (reconnect, session switch, missed event), it pulls the
    envelope and rebuilds its UI from scratch.

    Lifecycle: created at ``_chat_locked`` start, updated on each
    provider / tool event, removed on ``message_done`` /
    ``message_cancelled`` / ``error`` terminal events. Also removed
    (and flagged ``interrupted=True``) by the stale-turn watchdog
    when ``last_activity_at`` lags by > 5 minutes.
    """

    correlation_id: str
    started_at: float                 # unix seconds (time.time())
    last_activity_at: float           # bumped on every token / tool_call
    phase: str = "requesting"         # requesting|generating|thinking|tool_use|waiting|paused
    tool_calls_count: int = 0
    tokens_out: int = 0
    tokens_in: int = 0
    interrupted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": not self.interrupted,
            "correlation_id": self.correlation_id,
            "started_at": self.started_at,
            "last_activity_at": self.last_activity_at,
            "phase": self.phase,
            "tool_calls_count": self.tool_calls_count,
            "tokens_out": self.tokens_out,
            "tokens_in": self.tokens_in,
            "interrupted": self.interrupted,
            # Derived convenience for the client - duration the turn
            # has been running, ms since last observable activity. The
            # client can compute these too from ``server_time`` but
            # pre-computing avoids clock-skew confusion.
            "duration_ms": int((time.time() - self.started_at) * 1000),
            "idle_ms": int((time.time() - self.last_activity_at) * 1000),
        }


class AppManager:
    """Central manager for the full app lifecycle in the daemon.

    Usage::

        manager = AppManager(registry, service_bus, runtime_store)

        deployed = await manager.deploy(Path("my-app.yaml"))

        result = await manager.run_one_shot("my-app", "Hello!")

        apps = manager.list_apps()

        await manager.undeploy("my-app")
    """

    def __init__(
        self,
        registry: ModuleRegistry,
        service_bus: ServiceBus | None = None,
        runtime_store: AppRuntimeStore | None = None,
        *,
        stop_on_error: bool = False,
        session_dir: str | Path | None = None,
        session_backend_url: str | None = None,
        event_bus: Any | None = None,
    ) -> None:
        self._registry = registry
        self._service_bus = service_bus
        self._runtime_store = runtime_store
        self._stop_on_error = stop_on_error
        self._compiler = AppYAMLCompiler(registry)
        from digitorn.core.app.bundle_store import BundleStore
        self._bundle_store = BundleStore()
        self._deployed: dict[str, DeployedApp] = {}
        self._deploy_lock = asyncio.Lock()
        self._deploy_errors: dict[str, dict[str, Any]] = {}
        self._bg_start_tasks: set[asyncio.Task] = set()
        self._session_store = SessionStore(
            directory=session_dir,
            backend_url=session_backend_url,
        )
        recovered = self._session_store.recover_orphans()
        if recovered:
            logger.info("recovered_orphan_sessions count=%d", recovered)
        self._job_store = JobStore(backend=self._session_store._backend)
        # Quota store - SQL-backed, durable, unique source of truth.
        # Falls back to the legacy KV-backed store when:
        #   * ``init_db`` hasn't run yet (unit tests, early bootstrap);
        #   * the Postgres sync driver (``psycopg`` / ``psycopg2``) isn't
        #     installed on a Postgres deployment - the KV store keeps
        #     enforcement running until the admin adds the dependency.
        # Emit at WARNING level (not INFO) so the store kind is visible
        # regardless of the daemon's log level config - operators need
        # to spot the fallback immediately if the SQL backend didn't
        # load.
        self._quota_store = None
        try:
            from digitorn.core.database import _engine as _sql_engine
            if _sql_engine is not None:
                from digitorn.core.quota_sql import SqlQuotaStore
                self._quota_store = SqlQuotaStore(_sql_engine)
                import sys as _qs_sys
                print(
                    f"[QUOTA_STORE] kind=SQL class={type(self._quota_store).__name__} (source of truth)",
                    file=_qs_sys.stderr, flush=True,
                )
                logger.warning(
                    "quota_store_initialised kind=SQL class=%s (source of truth)",
                    type(self._quota_store).__name__,
                )
        except ImportError as exc:
            logger.warning(
                "quota_store: SQL backend unavailable (%s) - "
                "falling back to KV store. Install the sync DB driver "
                "to activate SQL persistence.", exc,
            )
        except Exception as exc:
            logger.warning(
                "quota_store: SQL init failed (%s) - falling back to KV",
                exc, exc_info=True,
            )
        if self._quota_store is None:
            try:
                from digitorn.core.quota import QuotaStore
                self._quota_store = QuotaStore(self._session_store._backend)
                import sys as _qs_sys
                print(
                    f"[QUOTA_STORE] kind=KV-FALLBACK class={type(self._quota_store).__name__} "
                    f"(NOT persistent across restart)",
                    file=_qs_sys.stderr, flush=True,
                )
                logger.warning(
                    "quota_store_initialised kind=KV-FALLBACK class=%s "
                    "(NOT persistent across daemon restart)",
                    type(self._quota_store).__name__,
                )
            except Exception as exc:
                logger.error("quota_store init failed: %s", exc, exc_info=True)
                self._quota_store = None
        self._channel_registry = ChannelRegistry()
        self._channel_registry.register_type(LLMNotificationChannel)
        self._channel_registry.register_type(WebhookChannel)
        self._channel_registry.register_type(LogChannel)
        self._channel_registry.register_type(GmailChannel)
        self._channel_registry.discover_plugins()
        self._llm_channel = LLMNotificationChannel(job_store=self._job_store)
        self._channel_registry.register_instance(
            "llm_notification", self._llm_channel,
        )
        self._scheduler = SchedulerService(self._job_store, self._channel_registry)
        if event_bus is None:
            from digitorn.core.events.event_buffer import EventBuffer
            from digitorn.core.events.session_bus import SocketIOBus
            event_bus = SocketIOBus(sio=None, buffer=EventBuffer())
        self.event_bus = event_bus
        self._notif_poller_task: asyncio.Task | None = None
        self._active_sessions: set[str] = set()  # "app_id:session_id" keys with turn in progress
        self._session_tasks: dict[str, asyncio.Task] = {}  # "app_id:session_id" → running agent_turn task

        # Per-session in-flight turn state - populated by _chat_locked,
        # consumed by build_state_envelope() / the /state endpoint / the
        # state:snapshot SSE event. Keyed by "app_id:session_id".
        # See :class:`TurnState` above for the contract.
        self._turn_state: dict[str, TurnState] = {}
        self._turn_state_lock = asyncio.Lock()
        # Heartbeat task per active turn - cancelled on message_done.
        self._turn_heartbeat_tasks: dict[str, asyncio.Task] = {}
        self._user_store = UserStore()
        from digitorn.core.app.secrets import SecretStore
        self._secret_store = SecretStore()


    async def deploy(
        self,
        yaml_path: Path,
        *,
        force: bool = False,
        inline_secrets: dict[str, str] | None = None,
        scope: str = "system",
        owner_user_id: str | None = None,
    ) -> DeployedApp:
        """Deploy an app from a YAML file.

        Full lifecycle: compile → bootstrap (setup steps) → build agent
        contexts → sync to DB → register in runtime store.

        Args:
            yaml_path: Path to the app YAML file.
            force: Re-deploy even if already deployed.

        Returns:
            DeployedApp ready for execution.

        Raises:
            AppCompilationError: If YAML validation fails.
            RuntimeError: If bootstrap or agent context building fails.
        """
        import yaml as _yaml

        raw = _yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        peek_app_id = (raw.get("app") or {}).get("app_id", "")
        legacy_secrets: dict[str, str] = {}
        if peek_app_id:
            try:
                legacy_secrets = await self._secret_store.get_all(peek_app_id)
            except Exception as exc:
                logger.warning("Secret store read failed for '%s': %s", peek_app_id, exc, exc_info=True)
        if inline_secrets:
            legacy_secrets.update(inline_secrets)

        # Merge legacy per-app secrets with the new CredentialStore
        # (system_wide + per_app_shared scopes visible at compile time).
        # Per-user scopes are resolved at runtime, not here - the
        # compile has no user context.
        try:
            from digitorn.core.credentials.compile_resolver import (
                build_compile_secrets,
            )
            credential_store = getattr(self, "_credential_store", None)
            db_secrets = await build_compile_secrets(
                credential_store,
                app_id=peek_app_id,
                legacy_secrets=legacy_secrets,
            )
        except Exception as exc:
            logger.warning(
                "CredentialStore resolver failed for '%s': %s - "
                "falling back to legacy secrets only",
                peek_app_id, exc,
            )
            db_secrets = legacy_secrets

        compiled = self._compiler.compile_file(
            yaml_path, secrets=db_secrets or None,
        )
        app_id = compiled.app_id

        async with self._deploy_lock:
            deployed_key = self._deployed_key(
                app_id, scope=scope, owner_user_id=owner_user_id,
            )
            if deployed_key in self._deployed and not force:
                raise RuntimeError(
                    f"App '{app_id}' already deployed at "
                    f"scope={scope!r}. Use force=True to redeploy."
                )

            previous = self._deployed.get(deployed_key)
            logger.info(
                "Deploying app '%s' from %s (scope=%s owner=%s)",
                app_id, yaml_path, scope, owner_user_id,
            )

            if previous is None:
                return await self._build_and_deploy(
                    compiled, scope=scope, owner_user_id=owner_user_id,
                )

            # BUG-081 (force=True redeploy): build the NEW DeployedApp
            # BEFORE tearing down the old one. If build fails, rollback
            # to the previous deploy atomically so a user with
            # ``force: true`` can't nuke a system-scope builtin
            # (``digitorn-chat``, …) by POSTing a YAML that fails to
            # compile. Without this, every user of that builtin got
            # 404 on their next message.
            self._deployed.pop(deployed_key, None)
            try:
                new_deployed = await self._build_and_deploy(
                    compiled, scope=scope, owner_user_id=owner_user_id,
                )
            except Exception:
                # Rollback: the build failed, put the old app back
                # verbatim. Users of the previous deploy see no
                # interruption.
                self._deployed[deployed_key] = previous
                logger.warning(
                    "Deploy of '%s' FAILED - rolled back to previous "
                    "deploy to keep existing users online.", app_id,
                )
                raise

            # Build succeeded - retire the previous deploy cleanly now
            # that the replacement is in place. Module-level shutdown
            # only; the heavier session/circuit-breaker teardown stays
            # on the ``undeploy()`` path because it's destructive to
            # conversation state - a silent redeploy keeps users
            # online rather than nuking their sessions.
            for _mid, _mod in list(getattr(previous, "modules", {}).items()):
                try:
                    await _mod.on_stop()
                except Exception as exc:
                    logger.debug(
                        "previous_deploy_module_on_stop_failed %s.%s: %s",
                        app_id, _mid, exc,
                    )
            if getattr(previous, "context_builder", None) is not None:
                try:
                    await previous.context_builder.on_stop()
                except Exception:
                    logger.debug(
                        "previous_deploy_cb_on_stop_failed", exc_info=True,
                    )
            return new_deployed


    async def run_one_shot(
        self,
        app_id: str,
        user_input: str,
        *,
        user_id: str | None = None,
        on_tool_call: Any | None = None,
    ) -> TurnResult:
        """Run a deployed app in one-shot mode.

        Args:
            app_id: The deployed app's ID.
            user_input: User input text.
            user_id: Caller - used to resolve user-scoped deploys.
            on_tool_call: Optional callback for tool call display.

        Returns:
            TurnResult with the agent's response.
        """
        deployed = self._get_deployed(app_id, user_id=user_id)


        if deployed.mode != "one_shot":
            raise RuntimeError(
                f"App '{app_id}' is in '{deployed.mode}' mode, not 'one_shot'"
            )

        from digitorn.core.runtime.app import RuntimeApp as RuntimeAppExecutor

        executor = RuntimeAppExecutor(
            app_id=app_id,
            execution=deployed.compiled.execution,
            contexts=deployed.contexts,
            modules=deployed.modules,
            context_builder=deployed.context_builder,
            hook_runner=deployed.hook_runner,
        )

        try:
            return await executor.run_one_shot(user_input, on_tool_call=on_tool_call)
        finally:
            pass

    async def get_conversation_executor(
        self,
        app_id: str,
        *,
        user_id: str | None = None,
    ) -> Any:
        """Get a RuntimeApp executor for conversation mode.

        Returns a RuntimeApp that the caller can use to run conversation/background.
        The app stays deployed after the conversation ends.
        """
        deployed = self._get_deployed(app_id, user_id=user_id)

        from digitorn.core.runtime.app import RuntimeApp as RuntimeAppExecutor

        return RuntimeAppExecutor(
            app_id=app_id,
            execution=deployed.compiled.execution,
            contexts=deployed.contexts,
            modules=deployed.modules,
            context_builder=deployed.context_builder,
            hook_runner=deployed.hook_runner,
        )


    def _register_wake_handler(self, app_id: str) -> None:
        async def _wake(session_id: str, message: str) -> None:
            existing = await asyncio.to_thread(self._session_store.get, app_id, session_id)
            if existing is None:
                logger.info(
                    "wake_skipped session_not_found app=%s session=%s",
                    app_id, session_id,
                )
                return
            try:
                await self.chat(
                    app_id, session_id, message,
                    user_id=existing.user_id,
                    reminder=True,
                )
            except Exception as exc:
                logger.warning(
                    "wake_chat_failed app=%s session=%s error=%s",
                    app_id, session_id, exc,
                )
        self._scheduler.register_wake_handler(app_id, _wake)

    async def chat(
        self,
        app_id: str,
        session_id: str,
        message: str,
        *,
        user_id: str | None = None,
        workspace: str | None = None,
        on_tool_call: Any | None = None,
        on_tool_start: Any | None = None,
        on_thinking: Any | None = None,
        on_thinking_started: Any | None = None,
        on_thinking_delta: Any | None = None,
        on_hook_event: Any | None = None,
        on_token: Any | None = None,
        on_stream_done: Any | None = None,
        on_status: Any | None = None,
        on_out_token: Any | None = None,
        on_in_token: Any | None = None,
        image_refs: list[dict[str, Any]] | None = None,
        reminder: bool = False,
        correlation_id: str | None = None,
        client_message_id: str | None = None,
    ) -> TurnResult:
        """Process a single conversation message within a session.

        Creates the session on first call. Maintains message history
        across calls with the same session_id.

        Events are also published to the session event bus for any
        persistent SSE subscribers (SDK clients).

        Args:
            app_id: The deployed app's ID.
            session_id: Unique session identifier (client-generated).
            message: User message text.
            on_tool_call: Optional callback for tool call display.
            on_tool_start: Optional callback before tool execution.
            on_thinking: Optional callback for agent reasoning text.

        Returns:
            TurnResult with the agent's response.
        """
        deployed = self._get_deployed(app_id, user_id=user_id)

        ws_mode = getattr(deployed.compiled.execution, "workspace_mode", "auto")
        yaml_ws = getattr(deployed.compiled.execution, "workspace", "")

        # Try to reuse persisted workspace from the session (set on first call)
        uid = user_id or "local"
        _existing_session = await asyncio.to_thread(self._session_store.get, app_id, session_id, user_id=uid)
        _persisted_ws = getattr(_existing_session, "workspace", "") if _existing_session else ""

        if ws_mode == "none":
            ws = ""
        elif ws_mode == "fixed":
            ws = str(Path(yaml_ws).resolve()) if yaml_ws else str(Path.cwd())
        elif ws_mode == "required":
            # Use client workspace, or persisted, or fail
            ws = workspace or _persisted_ws
            if not ws:
                raise RuntimeError("This app requires a workspace. Set one before chatting.")
            ws = str(Path(ws).resolve())
        else:
            # ``workspace_mode: auto`` - default to a per-session isolated
            # dir under ``~/.digitorn/workspaces/<app_id>/<sid>/`` when the
            # caller provides nothing. Falling back to ``Path.cwd()`` used
            # to silently dump every agent write into whatever directory
            # the daemon was launched from (usually the repo root), and
            # aliased every session to the same dir so the per-session
            # preview snapshot was never persisted. The isolated default
            # matches the layout that ``WorkspaceModule._resolve_sync_dir``
            # and ``hydrate_files_from_disk`` expect.
            per_session_default = str(
                Path.home() / ".digitorn" / "workspaces" / app_id / session_id
            )
            # Reject a ``_persisted_ws`` that equals the daemon's current
            # working directory - that's the stale value baked in by the
            # pre-fix code path for any session created before the
            # per-session default was introduced. Without this guard the
            # agent keeps seeing the daemon's cwd (typically the repo
            # root) as its workspace for the rest of the session's life.
            daemon_cwd = str(Path.cwd().resolve())
            if _persisted_ws:
                try:
                    if str(Path(_persisted_ws).resolve()) == daemon_cwd:
                        _persisted_ws = ""
                except Exception:
                    pass
            if workspace or _persisted_ws or yaml_ws:
                ws = workspace or _persisted_ws or yaml_ws
            else:
                ws = per_session_default
            ws = str(Path(ws).resolve()) if ws else ""

        if deployed.mode not in ("conversation", "one_shot"):
            raise RuntimeError(
                f"App '{app_id}' is in '{deployed.mode}' mode, "
                f"not compatible with chat"
            )

        from digitorn.core.workspace import WorkspaceLayout
        layout = WorkspaceLayout(ws, app_id)
        layout.ensure_session_dirs(session_id)

        fs_mod = deployed.modules.get("filesystem")
        if fs_mod and hasattr(fs_mod, "_checkpoint_dir"):
            fs_mod._checkpoint_dir = str(layout.session_checkpoints_dir(session_id))

        # Serialize concurrent access to the same session.
        # CRITICAL: the lock MUST be held during _chat_locked execution AND
        # all session persistence (put, save_messages, append_events) which
        # happens INSIDE _chat_locked. The lock is only released after
        # _chat_locked fully returns - never split persistence across the lock.
        session_lock = self._session_store.session_lock(app_id, session_id, uid)
        active_key = f"{app_id}:{session_id}"
        self._active_sessions.add(active_key)
        lock_acquired = False
        try:
            try:
                # Timeout matches the agent_turn hard limit by default
                # (300s). Configurable via ``session.lock_timeout``.
                try:
                    from digitorn.core.config import get_settings
                    _lock_timeout = get_settings().session.lock_timeout
                except Exception:
                    _lock_timeout = 300.0
                await asyncio.wait_for(
                    session_lock.acquire(), timeout=_lock_timeout,
                )
                lock_acquired = True
            except asyncio.TimeoutError:
                raise RuntimeError(f"Session lock timeout for {app_id}/{session_id}")
            # All session state mutations happen inside _chat_locked under
            # the acquired lock. No work after this call should touch the
            # session store for the same session_id.
            result = await self._chat_locked(
                deployed, app_id, session_id, uid, message, ws,
                on_tool_call, on_tool_start, on_thinking,
                on_hook_event, on_token,
                on_out_token, on_in_token,
                on_thinking_started=on_thinking_started,
                image_refs=image_refs,
                on_thinking_delta=on_thinking_delta,
                on_stream_done=on_stream_done,
                on_status=on_status,
                reminder=reminder,
                correlation_id=correlation_id,
                client_message_id=client_message_id,
            )
            return result
        finally:
            # Each cleanup wrapped - finally must never raise
            try:
                if lock_acquired:
                    session_lock.release()
            except Exception:
                logger.warning("session_lock_release_failed app=%s session=%s", app_id, session_id, exc_info=True)
            try:
                self._active_sessions.discard(active_key)
            except Exception:
                logger.debug("active_sessions_discard_failed", exc_info=True)
            # TurnState cleanup - cancels the heartbeat task and frees
            # the entry so the next ``/state`` query correctly reports
            # ``turn: null``. Wrapped - cleanup must never raise.
            try:
                self.turn_state_end(app_id, session_id)
            except Exception:
                logger.debug("turn_state_end_failed", exc_info=True)

    async def _chat_locked(
        self,
        deployed: Any,
        app_id: str,
        session_id: str,
        uid: str,
        message: str,
        workspace: str,
        on_tool_call: Any,
        on_tool_start: Any,
        on_thinking: Any,
        on_hook_event: Any,
        on_token: Any,
        on_out_token: Any | None = None,
        on_in_token: Any | None = None,
        on_thinking_started: Any | None = None,
        on_thinking_delta: Any | None = None,
        on_stream_done: Any | None = None,
        on_status: Any | None = None,
        image_refs: list[dict[str, Any]] | None = None,
        reminder: bool = False,
        correlation_id: str | None = None,
        client_message_id: str | None = None,
    ) -> "TurnResult":
        """Inner chat logic, called under per-session lock."""
        from digitorn.core.runtime.agent_loop import agent_turn

        from digitorn.core.runtime.types import WORKSPACE_PLACEHOLDER
        yaml_ws = getattr(deployed.compiled.execution, "workspace", "") or ""
        effective_prompt = deployed.entry_context.system_prompt or ""
       
        if effective_prompt:
            resolved_ws = workspace or yaml_ws or ""
            if yaml_ws and workspace and yaml_ws != workspace:
                effective_prompt = effective_prompt.replace(yaml_ws, workspace)
            effective_prompt = effective_prompt.replace(WORKSPACE_PLACEHOLDER, resolved_ws)

        session = await asyncio.to_thread(self._session_store.get, app_id, session_id, user_id=uid)
        if session is None:
            persisted_messages = await asyncio.to_thread(self._session_store.load_messages, app_id, session_id, user_id=uid)
            session = ConversationSession(
                session_id=session_id,
                app_id=app_id,
                user_id=uid,
                workspace=workspace,
            )
            if persisted_messages:
                session.messages = persisted_messages
                logger.info(
                    "Resumed session '%s' for app '%s' user '%s' (%d messages)",
                    session_id, app_id, uid, len(persisted_messages),
                )
            else:
                session.add_system(effective_prompt)
                logger.info(
                    "New session '%s' for app '%s' user '%s' workspace='%s'",
                    session_id, app_id, uid, workspace,
                )

        # Update workspace on the session if it was missing (e.g. old sessions)
        if workspace and not session.workspace:
            session.workspace = workspace

        # Defensive: strip {WORKSPACE} from the system message even for resumed
        # sessions that were persisted before substitution was applied at creation.
        from digitorn.core.runtime.types import apply_workspace_to_messages
        apply_workspace_to_messages(session.messages, workspace, yaml_ws)

        # ── Event log: capture everything for session reconstruction ──────
        _turn_index = session.turn_count
        _event_log: list[dict[str, Any]] = []
        _out_token_total = [0]
        _in_token_total = [0]

        try:
            from digitorn.core.config import get_settings
            _MAX_EVENTS_PER_TURN = get_settings().session.max_events_per_turn
        except Exception:
            _MAX_EVENTS_PER_TURN = 50000  # Safety cap - prevent OOM on runaway turns

        def _log_event(event_type: str, data: dict[str, Any]) -> None:
            if len(_event_log) >= _MAX_EVENTS_PER_TURN:
                return  # Silently drop - turn is already too large
            _event_log.append({
                "type": event_type,
                "ts": time.time(),
                "turn": _turn_index,
                "data": data,
            })

        # Capture ALL bus events (preview:*, widget:*, etc.) into the event log
        # so the client can fully replay the turn. This handler is removed at
        # turn end to avoid leaking between turns.
        async def _bus_capture(captured_user_id: str, envelope: dict[str, Any]) -> None:
            try:
                env_sid = envelope.get("session_id")
                if env_sid and env_sid != session_id:
                    return  # Ignore events from other sessions
                ev_type = envelope.get("type") or "unknown"
                # Skip types already logged explicitly (avoid duplicates)
                if ev_type in ("tool_start", "tool_call", "thinking", "status",
                               "stream_done", "hook", "token_count",
                               "turn_start", "turn_end", "stream_text",
                               "thinking_filtered"):
                    return
                _log_event(ev_type, envelope.get("payload") or {})
            except Exception:
                pass

        try:
            self.event_bus.add_handler(_bus_capture)
        except Exception:
            logger.debug("bus_capture_handler_add_failed", exc_info=True)

        if session.memory_snapshot:
            _mem = deployed.entry_context.memory_module
            if _mem and hasattr(_mem, 'store') and _mem.store:
                _mem.store.restore_from_dict(session.memory_snapshot)
                logger.info(
                    "Memory restored for session '%s' (goal=%s, todos=%d, facts=%d)",
                    session_id,
                    bool(_mem.store.working.goal),
                    len(_mem.store.working.todos),
                    len(_mem.store.working.key_facts),
                )

        # Restore preview/workspace file state from previous turn
        if session.preview_snapshot:
            _preview = deployed.entry_context.preview_module
            if _preview is not None:
                try:
                    pstate = _preview._store.get_or_create(session_id)
                    snap = session.preview_snapshot
                    # Restore state map
                    pstate.state = dict(snap.get("state", {}))
                    # Restore all resource channels (files, nodes, edges, etc.)
                    pstate.resources = {
                        name: dict(items)
                        for name, items in snap.get("resources", {}).items()
                    }
                    pstate._seq = snap.get("seq", 0)
                    logger.info(
                        "Preview restored for session '%s' (%d channels, %d files)",
                        session_id,
                        len(pstate.resources),
                        len(pstate.resources.get("files", {})),
                    )
                except Exception as exc:
                    logger.warning("Preview restore failed for '%s': %s", session_id, exc)

        # ── Smart resume: if session was interrupted, recover interrupted work ──
        if session.interrupted and session.messages:
            session.interrupted = False  # Clear flag
            _recovered = _recover_interrupted_session(session.messages)
            logger.info(
                "Session '%s' resumed after interruption (%d tool calls recovered)",
                session_id, _recovered,
            )

        # Build user message - multimodal if images provided
        if image_refs:
            from digitorn.core.runtime.multimodal import build_user_message_with_images
            user_msg = build_user_message_with_images(message, image_refs)
            session.messages.append(user_msg)
            if not session.title and message:
                session.title = message[:80]
        elif reminder:
            session.messages.append({
                "role": "system",
                "content": (
                    f"[REMINDER from cron] You scheduled this earlier and it "
                    f"just fired. Take whatever action you committed to. "
                    f"Message: {message}"
                ),
            })
            session.last_active = time.time()
        else:
            session.add_user(message)
        _log_event("turn_start", {"message": message, "images": len(image_refs or [])})

        await asyncio.to_thread(self._session_store.put, session)

        from digitorn.core.runtime.types import apply_workspace_override

        import copy
        ctx = copy.copy(deployed.entry_context)
        ctx.session_id = session_id
        ctx.user_id = uid
        # Tag the context with the app_id too - without this,
        # `_get_session_metrics(ctx)` falls back to app_id="default" and
        # SessionMetrics accumulate in the wrong bucket. Downstream
        # consumers (usage_events record, list_sessions join, cost
        # calculation) then see 0 because they look up by the real
        # app_id. We set it unconditionally; the entry_context copy
        # may or may not already have it set upstream.
        ctx.app_id = app_id
        apply_workspace_override(ctx, workspace, yaml_ws)

        if deployed.sandbox_pool is not None:
            # Per-session sandbox: acquire a worker from the pool
            try:
                pool_worker = await deployed.sandbox_pool.acquire(workspace, session_id)
                ctx.sandbox_worker = pool_worker
            except Exception as exc:
                logger.error("sandbox_pool_acquire_failed app=%s session=%s: %s", app_id, session_id, exc)
                # Fall through without sandbox - better than crashing
        elif deployed.sandbox_worker is not None:
            deployed.sandbox_worker.update_workspace(workspace)
            ctx.sandbox_worker = deployed.sandbox_worker

        cb = deployed.context_builder
        if cb is not None:
            cb._agent_context = ctx

        bus_key = self.event_bus.session_key(app_id, session_id, uid)

        # Wire event bus to agent context so emergency compaction can emit events
        ctx._event_bus = self.event_bus
        ctx._bus_key = bus_key

        # Register the TurnState so the /state endpoint + state:snapshot
        # SSE event can report "a turn is running now" the instant the
        # client asks, without having to wait for the first token event.
        # The correlation_id is authoritative - comes from the POST path.
        _turn_corr_id = correlation_id or ""
        if _turn_corr_id:
            self.turn_state_begin(app_id, session_id, _turn_corr_id)
            # Start the heartbeat pulser - announces liveness every few
            # seconds so a client watchdog can distinguish "still thinking"
            # from "server stuck".
            self._start_turn_heartbeat(app_id, session_id, uid, _turn_corr_id)

        _save_counter = 0

        async def _on_tool_call(name: str, params: dict, result: Any, call_id: str = "") -> None:
            # Defense-in-depth: the agent loop occasionally emitted
            # tool_call events with an empty name when the provider's
            # streaming chunk fragmented the name mid-flight and the
            # fragment was flushed before the fqn arrived. Clients saw
            # "?" bubbles. Recover the name from params / result_data
            # where possible; last resort is "unknown" - and we log a
            # stack trace of the source so we can hunt the root cause
            # rather than silently masking it.
            if not name:
                recovered = (params or {}).get("name") or ""
                if not recovered and isinstance(result, dict):
                    recovered = (
                        result.get("name", "")
                        or result.get("tool", "")
                        or ""
                    )
                if not recovered:
                    recovered = "unknown"
                import traceback as _tb
                logger.warning(
                    "tool_call_empty_name recovered=%r call_id=%r "
                    "params_keys=%s result_type=%s "
                    "stack=%s",
                    recovered, call_id,
                    list((params or {}).keys())[:5],
                    type(result).__name__,
                    "".join(_tb.format_stack()[-4:-1]).replace("\n", " | "),
                )
                name = recovered
            nonlocal _save_counter
            # ── Standardize result: ALWAYS extract success + error + data ──
            # Every tool_call event sent to the client has the same shape:
            #   success: bool (always present)
            #   error: str (always present, empty if no error)
            #   result: dict (always present, tool-specific data)
            ok, err = True, ""
            result_data: Any = None
            if isinstance(result, dict):
                # Explicit success=false means failure
                if result.get("success") is False:
                    ok = False
                # Explicit error field means failure
                if result.get("error") and result.get("error") != "":
                    ok = False
                    err = str(result.get("error", ""))
                result_data = result
            elif hasattr(result, "success"):
                ok = result.success
                err = getattr(result, "error", "") or ""
                if hasattr(result, "data") and isinstance(result.data, dict):
                    result_data = result.data

            from digitorn.core.cli.ui import _tool_label
            label, detail = _tool_label(name, params)

            display = _resolve_tool_display(deployed, name, params)

            event_data: dict[str, Any] = {
                "id": call_id,
                "name": name, "params": params,
                "success": ok, "error": err,
                "label": label, "detail": detail,
                "display": display,
                "result": result_data,
            }

            # Include unified diff for edit-type tools (clients display it inline)
            if isinstance(result_data, dict) and "diff" in result_data:
                event_data["diff"] = result_data["diff"][:4000]

            # Include previous_content from metadata for frontend diff view.
            # metadata is NOT sent to the LLM - only to SSE clients.
            _meta = getattr(result, "metadata", None)
            if not _meta and isinstance(result, dict):
                _meta = result.get("metadata")
            if isinstance(_meta, dict):
                if "previous_content" in _meta:
                    event_data["previous_content"] = _meta["previous_content"]
                if "new_content" in _meta:
                    event_data["new_content"] = _meta["new_content"]
                if "image_data" in _meta:
                    event_data["image_data"] = _meta["image_data"]
                    event_data["image_mime"] = _meta.get("media_type", "image/png")

            from digitorn.core.events.envelope import (
                SessionEvent, OpType, OpState, gen_op_id,
            )
            # Same op_id as the preceding ``tool_start`` - the client
            # uses it to correlate running → completed on the same
            # chip. Falls back to a generated id only if the provider
            # gave us nothing (defensive).
            op_id = call_id or gen_op_id("tool")
            op_state = OpState.FAILED if not ok else OpState.COMPLETED
            event_data["op_id"] = op_id
            event_data["correlation_id"] = correlation_id or None
            await self.event_bus.emit(SessionEvent.build(
                type="tool_call",
                app_id=app_id,
                session_id=session_id,
                user_id=uid,
                op_id=op_id,
                op_type=OpType.TOOL,
                op_state=op_state,
                correlation_id=correlation_id or "",
                payload=event_data,
            ))

            # Derived events - mirror what /chat/stream builds from tool_call
            # Resolve short names (Agent → agent_spawn.spawn_agent) to get the action part
            from digitorn.core.runtime.tool_names import to_fqn
            inner_name = params.get("name", name) if name == "execute_tool" else name
            resolved = to_fqn(inner_name)
            action = resolved.split(".")[-1] if "." in resolved else inner_name
            logger.debug(
                "derived_event_check name=%r action=%r result_data_type=%s",
                name, action, type(result_data).__name__ if result_data else "None",
            )

            _MEMORY_ACTIONS = {"set_goal", "remember", "task_create", "task_update"}
            _SHELL_ACTIONS = {"bash"}
            _AGENT_ACTIONS = {"agent"}

            if action in _MEMORY_ACTIONS:
                from digitorn.core.events.envelope import (
                    SessionEvent as _SE, OpType as _OT, OpState as _OS,
                )
                # memory_update is a side effect of the tool call that
                # just completed - reuse the tool's op_id as parent
                # so the client can show it under the same chip.
                await self.event_bus.emit(_SE.build(
                    type="memory_update",
                    app_id=app_id, session_id=session_id, user_id=uid,
                    op_id=(call_id or op_id or f"memory-{name}"),
                    op_type=_OT.TOOL, op_state=_OS.COMPLETED,
                    op_parent_id=(call_id or op_id) if call_id else None,
                    correlation_id=correlation_id or "",
                    payload={
                        "action": action, "result": result_data, "name": name,
                        "op_parent_id": call_id or op_id,
                    },
                ))
            elif action in _SHELL_ACTIONS:
                # Extract stdout/stderr - try every known result structure
                _stdout, _stderr = "", ""
                for src in (result_data, getattr(result, "data", None), result):
                    if isinstance(src, dict) and ("stdout" in src or "stderr" in src):
                        _stdout = src.get("stdout", "")
                        _stderr = src.get("stderr", "")
                        break
                    if isinstance(src, dict) and "data" in src and isinstance(src["data"], dict):
                        _stdout = src["data"].get("stdout", "")
                        _stderr = src["data"].get("stderr", "")
                        break
                if _stdout or _stderr:
                    from digitorn.core.events.envelope import (
                        SessionEvent as _SE, OpType as _OT, OpState as _OS,
                    )
                    # terminal_output belongs to the shell tool call;
                    # share its op_id so the client attaches the
                    # stdout/stderr panel to the right chip.
                    await self.event_bus.emit(_SE.build(
                        type="terminal_output",
                        app_id=app_id, session_id=session_id, user_id=uid,
                        op_id=(call_id or op_id or f"shell-{name}"),
                        op_type=_OT.TOOL, op_state=_OS.COMPLETED,
                        op_parent_id=(call_id or op_id) if call_id else None,
                        correlation_id=correlation_id or "",
                        payload={
                            "stdout": _stdout[:2000], "stderr": _stderr[:500],
                            "op_parent_id": call_id or op_id,
                        },
                    ))
            elif action in _AGENT_ACTIONS:
                # Build structured agent_event from tool result
                _agent_data: dict[str, Any] = {"action": action, "name": name}
                if isinstance(result_data, dict):
                    # For wait/wait_all: forward results + completed_agents
                    if "results" in result_data:
                        _agent_data["action"] = "agent_wait_all"
                        _agent_data["completed_agents"] = [
                            {"agent_id": r.get("agent_id", ""), "status": r.get("status", "")}
                            for r in result_data.get("results", [])
                        ]
                    # For spawn: forward agent_id, specialist, task
                    if "agent_id" in result_data:
                        _agent_data["agent_id"] = result_data["agent_id"]
                    if "specialist" in result_data:
                        _agent_data["specialist"] = result_data["specialist"]
                    if "task" in result_data:
                        _agent_data["task"] = str(result_data["task"])[:200]
                    if "status" in result_data:
                        _agent_data["status"] = result_data["status"]
                    _agent_data["result"] = result_data
                from digitorn.core.events.envelope import (
                    SessionEvent, OpType, OpState, gen_op_id,
                )
                agent_id_here = (
                    _agent_data.get("agent_id")
                    or gen_op_id("agent")
                )
                # This path reflects the TOOL result of ``Agent(...)``
                # (dispatch/status/wait). The underlying sub-agent
                # cycle's spawn/progress/result events are emitted
                # separately by the ``_relay`` notify path above -
                # here we only carry the current dispatch snapshot so
                # clients don't need to pick between two sources.
                _status = _agent_data.get("status", "")
                _op_state = {
                    "spawned": OpState.RUNNING,
                    "running": OpState.RUNNING,
                    "completed": OpState.COMPLETED,
                    "failed": OpState.FAILED,
                    "cancelled": OpState.CANCELLED,
                    "timeout": OpState.TIMEOUT,
                }.get(_status, OpState.RUNNING)
                _agent_data["op_id"] = agent_id_here
                await self.event_bus.emit(SessionEvent.build(
                    type="agent_event",
                    app_id=app_id,
                    session_id=session_id,
                    user_id=uid,
                    op_id=agent_id_here,
                    op_type=OpType.AGENT,
                    op_state=_op_state,
                    correlation_id=correlation_id or "",
                    payload=_agent_data,
                ))

            if on_tool_call is not None:
                await on_tool_call(name, params, result, call_id)

            # Log to persistent event log
            _log_event("tool_call", {
                "name": name, "label": label, "detail": detail,
                "params": params, "success": ok, "error": err,
            })

            # Persist after EVERY tool call - zero data loss on crash/disconnect.
            # A client reconnecting with ?since=N gets everything.
            # Wrapped in to_thread() because the KV backend (DiskCache/SQLite)
            # uses synchronous I/O that would block the event loop.
            try:
                _store = self._session_store
                _msgs = session.messages
                _uid = session.user_id
                _elog = _event_log
                await asyncio.to_thread(
                    _store.save_messages, app_id, session_id, _msgs, user_id=_uid,
                )
                # Save ONLY this turn's events using a turn-scoped key, so
                # previous turns are not overwritten. The full history is
                # reconstructed by load_session_events() which aggregates
                # all turn event logs.
                await asyncio.to_thread(
                    _store.save_turn_events, app_id, session_id, _turn_index, _elog, user_id=_uid,
                )
            except Exception:
                logger.warning("Failed to persist messages for %s/%s", app_id, session_id, exc_info=True)

        async def _on_tool_start_bus(name: str, params: dict, call_id: str = "") -> None:
            from digitorn.core.cli.ui import _tool_label
            from digitorn.core.events.envelope import (
                SessionEvent, OpType, OpState, gen_op_id,
            )
            # TurnState: advance phase + bump liveness. The state
            # envelope picks this up instantly so /state sees
            # phase='tool_use' + updated tool_calls_count, and the
            # client reflects "agent is calling tool X" without
            # having to parse the event stream manually.
            self.turn_state_update(
                app_id, session_id,
                phase="tool_use", tool_calls_delta=1,
            )
            label, detail = _tool_label(name, params)
            display = _resolve_tool_display(deployed, name, params)
            # The op_id is the provider-assigned call_id (Anthropic
            # ``tool_use.id`` / OpenAI ``tool_call.id``). Falling back
            # to a fresh ``op-tool-<hex>`` preserves the contract when
            # the provider streams a call without an id (rare, seen
            # on partial chunks).
            op_id = call_id or gen_op_id("tool")
            await self.event_bus.emit(SessionEvent.build(
                type="tool_start",
                app_id=app_id,
                session_id=session_id,
                user_id=uid,
                op_id=op_id,
                op_type=OpType.TOOL,
                op_state=OpState.RUNNING,
                correlation_id=correlation_id or "",
                op_parent_id=None,
                payload={
                    "id": op_id,          # legacy alias - old clients
                    "call_id": call_id,   # legacy alias
                    "name": name,
                    "params": params,
                    "label": label,
                    "detail": detail,
                    "display": display,
                    "correlation_id": correlation_id or None,
                },
            ))
            _log_event("tool_start", {"name": name, "label": label, "detail": detail, "params": params})
            if on_tool_start is not None:
                await on_tool_start(name, params, call_id)

        async def _on_thinking_bus(text: str) -> None:
            if not text or not text.strip():
                return
            stripped = text.strip()
            # Filter short narrations that just describe tool calls -
            # the ToolCallGroup already shows this info.
            lines = stripped.split("\n")
            if len(lines) <= 2 and len(stripped) < 80:
                _log_event("thinking_filtered", {"text": stripped})
                return
            # Turn-scoped helper - every event of THIS turn shares
            # op_id = correlation_id (the turn's id), op_type = TURN.
            # Centralised so the 7 emitters below don't repeat the
            # boilerplate (and can't forget a field).
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS,
            )
            _turn_op_id = correlation_id or f"turn-{session_id}"

            def _turn_event(ev_type: str, state: _OS, payload: dict) -> _SE:
                return _SE.build(
                    type=ev_type,
                    app_id=app_id, session_id=session_id, user_id=uid,
                    op_id=_turn_op_id, op_type=_OT.TURN, op_state=state,
                    correlation_id=correlation_id or "",
                    payload=payload,
                )

            await self.event_bus.emit(_turn_event(
                "thinking", _OS.RUNNING, {"text": stripped},
            ))
            _log_event("thinking", {"text": stripped})
            if on_thinking is not None:
                await on_thinking(stripped)

        async def _on_thinking_started_bus() -> None:
            self.turn_state_update(app_id, session_id, phase="thinking")
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS,
            )
            _turn_op_id = correlation_id or f"turn-{session_id}"
            await self.event_bus.emit(_SE.build(
                type="thinking_started", app_id=app_id, session_id=session_id,
                user_id=uid, op_id=_turn_op_id, op_type=_OT.TURN,
                op_state=_OS.RUNNING, correlation_id=correlation_id or "",
            ))
            if on_thinking_started is not None:
                await on_thinking_started()

        async def _on_thinking_delta_bus(delta: str) -> None:
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS,
            )
            _turn_op_id = correlation_id or f"turn-{session_id}"
            await self.event_bus.emit(_SE.build(
                type="thinking_delta", app_id=app_id, session_id=session_id,
                user_id=uid, op_id=_turn_op_id, op_type=_OT.TURN,
                op_state=_OS.RUNNING, correlation_id=correlation_id or "",
                payload={"delta": delta},
            ))
            if on_thinking_delta is not None:
                await on_thinking_delta(delta)

        _stream_chunks: list[str] = []

        def _emit_turn_bg(ev_type: str, state, payload: dict) -> None:
            """Fire-and-forget turn-scoped emission from sync callbacks.
            Same contract as the async helpers, but suitable for the
            ``_on_token_bus`` / ``_track_*_token`` sync signatures.
            """
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT,
            )
            _turn_op_id = correlation_id or f"turn-{session_id}"
            try:
                _loop = asyncio.get_running_loop()
                _loop.create_task(self.event_bus.emit(_SE.build(
                    type=ev_type, app_id=app_id, session_id=session_id,
                    user_id=uid, op_id=_turn_op_id, op_type=_OT.TURN,
                    op_state=state, correlation_id=correlation_id or "",
                    payload=payload,
                )))
            except RuntimeError:
                pass

        def _on_token_bus(delta: str) -> None:
            from digitorn.core.events.envelope import OpState as _OS
            _stream_chunks.append(delta)
            # Bump liveness - token arrival is the primary signal that
            # the LLM is actually producing output (not stuck in a
            # provider retry loop).
            self.turn_state_update(
                app_id, session_id, phase="generating",
            )
            _emit_turn_bg("token", _OS.RUNNING, {"delta": delta})
            if on_token is not None:
                if asyncio.iscoroutinefunction(on_token):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(on_token(delta))
                    except RuntimeError:
                        pass
                else:
                    on_token(delta)

        def _track_out_token(count: int) -> None:
            from digitorn.core.events.envelope import OpState as _OS
            _out_token_total[0] += count
            self.turn_state_update(
                app_id, session_id, tokens_out_delta=count,
            )
            _emit_turn_bg("out_token", _OS.RUNNING, {"count": count})
            if on_out_token is not None:
                on_out_token(count)

        def _track_in_token(count: int) -> None:
            from digitorn.core.events.envelope import OpState as _OS
            _in_token_total[0] += count
            self.turn_state_update(
                app_id, session_id, tokens_in_delta=count,
            )
            _emit_turn_bg("in_token", _OS.RUNNING, {"count": count})
            if on_in_token is not None:
                on_in_token(count)

        def _on_status_bus(phase: str, details: dict | None = None) -> None:
            from digitorn.core.events.envelope import OpState as _OS
            _emit_turn_bg(
                "status", _OS.RUNNING, {"phase": phase, **(details or {})},
            )
            _log_event("status", {"phase": phase, **(details or {})})
            if on_status is not None:
                on_status(phase, details)

        def _on_stream_done_bus() -> None:
            from digitorn.core.events.envelope import OpState as _OS
            # stream_done marks the end of LLM streaming, not the end
            # of the turn (message_done is the terminal). Keep RUNNING
            # so a reconnecting client still sees the turn as active
            # until message_done lands.
            _emit_turn_bg("stream_done", _OS.RUNNING, {})
            _log_event("stream_done", {})
            if on_stream_done is not None:
                on_stream_done()

        async def _on_hook_event(hook_event: Any) -> None:
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS, gen_op_id,
            )
            hook_data = {
                "hook_id": hook_event.hook_id,
                "action_type": hook_event.action_type,
                "phase": hook_event.phase,
                "details": hook_event.details,
            }
            # A hook firing is a ONE-SHOT event, not a long-running
            # op. We used to reuse ``hook_event.hook_id`` as op_id
            # with op_state=RUNNING for phases that weren't explicitly
            # completed/failed - that left ``_system`` (the singleton
            # id used by built-in hooks) stuck in ``active_ops`` forever.
            # Fix: every hook event is TERMINAL on emission, and each
            # invocation gets a fresh op_id so two firings of the same
            # hook don't overwrite each other's state in the client
            # registry.
            phase = (hook_event.phase or "").lower()
            if phase in ("failed", "error"):
                op_state = _OS.FAILED
            elif phase in ("cancelled",):
                op_state = _OS.CANCELLED
            else:
                # pre / on / completed / done / success / unknown →
                # COMPLETED. The event is itself the "I happened" -
                # the client renders it as a log entry, not a chip
                # that stays alive.
                op_state = _OS.COMPLETED
            # Fresh op_id per fire. The stable hook_id is preserved in
            # the payload for clients that want to group all firings
            # of the same hook in a debug panel.
            hook_op_id = gen_op_id("hook")
            hook_data["hook_op_id"] = hook_op_id
            await self.event_bus.emit(_SE.build(
                type="hook", app_id=app_id, session_id=session_id,
                user_id=uid, op_id=hook_op_id, op_type=_OT.SYSTEM,
                op_state=op_state, correlation_id=correlation_id or "",
                payload=hook_data,
            ))
            _log_event("hook", hook_data)
            if on_hook_event is not None:
                await on_hook_event(hook_event)

        hook_runner = deployed.hook_runner

        _had_hook_cb = False
        if hook_runner is not None:
            _prev_hook_cb = hook_runner.on_hook_event
            hook_runner.on_hook_event = _on_hook_event
            _had_hook_cb = True

        _turn_error = None
        _aborted = False
        active_key = f"{app_id}:{session_id}"
        # Expose the live session + store to ctx so runtime helpers
        # (e.g. title_generator) can update persisted fields without
        # plumbing a new parameter through the whole call chain.
        try:
            ctx.session = session  # type: ignore[attr-defined]
            ctx.session_store = self._session_store  # type: ignore[attr-defined]
            # Quota enforcement hook. agent_loop reads ``ctx.quota_store``
            # on every turn to check+charge messages / tokens / cost
            # against the admin's rules. The store is shared daemon-wide
            # (one KV backend for definitions + rolling counters).
            _qstore = getattr(self, "_quota_store", None)
            if _qstore is not None:
                ctx.quota_store = _qstore  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            _turn_coro = agent_turn(
                ctx,
                session.messages,
                max_turns=deployed.compiled.execution.max_turns,
                timeout=deployed.compiled.execution.timeout,
                on_tool_call=_on_tool_call,
                on_tool_start=_on_tool_start_bus,
                on_thinking=_on_thinking_bus,
                on_thinking_started=_on_thinking_started_bus,
                on_thinking_delta=_on_thinking_delta_bus,
                hook_runner=hook_runner,
                on_token=_on_token_bus,
                on_stream_done=_on_stream_done_bus,
                on_status=_on_status_bus,
                on_out_token=_track_out_token,
                on_in_token=_track_in_token,
            )
            _task = asyncio.current_task()
            if _task is not None:
                self._session_tasks[active_key] = _task
            result = await _turn_coro
        except asyncio.CancelledError:
            _aborted = True
            result = TurnResult(content="[Interrupted by user]", error="aborted")
        except Exception as exc:
            _turn_error = exc
            result = TurnResult(content="", error=str(exc))
        finally:
            # Each cleanup step is wrapped individually so a failure in one
            # never prevents the others from running. The finally block must
            # NEVER raise - leaks happen when it does.
            try:
                self._session_tasks.pop(active_key, None)
            except Exception:
                logger.debug("session_task_pop_failed", exc_info=True)
            # Persist event log even if turn crashed - partial replay > nothing
            try:
                if _event_log:
                    await asyncio.to_thread(
                        self._session_store.save_turn_events,
                        app_id, session_id, _turn_index, _event_log, user_id=session.user_id,
                    )
            except Exception:
                logger.warning("failed to persist event log on error for %s/%s", app_id, session_id)
            # Remove bus capture handler (safety net for early returns/crashes)
            try:
                self.event_bus.remove_handler(_bus_capture)
            except Exception:
                pass
            try:
                if _had_hook_cb and hook_runner is not None:
                    hook_runner.on_hook_event = _prev_hook_cb
            except Exception:
                logger.debug("hook_callback_restore_failed", exc_info=True)
            # Mark session as interrupted if turn failed or was aborted
            # - enables smart resume (orphaned tool_calls get synthetic results)
            if _aborted or _turn_error or (result and result.error):
                try:
                    session.interrupted = True
                    session.interrupted_at = time.time()
                except Exception:
                    logger.debug("session_interrupt_flag_failed", exc_info=True)
                try:
                    await asyncio.to_thread(self._session_store.put, session)
                except Exception:
                    logger.warning("failed to persist interrupted session %s (put)", session_id)
                try:
                    await asyncio.to_thread(
                        self._session_store.save_messages,
                        app_id, session_id, session.messages, user_id=session.user_id,
                    )
                except Exception:
                    logger.warning("failed to persist interrupted session %s (messages)", session_id)

        # ── Log final events for the turn ──────────────────────────────────
        if _stream_chunks:
            _log_event("stream_text", {"content": "".join(_stream_chunks)})
        if _out_token_total[0] or _in_token_total[0]:
            _log_event("token_count", {
                "out_tokens": _out_token_total[0],
                "in_tokens": _in_token_total[0],
            })
        try:
            _rp = int(getattr(result, "prompt_tokens", 0) or 0)
            _rc = int(getattr(result, "completion_tokens", 0) or 0)
            if (_in_token_total[0], _out_token_total[0]) != (_rp, _rc):
                logger.warning(
                    "token_stream_mismatch app=%s sid=%s "
                    "in_stream=%d vs result.prompt=%d  "
                    "out_stream=%d vs result.completion=%d",
                    app_id, session_id,
                    _in_token_total[0], _rp,
                    _out_token_total[0], _rc,
                )
        except Exception:
            pass
        _log_event("turn_end", {
            "content": result.content,
            "tool_calls_count": result.tool_calls_count,
            "turns_used": result.turns_used,
            "truncated": result.truncated,
            "error": result.error,
        })

        # Remove the bus capture handler - prevents cross-turn leakage
        try:
            self.event_bus.remove_handler(_bus_capture)
        except Exception:
            pass

        if result.content:
            session.add_assistant(result.content)

        _mem = ctx.memory_module
        if _mem and hasattr(_mem, 'store') and _mem.store:
            try:
                session.memory_snapshot = _mem.store.to_dict()
            except Exception:
                pass

        # Persist preview/workspace file state across turns
        _preview = ctx.preview_module
        if _preview is not None:
            try:
                snap = _preview.snapshot_for(session_id)
                if snap and snap.get("resources"):
                    session.preview_snapshot = snap
            except Exception:
                pass

        # ── Persist session, messages, events - crash-safe ──
        # All three operations are in a try block to ensure partial
        # persistence doesn't prevent the result from being returned.
        session.turn_count += 1
        if not _aborted:
            session.interrupted = False  # Successful turn clears interruption flag
        try:
            _store = self._session_store
            _uid = session.user_id
            await asyncio.to_thread(_store.put, session)
            await asyncio.to_thread(_store.save_messages, app_id, session_id, session.messages, user_id=_uid)
            await asyncio.to_thread(_store.append_events, app_id, session_id, _event_log, user_id=_uid)
        except Exception as persist_exc:
            logger.warning("session_persistence_failed: %s", persist_exc)

        # Build rich result event with usage/cost/context for all SSE clients
        result_event_data: dict[str, Any] = {
            "content": result.content,
            "session_id": session_id,
            "tool_calls_count": result.tool_calls_count,
            "turns_used": result.turns_used,
            "truncated": result.truncated,
            "error": result.error,
        }

        # Usage: token counts + cost estimate
        result_event_data["usage"] = {
            "input_tokens": result.prompt_tokens,
            "output_tokens": result.completion_tokens,
        }
        try:
            from digitorn.core.runtime.session_metrics import get_session_metrics
            sm = get_session_metrics(app_id, session_id)
            result_event_data["usage"]["total_input_tokens"] = sm.prompt_tokens
            result_event_data["usage"]["total_output_tokens"] = sm.completion_tokens
            result_event_data["usage"]["total_tokens"] = sm.total_tokens
            result_event_data["turn_number"] = sm.turn
            _model = sm.model or (getattr(ctx.provider, "model", "") if ctx else "")
            _ml = _model.lower()
            if "opus" in _ml:
                _pi, _po = 15.0, 75.0
            elif "sonnet" in _ml:
                _pi, _po = 3.0, 15.0
            else:
                _pi, _po = 0.80, 4.0
            result_event_data["usage"]["cost_usd"] = round(
                sm.prompt_tokens * _pi / 1_000_000 + sm.completion_tokens * _po / 1_000_000, 6
            )
            result_event_data["context"] = sm.context.snapshot()
        except Exception:
            pass

        try:
            _ws = workspace or ""
            if _ws:
                from digitorn.core.api.apps_v2 import _get_workspace_status
                result_event_data["workspace_status"] = await asyncio.to_thread(
                    _get_workspace_status, _ws,
                )
        except Exception:
            pass

        if not _aborted:
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS,
            )
            # ``result`` is the turn's payload event (assistant text,
            # usage, workspace status). Share op_id with the turn,
            # op_state=COMPLETED marks the turn's payload delivery.
            _turn_op_id = correlation_id or f"turn-{session_id}"
            await self.event_bus.emit(_SE.build(
                type="result",
                app_id=app_id, session_id=session_id, user_id=uid,
                op_id=_turn_op_id, op_type=_OT.TURN, op_state=_OS.COMPLETED,
                correlation_id=correlation_id or "",
                payload=result_event_data,
            ))

        # Persist the usage event for token/cost tracking. This is
        # the single authoritative row the Settings → Usage screen
        # reads from via /api/users/me/usage. Failures are logged
        # but never block the turn completion path.
        try:
            _usage_store = getattr(self, "_usage_store", None)
            if _usage_store is not None:
                _provider = (
                    getattr(ctx.provider, "backend", "")
                    if ctx else ""
                ) or getattr(ctx.provider, "provider_id", "") if ctx else ""
                _model = (
                    getattr(ctx.provider, "model", "") if ctx else ""
                ) or ""
                # Best-effort: use session metrics for totals if the
                # raw result doesn't have them (sub-agents).
                _pt = result.prompt_tokens
                _ct = result.completion_tokens
                if (_pt + _ct) == 0:
                    try:
                        from digitorn.core.runtime.session_metrics import (
                            get_session_metrics,
                        )
                        sm_fallback = get_session_metrics(app_id, session_id)
                        _pt = sm_fallback.prompt_tokens or 0
                        _ct = sm_fallback.completion_tokens or 0
                    except Exception:
                        pass
                if (_pt + _ct) > 0:
                    await _usage_store.record(
                        user_id=uid or "local",
                        app_id=app_id,
                        session_id=session_id,
                        provider=_provider or "unknown",
                        model=_model or "unknown",
                        prompt_tokens=_pt,
                        completion_tokens=_ct,
                    )
        except Exception as usage_exc:
            logger.warning("usage_record_failed: %s", usage_exc, exc_info=True)

        # Emit a dedicated error event so clients can display it prominently.
        # The result event also has error, but clients may not check it.
        if result.error and result.error != "aborted":
            from digitorn.core.api.apps_v2 import _classify_error
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS,
            )
            try:
                error_data = _classify_error(
                    _turn_error if _turn_error else RuntimeError(result.error)
                )
                error_data["session_id"] = session_id
                _turn_op_id = correlation_id or f"turn-{session_id}"
                await self.event_bus.emit(_SE.build(
                    type="error",
                    app_id=app_id, session_id=session_id, user_id=uid,
                    op_id=_turn_op_id, op_type=_OT.TURN, op_state=_OS.FAILED,
                    correlation_id=correlation_id or "",
                    payload=error_data,
                ))
            except Exception:
                pass  # Don't crash if error classification fails

        return result

    async def check_notifications(
        self,
        app_id: str,
        session_id: str,
        *,
        user_id: str = "local",
        on_tool_call: Any | None = None,
        on_hook_event: Any | None = None,
    ) -> TurnResult | None:
        """Drain background notifications and run an agent turn if any exist.

        Returns a TurnResult if notifications were found and processed,
        or None if there are no pending notifications.
        """
        from digitorn.core.runtime.agent_loop import agent_turn

        deployed = self._get_deployed(app_id, user_id=user_id)
        cb = deployed.context_builder
        if cb is None or not hasattr(cb, "drain_bg_notifications"):
            return None

        notifications = cb.drain_bg_notifications(session_id=session_id)

        buffered = self._job_store.drain_buffered(app_id)
        if buffered:
            notifications.extend(buffered)

        if not notifications:
            return None

        session = await asyncio.to_thread(self._session_store.get, app_id, session_id)
        if session is None:
            return None

        from digitorn.core.runtime.agent_loop import (
            _format_bg_task_notification,
            _format_watcher_notification,
        )

        for notif in notifications:
            if notif.get("type") == "watcher":
                text = _format_watcher_notification(notif)
            else:
                text = _format_bg_task_notification(notif)

            session.messages.append({"role": "system", "content": text})

        logger.info(
            "Background notification check: %d task(s), triggering agent turn",
            len(notifications),
        )

        bus_key = self.event_bus.session_key(app_id, session_id, user_id)

        for notif in notifications:
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS, gen_op_id,
            )
            # Each background-task notification has a task_id that
            # doubles as its op_id (lifecycle: running → progress
            # events → completed/failed). We always emit RUNNING here
            # because the notification arrives WHILE the task is alive;
            # the terminal state is later emitted by bg_task_update.
            _task_id = notif.get("task_id") or gen_op_id("bg")
            await self.event_bus.emit(_SE.build(
                type="notification",
                app_id=app_id, session_id=session_id, user_id=user_id,
                op_id=_task_id, op_type=_OT.TOOL, op_state=_OS.RUNNING,
                payload=notif,
            ))

        async def _on_tool_call(name: str, params: dict, result_val: Any, call_id: str = "") -> None:
            from digitorn.core.events.envelope import (
                SessionEvent, OpType, OpState, gen_op_id,
            )
            ok, err = True, ""
            if isinstance(result_val, dict):
                ok = result_val.get("success", True)
                err = result_val.get("error", "")
            elif hasattr(result_val, "success"):
                ok = result_val.success
                err = getattr(result_val, "error", "") or ""
            op_id = call_id or gen_op_id("tool")
            await self.event_bus.emit(SessionEvent.build(
                type="tool_call",
                app_id=app_id,
                session_id=session_id,
                user_id=user_id,
                op_id=op_id,
                op_type=OpType.TOOL,
                op_state=OpState.FAILED if not ok else OpState.COMPLETED,
                payload={
                    "id": op_id,
                    "call_id": call_id,
                    "name": name, "params": params,
                    "success": ok, "error": err,
                },
            ))
            if on_tool_call is not None:
                await on_tool_call(name, params, result_val, call_id)

        from digitorn.core.runtime.types import apply_workspace_override

        import copy
        ctx = copy.copy(deployed.entry_context)
        ctx.session_id = session_id
        yaml_ws = getattr(deployed.compiled.execution, "workspace", "")
        ws = yaml_ws or str(Path.cwd())
        apply_workspace_override(ctx, ws, yaml_ws)
        hook_runner = deployed.hook_runner

        _had_hook_cb = False
        if hook_runner is not None and on_hook_event is not None:
            _prev_hook_cb = hook_runner.on_hook_event
            hook_runner.on_hook_event = on_hook_event
            _had_hook_cb = True

        try:
            result = await agent_turn(
                ctx,
                session.messages,
                max_turns=deployed.compiled.execution.max_turns,
                timeout=deployed.compiled.execution.timeout,
                on_tool_call=_on_tool_call,
                hook_runner=hook_runner,
            )
        finally:
            if _had_hook_cb and hook_runner is not None:
                hook_runner.on_hook_event = _prev_hook_cb

        if result.content:
            session.add_assistant(result.content)

        await asyncio.to_thread(self._session_store.put, session)

        from digitorn.core.events.envelope import (
            SessionEvent as _SE, OpType as _OT, OpState as _OS,
        )
        # notification_result closes the background-notification cycle
        # started by the ``notification`` emits above. Use a stable
        # op_id based on the aggregate (count) so the client can
        # correlate per-batch if it wants to.
        await self.event_bus.emit(_SE.build(
            type="notification_result",
            app_id=app_id, session_id=session_id, user_id=user_id,
            op_id=f"notif-batch-{session_id}",
            op_type=_OT.SYSTEM,
            op_state=_OS.FAILED if result.error else _OS.COMPLETED,
            payload={
                "content": result.content,
                "session_id": session_id,
                "notifications_count": len(notifications),
                "tool_calls_count": result.tool_calls_count,
                "error": result.error,
            },
        ))

        return result

    def has_active_bg_tasks(self, app_id: str) -> bool:
        """Check if a deployed app has any active background tasks."""
        deployed = self._deployed.get(app_id)
        if deployed is None:
            return False
        cb = deployed.context_builder
        if cb is None or not hasattr(cb, "has_active_bg_tasks"):
            return False
        return cb.has_active_bg_tasks()

    def _make_approval_publisher(self, app_id: str) -> Any:
        """Build an approval callback that republishes to the session bus.

        Registered on each deployed app's ``ApprovalQueue`` so that when a
        tool execution awaits approval, Flutter clients listening on the
        Socket.IO ``session:{id}`` (or ``user:{id}``) room see the request
        immediately - no per-connection wiring, no polling.
        """
        async def _publish(request: Any) -> None:
            try:
                from digitorn.core.events.envelope import (
                    SessionEvent, OpType, OpState,
                )
                uid = request.user_id or "local"
                sid = getattr(request, "session_id", "") or ""
                payload = request.to_dict()
                # request_id doubles as the op_id - the pair
                # (approval_request, approval_resolved) for one pending
                # approval shares it so the client can close the modal
                # deterministically on resolution.
                op_id = getattr(request, "request_id", "") or "op-approval"
                payload["op_id"] = op_id
                # Progress heartbeats (see ApprovalQueue) send the same
                # request with description patched to "(still waiting…)"
                # - we treat them as op_state=waiting_approval heartbeats
                # carrying the same op_id.
                # Same invariant as approval_resolved below - refuse to
                # emit without a real session_id so the event actually
                # lands in the originating session's history_log.
                if not sid:
                    logger.warning(
                        "approval_request_missing_session_id app=%s op=%s "
                        "- skipping bus emit",
                        app_id, op_id,
                    )
                else:
                    await self.event_bus.emit(SessionEvent.build(
                        type="approval_request",
                        app_id=app_id,
                        session_id=sid,
                        user_id=uid,
                        op_id=op_id,
                        op_type=OpType.APPROVAL,
                        op_state=OpState.WAITING_APPROVAL,
                        payload=payload,
                    ))
            except Exception as exc:
                logger.warning(
                    "approval_publish_failed app=%s: %s", app_id, exc,
                )
        return _publish

    def _approval_resolve_publisher(self, app_id: str):
        """Return a callback that publishes `approval_resolved` on SSE.

        Plugs the gap that left the UI showing a pending approval forever
        after the user resolved it (or the timeout fired): without this
        signal the frontend had no trigger to drop the badge/card.
        """
        async def _publish_resolved(request: Any, approved: bool, reason: str) -> None:
            try:
                from digitorn.core.events.envelope import (
                    SessionEvent, OpType, OpState,
                )
                uid = request.user_id or "local"
                sid = getattr(request, "session_id", "") or ""
                payload = dict(request.to_dict())
                payload["approved"] = bool(approved)
                payload["reason"] = reason
                op_id = getattr(request, "request_id", "") or "op-approval"
                payload["op_id"] = op_id
                # Heartbeat vs real resolution: the ApprovalQueue emits
                # a heartbeat with reason == "pending_heartbeat" every
                # 15 s (see BUG-008 fix). The client should see op_state
                # stay WAITING_APPROVAL in that case, and move to a
                # terminal state only for a real user decision.
                if reason == "pending_heartbeat":
                    op_state = OpState.WAITING_APPROVAL
                    ev_type = "approval_progress"
                elif approved:
                    op_state = OpState.COMPLETED
                    ev_type = "approval_resolved"
                else:
                    # Denied / timeout. ApprovalQueue uses "Approval
                    # timed out" in the reason - promote that to the
                    # TIMEOUT terminal state.
                    reason_l = (reason or "").lower()
                    if "time" in reason_l and "out" in reason_l:
                        op_state = OpState.TIMEOUT
                    else:
                        op_state = OpState.CANCELLED
                    ev_type = "approval_resolved"
                # Strict: refuse to emit a session-scoped event without
                # a real session_id. The old fallback ``"anonymous_session"``
                # would land in history_log under a fake session that no
                # client is ever subscribed to - the approval outcome
                # would vanish from the original session's history. Log
                # loudly and drop the publish; the local callback still
                # fires so the tool call itself gets resolved.
                if not sid:
                    logger.warning(
                        "approval_resolved_missing_session_id app=%s op=%s "
                        "- skipping bus emit (no session to publish to)",
                        app_id, op_id,
                    )
                else:
                    await self.event_bus.emit(SessionEvent.build(
                        type=ev_type,
                        app_id=app_id,
                        session_id=sid,
                        user_id=uid,
                        op_id=op_id,
                        op_type=OpType.APPROVAL,
                        op_state=op_state,
                        payload=payload,
                    ))
            except Exception as exc:
                logger.warning(
                    "approval_resolved_publish_failed app=%s: %s", app_id, exc,
                )
        return _publish_resolved

    async def start_notification_poller(self, interval: float = 1.0) -> None:
        """Start the background-notification drain loop.

        Replaces the per-SSE-connection watcher. Runs a single task that
        ticks at ``interval`` seconds, enumerates deployed apps with
        pending background-task notifications, and calls
        ``check_notifications`` on each affected session. That method
        already publishes ``notification`` / ``tool_call`` /
        ``notification_result`` events to the session bus, so Flutter
        clients see bg-task completions in real time even when the user
        is not actively chatting.
        """
        if getattr(self, "_notif_poller_task", None) is not None:
            return

        async def _loop() -> None:
            logger.info("notification_poller_started interval=%ss", interval)
            while True:
                try:
                    await asyncio.sleep(interval)
                    for deployed_key, deployed in list(self._deployed.items()):
                        app_id = deployed.app_id
                        cb = deployed.context_builder
                        if cb is None or not hasattr(cb, "_bg_notifications"):
                            continue
                        pending: list[tuple[str, str]] = []
                        for sid, q in list(cb._bg_notifications.items()):
                            if sid == "_standalone" or q.empty():
                                continue
                            uid = "local"
                            try:
                                inner = getattr(q, "_queue", None)
                                if inner:
                                    first = inner[0]
                                    if isinstance(first, dict):
                                        uid = first.get("user_id") or "local"
                            except Exception:
                                pass
                            pending.append((sid, uid))
                        for sid, uid in pending:
                            try:
                                await self.check_notifications(
                                    app_id, sid, user_id=uid,
                                )
                            except Exception as exc:
                                logger.warning(
                                    "notification_poller_check_failed "
                                    "app=%s session=%s: %s",
                                    app_id, sid, exc,
                                )
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.warning("notification_poller_tick_error: %s", exc)
            logger.info("notification_poller_stopped")

        self._notif_poller_task = asyncio.create_task(_loop())

    async def stop_notification_poller(self) -> None:
        task = getattr(self, "_notif_poller_task", None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        self._notif_poller_task = None

    async def start_stale_turn_watchdog(
        self,
        interval: float = 30.0,
        staleness_threshold: float = 300.0,  # 5 minutes
    ) -> None:
        """Scan the TurnState store every ``interval`` seconds and mark
        turns with no activity for > ``staleness_threshold`` as
        interrupted. Emits a terminal ``error`` event so clients clear
        their "turn in progress" UI.

        Covers the edge case where an agent turn hangs (LLM never
        returns, subprocess deadlock, unhandled exception swallowed by
        a bad try/except). Without this, the TurnState would live
        forever, the client's send button would stay animated, and the
        user would have no way to recover short of restarting the app.
        """
        if getattr(self, "_stale_turn_watchdog_task", None) is not None:
            logger.warning("stale_turn_watchdog already running")
            return

        async def _loop() -> None:
            logger.info(
                "stale_turn_watchdog_started interval=%ss threshold=%ss",
                interval, staleness_threshold,
            )
            while True:
                try:
                    await asyncio.sleep(interval)
                    now = time.time()
                    stale: list[tuple[str, str, TurnState]] = []
                    for key, state in list(self._turn_state.items()):
                        if state.interrupted:
                            continue
                        if now - state.last_activity_at > staleness_threshold:
                            parts = key.split(":", 1)
                            if len(parts) == 2:
                                stale.append((parts[0], parts[1], state))

                    for app_id, session_id, state in stale:
                        logger.warning(
                            "stale_turn_detected app=%s session=%s "
                            "corr=%s idle=%.1fs",
                            app_id, session_id,
                            state.correlation_id,
                            now - state.last_activity_at,
                        )
                        final = self.turn_state_end(
                            app_id, session_id, interrupted=True,
                        )
                        try:
                            from digitorn.core.events.envelope import (
                                SessionEvent, OpType, OpState,
                            )
                            await self.event_bus.emit(SessionEvent.build(
                                type="error",
                                app_id=app_id,
                                session_id=session_id,
                                user_id=(state.correlation_id and "local") or "local",
                                op_id=state.correlation_id,
                                op_type=OpType.TURN,
                                op_state=OpState.FAILED,
                                correlation_id=state.correlation_id,
                                payload={
                                    "error": "Turn timed out - no activity for >5 min",
                                    "code": "turn_stale",
                                    "correlation_id": state.correlation_id,
                                    "turn": final.to_dict() if final else None,
                                },
                            ))
                        except Exception as exc:
                            logger.debug("stale_turn emit failed: %s", exc)
                except asyncio.CancelledError:
                    logger.info("stale_turn_watchdog_stopped")
                    return
                except Exception as exc:
                    logger.warning("stale_turn_watchdog_tick_error: %s", exc)

        self._stale_turn_watchdog_task = asyncio.create_task(_loop())

    async def stop_stale_turn_watchdog(self) -> None:
        task = getattr(self, "_stale_turn_watchdog_task", None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        self._stale_turn_watchdog_task = None

    async def get_session(self, app_id: str, session_id: str, user_id: str | None = None) -> ConversationSession | None:
        """Get a conversation session - single source of truth = DB.

        Lookup order:

        1. **Hot-path cache** (``SessionStore`` on DiskCache/Redis) -
           returns immediately on hit.
        2. **DB rehydration fallback** - on cache miss (session expired,
           daemon restarted, cross-machine call) we rebuild the
           ``ConversationSession`` from the durable tables
           ``user_sessions`` + ``session_messages``. The DB is the
           authoritative store; the cache is a pure accelerator.
        3. Only return None if the DB has no record either (genuinely
           new / deleted session).

        Rebuilt sessions are re-populated into the cache so the next
        read is hot again. No data loss on idle expiry, no duplicate
        stores, no "closing the client nukes my history".

        SECURITY: user_id is enforced. If user_id is None, falls back
        to "local" (single-user mode). Cross-user session scan is NOT
        allowed at either the cache or the DB layer.
        """
        uid = user_id or "local"
        session = await asyncio.to_thread(
            self._session_store.get, app_id, session_id, user_id=uid,
        )
        if session is not None:
            return session

        # Cache miss → rebuild from the DB (source of truth).
        return await self._rebuild_session_from_db(
            app_id, session_id, user_id=uid,
        )

    async def _rebuild_session_from_db(
        self, app_id: str, session_id: str, user_id: str,
    ) -> ConversationSession | None:
        """Reconstruct a ConversationSession from the durable DB rows.

        Returns None if no row exists (which means: never persisted -
        either a brand-new sid, or a session whose first turn failed
        and was rejected by the commit-on-first-success gate).

        Otherwise rebuilds:
          - messages (from ``history_log`` where kind='message',
            ordered by seq)
          - created_at / last_active_at (from ``user_sessions``)
          - title (if captured by the semantic title generator)

        Then pushes the rebuilt session back into the cache so the
        next call hits hot.
        """
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import HistoryLog, UserSession
        from sqlalchemy import select
        from digitorn.core.app.sessions import ConversationSession

        try:
            factory = get_session_factory()
        except Exception as exc:
            logger.debug("session rebuild: DB not ready: %s", exc)
            return None

        def _row_to_msg(m: HistoryLog) -> dict[str, Any]:
            msg: dict[str, Any] = {"role": m.role or ""}
            # Multimodal messages carry their structured ``raw_content``
            # in payload - prefer it so images / documents replay intact.
            raw = None
            if isinstance(m.payload, dict):
                raw = m.payload.get("raw_content")
            if raw is not None:
                msg["content"] = raw
            elif m.content is not None:
                msg["content"] = m.content
            if m.tool_call_id:
                msg["tool_call_id"] = m.tool_call_id
            if m.tool_calls:
                msg["tool_calls"] = m.tool_calls
            if m.name:
                msg["name"] = m.name
            return msg

        try:
            async with factory() as db:
                row = (
                    await db.execute(
                        select(UserSession).where(
                            UserSession.app_id == app_id,
                            UserSession.session_id == session_id,
                            UserSession.user_id == user_id,
                        )
                    )
                ).scalar_one_or_none()

                if row is None:
                    return None

                # ── Compaction-aware resume ──────────────────────────
                # If this session has any persisted ``compaction``
                # event, we resume from the LATEST one: skip everything
                # before ``kept_range.from_seq``, inject the compacted
                # system note reconstructed from the event's payload,
                # then replay only the kept + post-compaction messages.
                # Fallback to full-history rebuild when no compaction
                # exists (pre-feature sessions, brand-new sessions).
                compaction_row = (
                    await db.execute(
                        select(HistoryLog)
                        .where(HistoryLog.kind == "event")
                        .where(HistoryLog.type == "compaction")
                        .where(HistoryLog.app_id == app_id)
                        .where(HistoryLog.session_id == session_id)
                        .order_by(HistoryLog.seq.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()

                messages: list[dict[str, Any]] = []
                memory_snapshot: dict[str, Any] | None = None

                if compaction_row is not None and isinstance(
                    compaction_row.payload, dict
                ):
                    payload = compaction_row.payload
                    kept_from_seq = int(
                        (payload.get("kept_range") or {}).get("from_seq", 0)
                    )

                    # Preserve the app's ORIGINAL system prompt (seq 0
                    # region) - we still want the agent to see it so
                    # its identity/policies aren't lost after compaction.
                    original_system = (
                        await db.execute(
                            select(HistoryLog)
                            .where(HistoryLog.kind == "message")
                            .where(HistoryLog.app_id == app_id)
                            .where(HistoryLog.session_id == session_id)
                            .where(HistoryLog.role == "system")
                            .where(HistoryLog.seq < kept_from_seq)
                            .order_by(HistoryLog.seq.asc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if original_system is not None:
                        messages.append(_row_to_msg(original_system))

                    # The compacted system note - reconstructed from the
                    # frozen snapshot (summary + tools + memory + …).
                    from digitorn.core.runtime.compaction_persistence import (
                        build_system_note_from_payload,
                    )
                    messages.append(build_system_note_from_payload(payload))

                    # Kept + post-compaction messages
                    kept_rows = (
                        await db.execute(
                            select(HistoryLog)
                            .where(HistoryLog.kind == "message")
                            .where(HistoryLog.app_id == app_id)
                            .where(HistoryLog.session_id == session_id)
                            .where(HistoryLog.seq >= kept_from_seq)
                            .order_by(HistoryLog.seq.asc())
                        )
                    ).scalars().all()
                    messages.extend(_row_to_msg(m) for m in kept_rows)

                    mem = payload.get("memory_snapshot")
                    if isinstance(mem, dict) and mem:
                        memory_snapshot = mem

                    logger.info(
                        "session_rebuild_compacted app=%s session=%s "
                        "kept_from_seq=%d kept_msgs=%d",
                        app_id, session_id, kept_from_seq, len(kept_rows),
                    )
                else:
                    # No compaction on record - full history rebuild
                    # (the original behaviour).
                    msg_rows = (
                        await db.execute(
                            select(HistoryLog)
                            .where(HistoryLog.kind == "message")
                            .where(HistoryLog.app_id == app_id)
                            .where(HistoryLog.session_id == session_id)
                            .order_by(HistoryLog.seq.asc())
                        )
                    ).scalars().all()
                    messages.extend(_row_to_msg(m) for m in msg_rows)

            # Build the hot ConversationSession from DB rows. Title
            # defaults to the first user message's head (80 chars) -
            # matches the semantic title generator's fallback.
            title = ""
            for m in messages:
                if m.get("role") == "user":
                    content = m.get("content") or ""
                    if isinstance(content, list):
                        content = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict)
                        )
                    title = str(content)[:80]
                    break

            session = ConversationSession(
                session_id=session_id,
                app_id=app_id,
                user_id=user_id,
                messages=messages,
                title=title,
                created_at=(
                    row.created_at.timestamp()
                    if row.created_at else time.time()
                ),
                last_active=(
                    row.last_active_at.timestamp()
                    if row.last_active_at else time.time()
                ),
                # Compaction-restored memory takes precedence so the
                # agent resumes with the exact goal/todos/facts snapshot
                # it held at compaction time. Empty when no compaction
                # exists for this session.
                memory_snapshot=memory_snapshot or {},
            )

            # Warm the cache so the next read is hot. Idempotent -
            # race-safe even if multiple concurrent misses fire.
            try:
                await asyncio.to_thread(self._session_store.put, session)
            except Exception as exc:
                logger.debug("session rebuild: cache warmup failed: %s", exc)

            logger.info(
                "session_rebuilt_from_db app=%s session=%s user=%s messages=%d",
                app_id, session_id, user_id, len(messages),
            )
            return session
        except Exception as exc:
            logger.warning(
                "session_rebuild_failed app=%s session=%s: %s",
                app_id, session_id, exc, exc_info=True,
            )
            return None

    async def drain_session_queue(
        self, app_id: str, session_id: str, user_id: str = "local",
    ) -> int:
        """Dispatch queued messages for a session until the queue is
        empty. Called from Socket.IO ``join_session`` after a crash /
        reconnect so pending work resumes without the user having to
        trigger it.

        Returns the number of messages successfully processed.
        """
        from digitorn.core.app import message_queue as _mq
        processed = 0
        while True:
            entry = await _mq.next_queued(session_id)
            if entry is None:
                break

            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS,
            )
            # Publish message_started for the client UI.
            try:
                await self.event_bus.emit(_SE.build(
                    type="message_started",
                    app_id=app_id, session_id=session_id, user_id=user_id,
                    op_id=entry.correlation_id,
                    op_type=_OT.TURN, op_state=_OS.RUNNING,
                    correlation_id=entry.correlation_id,
                    payload={
                        "correlation_id": entry.correlation_id,
                        "session_id": session_id,
                        "position": entry.position,
                        "resumed": True,
                    },
                ))
            except Exception:
                pass

            try:
                await self.chat(
                    app_id, session_id, entry.message,
                    user_id=user_id,
                    image_refs=entry.image_refs or None,
                    correlation_id=entry.correlation_id,
                )
            except Exception as exc:
                logger.warning(
                    "drain_session_queue: chat failed app=%s sid=%s: %s",
                    app_id, session_id, exc,
                )
                try:
                    await _mq.mark_failed(
                        entry.id, error_code="internal",
                    )
                    _mq.fail_awaiter(entry.correlation_id, exc)
                    await self.event_bus.emit(_SE.build(
                        type="error",
                        app_id=app_id, session_id=session_id, user_id=user_id,
                        op_id=entry.correlation_id,
                        op_type=_OT.TURN, op_state=_OS.FAILED,
                        correlation_id=entry.correlation_id,
                        payload={
                            "error": str(exc)[:500],
                            "code": "internal",
                            "correlation_id": entry.correlation_id,
                        },
                    ))
                except Exception:
                    pass
                continue

            # Success - mark done + publish.
            try:
                await _mq.mark_done(entry.id)
                _mq.resolve_awaiter(
                    entry.correlation_id, {"status": "completed"},
                )
                await self.event_bus.emit(_SE.build(
                    type="message_done",
                    app_id=app_id, session_id=session_id, user_id=user_id,
                    op_id=entry.correlation_id,
                    op_type=_OT.TURN, op_state=_OS.COMPLETED,
                    correlation_id=entry.correlation_id,
                    payload={
                        "correlation_id": entry.correlation_id,
                        "session_id": session_id,
                    },
                ))
            except Exception:
                pass
            processed += 1
        if processed:
            logger.info(
                "drain_session_queue finished app=%s sid=%s processed=%d",
                app_id, session_id, processed,
            )
        return processed

    async def end_session(self, app_id: str, session_id: str, user_id: str = "local") -> bool:
        """End and remove a conversation session."""
        # Fire the `session_end` hook before the store delete - lets
        # apps persist final state (export snapshot, flush logs) while
        # the session is still readable.
        try:
            deployed = self.get(app_id, user_id=user_id)
            cb = getattr(deployed, "context_builder", None) if deployed else None
            hook_runner = getattr(cb, "hook_runner", None) if cb else None
            if hook_runner is not None:
                from digitorn.core.runtime.hooks import TurnState
                state = TurnState(
                    messages=[],
                    turn=0, max_turns=0, tool_calls_count=0,
                    agent_id="",
                )
                state._session_id = session_id  # type: ignore[attr-defined]
                await hook_runner.run("session_end", state)
        except Exception as exc:
            logger.debug("session_end hook failed: %s", exc)

        # Clean up session-scoped runtime resources (fire-and-forget)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.cleanup_session(app_id, session_id))
        except RuntimeError:
            pass  # No event loop - standalone CLI, resources will be cleaned on undeploy
        return await asyncio.to_thread(self._session_store.delete, app_id, session_id, user_id=user_id)

    def is_session_active(self, app_id: str, session_id: str) -> bool:
        """In-memory check: a turn is currently held by this process."""
        return f"{app_id}:{session_id}" in self._active_sessions

    async def is_turn_running(self, app_id: str, session_id: str) -> bool:
        """Authoritative check combining in-memory turns (fast-path) AND queue DB rows."""
        if self.is_session_active(app_id, session_id):
            return True
        try:
            from digitorn.core.app import message_queue as _mq
            return await _mq.has_running(session_id)
        except Exception:
            return False

    # ── TurnState store - source of truth for client UI sync ───────────
    #
    # The following helpers manipulate ``self._turn_state`` which backs
    # ``build_state_envelope`` and drives the client's animated send
    # button / progress bar / queue chip. The contract is simple: every
    # mutation happens while we hold the session lock (already true for
    # all ``_chat_locked`` call sites); readers only get a snapshot copy
    # so they never see a half-built turn mid-mutation.

    def _turn_key(self, app_id: str, session_id: str) -> str:
        return f"{app_id}:{session_id}"

    def turn_state_begin(
        self, app_id: str, session_id: str, correlation_id: str,
    ) -> TurnState:
        """Create the TurnState for a new turn. Returns the fresh state.

        Idempotent: if a TurnState already exists for this session (e.g.
        a resumed turn after reconnect), it's overwritten - the new
        correlation_id is authoritative.
        """
        now = time.time()
        state = TurnState(
            correlation_id=correlation_id,
            started_at=now,
            last_activity_at=now,
            phase="requesting",
        )
        self._turn_state[self._turn_key(app_id, session_id)] = state
        return state

    def turn_state_update(
        self,
        app_id: str,
        session_id: str,
        *,
        phase: str | None = None,
        tool_calls_delta: int = 0,
        tokens_out_delta: int = 0,
        tokens_in_delta: int = 0,
    ) -> TurnState | None:
        """Mutate the live TurnState. Silently no-ops when no turn is
        active (e.g. a late event arriving after ``message_done``)."""
        state = self._turn_state.get(self._turn_key(app_id, session_id))
        if state is None:
            return None
        state.last_activity_at = time.time()
        if phase is not None:
            state.phase = phase
        if tool_calls_delta:
            state.tool_calls_count += tool_calls_delta
        if tokens_out_delta:
            state.tokens_out += tokens_out_delta
        if tokens_in_delta:
            state.tokens_in += tokens_in_delta
        return state

    def turn_state_end(
        self, app_id: str, session_id: str, *, interrupted: bool = False,
    ) -> TurnState | None:
        """Remove the TurnState on terminal event.

        Returns the final state snapshot for the caller to log / emit
        if useful. ``interrupted=True`` is set by the watchdog or an
        abort; a clean ``message_done`` leaves it False.
        """
        key = self._turn_key(app_id, session_id)
        state = self._turn_state.pop(key, None)
        if state is None:
            return None
        if interrupted:
            state.interrupted = True
        # Cancel the heartbeat pulser if one is registered.
        hb = self._turn_heartbeat_tasks.pop(key, None)
        if hb is not None and not hb.done():
            hb.cancel()
        return state

    def turn_state_get(
        self, app_id: str, session_id: str,
    ) -> TurnState | None:
        """Return a live reference (NOT a copy) to the TurnState.

        Callers must not mutate the returned object - use the
        ``turn_state_update`` helper. For a safe external view use
        ``turn_state_snapshot`` which returns the dict form.
        """
        return self._turn_state.get(self._turn_key(app_id, session_id))

    def turn_state_snapshot(
        self, app_id: str, session_id: str,
    ) -> dict[str, Any] | None:
        state = self.turn_state_get(app_id, session_id)
        return state.to_dict() if state else None

    def _start_turn_heartbeat(
        self, app_id: str, session_id: str, user_id: str,
        correlation_id: str,
    ) -> None:
        """Spawn a background task emitting ``turn:heartbeat`` every 3s
        until the turn ends. Lets a client watchdog distinguish "still
        generating" from "server stuck" - without a heartbeat a 90s
        tool call looks identical to a hung turn.

        The heartbeat event carries the current TurnState snapshot so
        even a client that missed every intermediate delta can resync
        immediately from the pulse.
        """
        key = self._turn_key(app_id, session_id)
        # Cancel any stale heartbeat from a previous turn on the same
        # session - shouldn't happen since turn_state_end cancels too,
        # but cheap belt-and-braces.
        old = self._turn_heartbeat_tasks.pop(key, None)
        if old is not None and not old.done():
            old.cancel()

        async def _pulse() -> None:
            from digitorn.core.events.envelope import (
                SessionEvent, OpType, OpState,
            )
            try:
                while True:
                    await asyncio.sleep(3.0)
                    state = self.turn_state_get(app_id, session_id)
                    if state is None:
                        return  # turn ended; nothing to report
                    try:
                        await self.event_bus.emit(SessionEvent.build(
                            type="turn:heartbeat",
                            app_id=app_id,
                            session_id=session_id,
                            user_id=user_id,
                            op_id=correlation_id,
                            op_type=OpType.TURN,
                            op_state=OpState.RUNNING,
                            correlation_id=correlation_id,
                            payload={"turn": state.to_dict()},
                        ))
                    except Exception as exc:
                        logger.debug(
                            "turn_heartbeat_emit_failed session=%s: %s",
                            session_id, exc,
                        )
            except asyncio.CancelledError:
                return

        task = asyncio.create_task(_pulse(), name=f"turn-heartbeat:{key}")
        self._turn_heartbeat_tasks[key] = task

    async def build_state_envelope(
        self, app_id: str, session_id: str, user_id: str = "local",
    ) -> dict[str, Any]:
        """Assemble the authoritative state envelope for a session.

        This is THE contract between server and client. Anything the
        client's UI needs to render correctly lives here. The client
        treats whatever this function returns as "ground truth" -
        local state is recomputed from this whenever uncertainty arises
        (reconnect, session switch, missed event, watchdog timeout).

        Safe to call from any context; read-mostly (only queue depth
        and compaction lookup touch the DB).
        """
        # Current session-scoped seq - the max seq already emitted on
        # the bus for this session. The client keeps its own
        # ``last_seen_seq`` and compares against ``envelope.seq`` to
        # detect whether it's caught up. Reads the in-memory counter
        # directly so we don't accidentally bump it (``next_seq`` would).
        current_seq = 0
        try:
            buffer = getattr(self.event_bus, "_buffer", None)
            if buffer is not None and hasattr(buffer, "_seq"):
                scope_key = f"session::{session_id}"
                current_seq = int(buffer._seq.get(scope_key, 0) or 0)
        except Exception:
            current_seq = 0

        # Queue snapshot - same payload shape as the SSE queue:snapshot
        # event, for client-side reuse of the existing reducer.
        queue_payload: dict[str, Any] = {
            "entries": [], "depth": 0,
            "is_active": False, "running_correlation_id": None,
        }
        try:
            from digitorn.core.app import message_queue as _mq
            entries = await _mq.list_for_session(session_id)
            running = next(
                (e for e in entries if e.status == "running"), None,
            )
            queue_payload = {
                "entries": [e.to_dict() for e in entries],
                "depth": len(entries),
                "is_active": running is not None,
                "running_correlation_id": (
                    running.correlation_id if running else None
                ),
            }
        except Exception as exc:
            logger.debug("state_envelope queue failed: %s", exc)

        # Compaction - look up the latest for this session so the
        # client can show "context compacted at …" badges and decide
        # whether to fetch gap events from a later seq.
        compaction_info: dict[str, Any] = {
            "had_compaction": False, "last_at_seq": None,
        }
        try:
            from digitorn.core.database import get_session_factory
            from digitorn.core.models import HistoryLog
            from sqlalchemy import select
            factory = get_session_factory()
            async with factory() as db:
                row = (await db.execute(
                    select(HistoryLog)
                    .where(HistoryLog.kind == "event")
                    .where(HistoryLog.type == "compaction")
                    .where(HistoryLog.session_id == session_id)
                    .order_by(HistoryLog.seq.desc())
                    .limit(1)
                )).scalar_one_or_none()
                if row is not None:
                    compaction_info = {
                        "had_compaction": True,
                        "last_at_seq": int(row.seq),
                        "kept_from_seq": int(
                            (row.payload or {}).get("kept_range", {}).get("from_seq", 0)
                        ) if isinstance(row.payload, dict) else 0,
                    }
        except Exception as exc:
            logger.debug("state_envelope compaction failed: %s", exc)

        # Turn - live TurnState or None
        turn_payload = self.turn_state_snapshot(app_id, session_id)

        from datetime import datetime, timezone as _tz
        return {
            "schema_version": 1,
            "app_id": app_id,
            "session_id": session_id,
            "user_id": user_id,
            "seq": current_seq,
            "turn": turn_payload,
            "queue": queue_payload,
            "compaction": compaction_info,
            "server_time": datetime.now(_tz.utc).isoformat(),
        }

    def reserve_session(self, app_id: str, session_id: str) -> bool:
        """Atomically reserve a session as active. Returns False if already active."""
        key = f"{app_id}:{session_id}"
        if key in self._active_sessions:
            return False
        self._active_sessions.add(key)
        return True

    def release_session(self, app_id: str, session_id: str) -> None:
        self._active_sessions.discard(f"{app_id}:{session_id}")

    async def list_sessions(
        self,
        app_id: str,
        user_id: str | None = None,
        limit: int = 0,
        offset: int = 0,
        *,
        include_empty: bool = False,
    ) -> list[dict[str, Any]]:
        """List sessions for an app, optionally filtered by user.

        Enriches each row with the deployed app's visual metadata
        (name, icon, color) so the Flutter client can render rich
        session cards without an extra join. ``is_active`` is added
        by the API layer.

        **Commit-on-first-success** (default): draft sessions created
        via ``POST /sessions`` but where the user never actually sent
        a message are HIDDEN from the list. A session is considered
        "committed" the instant its first user/assistant turn lands.
        This keeps the drawer clean - no empty rows the user never
        asked for, no ghost entries when they tap ``+ New`` and then
        navigate away. Set ``include_empty=True`` for admin cleanup
        views that need to see orphan drafts.
        """
        # Always list the full set first - we filter by "has a
        # real message" before applying pagination so ``limit`` counts
        # against the visible rows, not the raw in-memory ones.
        if user_id:
            rows = await asyncio.to_thread(
                self._session_store.list_for_user,
                app_id, user_id, limit=0, offset=0,
            )
        else:
            rows = await asyncio.to_thread(
                self._session_store.list_for_app,
                app_id, limit=0, offset=0,
            )

        if not include_empty:
            # ``last_message_role`` is "" when the session only holds
            # the injected system prompt - i.e. the user never typed
            # anything. That is the exact definition of "draft" the
            # drawer should omit.
            rows = [r for r in rows if (r.get("last_message_role") or "")]

        # Defensive re-sort by ``last_active`` DESC - the store
        # already sorts, but explicit is safer given the filter above
        # may later reshuffle with additional criteria.
        rows.sort(key=lambda s: s.get("last_active", 0) or 0, reverse=True)

        if offset:
            rows = rows[offset:]
        if limit:
            rows = rows[:limit]

        deployed = self._deployed.get(app_id)
        if deployed is not None:
            meta = deployed.compiled.meta
            app_name = getattr(meta, "name", app_id)
            app_icon = getattr(meta, "icon", "") or ""
            app_color = getattr(meta, "color", "") or ""
            for r in rows:
                r["app_name"] = app_name
                r["app_icon"] = app_icon
                r["app_color"] = app_color

        # Hydrate each session row with REAL tokens + cost from usage_events.
        # Without this, every row shows tokens=0 / cost_usd=0.0 on the list
        # view - the detail endpoint had to be opened to see the truth.
        usage_store = getattr(self, "_usage_store", None)
        if usage_store is not None and rows:
            sids = [r.get("session_id") for r in rows if r.get("session_id")]
            try:
                totals = await usage_store.totals_by_session(
                    app_id=app_id, session_ids=sids,
                )
            except Exception:
                logger.debug("list_sessions: totals_by_session failed", exc_info=True)
                totals = {}
            for r in rows:
                t = totals.get(r.get("session_id"))
                if t:
                    r["tokens"] = {
                        "prompt": t["prompt_tokens"],
                        "completion": t["completion_tokens"],
                        "total": t["tokens"],
                    }
                    r["cost_usd"] = t["cost_usd"]
                else:
                    r["tokens"] = {"prompt": 0, "completion": 0, "total": 0}
                    r["cost_usd"] = 0.0

        return rows

    async def count_sessions(
        self, app_id: str, user_id: str | None = None,
        *, include_empty: bool = False,
    ) -> int:
        """Count total sessions for an app/user (for pagination).

        Default excludes draft sessions so the client's pagination
        math lines up with what ``list_sessions`` returns. Admin views
        that pass ``include_empty=True`` get the raw count including
        orphan drafts.
        """
        if not include_empty:
            # Cheaper to reuse ``list_sessions`` (already filters) than
            # replicate the predicate. We only read length, not rows.
            rows = await self.list_sessions(
                app_id, user_id=user_id, limit=0, offset=0,
            )
            return len(rows)
        if user_id:
            return await asyncio.to_thread(self._session_store.count_for_user, app_id, user_id)
        return len(await asyncio.to_thread(self._session_store._index_get, app_id))


    @staticmethod
    def _deployed_key(
        app_id: str, scope: str = "system",
        owner_user_id: str | None = None,
    ) -> str:
        """Build the ``_deployed`` dict key for a (app_id, scope,
        owner) tuple.

        System: ``system::<app_id>``
        User:   ``user:<uid>:<app_id>``

        This lets the same app_id be deployed in two scopes at
        once (admin system install + user override) without
        collision in the shared map.
        """
        if scope == "user":
            if not owner_user_id:
                raise ValueError("user scope requires owner_user_id")
            return f"user:{owner_user_id}:{app_id}"
        return f"system::{app_id}"

    def get(
        self,
        app_id: str,
        *,
        user_id: str | None = None,
    ) -> DeployedApp | None:
        """Get a deployed app by ID, resolved for a specific caller.

        Resolution order:
          1. User-scoped deploy owned by ``user_id`` (when provided)
          2. System-scoped deploy
          3. Legacy: bare ``app_id`` key (backwards compat for
             tests and old code paths that haven't been updated)

        Returns None if nothing matches.
        """
        if user_id:
            user_key = self._deployed_key(app_id, "user", user_id)
            hit = self._deployed.get(user_key)
            if hit is not None:
                return hit
        system_key = self._deployed_key(app_id, "system")
        hit = self._deployed.get(system_key)
        if hit is not None:
            return hit
        # Legacy bare key - kept for backwards compat with old
        # call sites that pre-date the scoping refactor.
        legacy = self._deployed.get(app_id)
        if legacy is not None:
            return legacy
        # Last resort: scan any user-scoped deploy of this app. Needed
        # for admin-style tools (diagnostics, /api/apps listing from a
        # session-less caller) that previously returned "not deployed"
        # for every user-scoped app because no user_id was passed in.
        suffix = f":{app_id}"
        for key, app in self._deployed.items():
            if key.endswith(suffix) and key.startswith("user:"):
                return app
        return None

    def list_apps(
        self,
        *,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List deployed apps visible to a caller.

        Without ``user_id``, returns every deploy (admin view).
        With ``user_id``, returns:
          - every system-scoped deploy
          - every user-scoped deploy belonging to the caller
        Any system deploy shadowed by a user deploy of the same
        app_id is hidden (user version wins).

        Disabled apps are invisible here - they're not in ``_deployed``.
        Use ``list_disabled_apps()`` (admin-only at the API layer) to
        surface them.
        """
        if user_id is None:
            return [app.summary() for app in self._deployed.values()]

        # User-scoped view: filter + shadow
        seen_app_ids: set[str] = set()
        out: list[dict[str, Any]] = []
        # User deploys first so they shadow system ones
        for key, app in self._deployed.items():
            if getattr(app, "scope", "system") == "user":
                if getattr(app, "owner_user_id", None) != user_id:
                    continue
                out.append(app.summary())
                seen_app_ids.add(app.app_id)
        for key, app in self._deployed.items():
            if getattr(app, "scope", "system") != "user":
                if app.app_id in seen_app_ids:
                    continue
                out.append(app.summary())
                seen_app_ids.add(app.app_id)
        return out

    async def list_disabled_apps(
        self,
        *,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a minimal summary of every disabled app.

        Scoping: when ``user_id`` is provided, returns only:
          - the user's own disabled installs (scope='user', owner=user_id)
          - every disabled system install (scope='system')
        When ``user_id`` is None (admin view), returns every disabled
        install across all scopes.

        Disabled apps are not in ``_deployed``; this reads from DB.
        """
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import Application
        from sqlalchemy import or_, select

        try:
            sf = get_session_factory()
        except RuntimeError:
            return []

        async with sf() as session:
            stmt = select(Application).where(Application.disabled == True)  # noqa: E712
            if user_id is not None:
                stmt = stmt.where(
                    or_(
                        Application.scope == "system",
                        (Application.scope == "user") & (Application.owner_user_id == user_id),
                    )
                )
            r = await session.execute(stmt)
            rows = r.scalars().all()

        return [
            {
                "app_id": a.app_id,
                "scope": a.scope,
                "owner_user_id": a.owner_user_id,
                "name": a.name,
                "version": a.version,
                "disabled": True,
                "disabled_at": a.disabled_at.isoformat() if a.disabled_at else None,
                "disabled_reason": a.disabled_reason or "",
                "has_bundle": a.current_bundle_id is not None,
            }
            for a in rows
        ]

    def is_deployed(
        self, app_id: str, *, user_id: str | None = None,
    ) -> bool:
        """Check if an app is deployed (and visible to the caller
        when ``user_id`` is provided)."""
        return self.get(app_id, user_id=user_id) is not None

    # -- Secret store delegation -----------------------------------------

    async def list_secrets(self, app_id: str) -> list[str]:
        """List secret keys for an app."""
        return await self._secret_store.list_secrets(app_id)

    async def get_secret(self, app_id: str, key: str) -> str | None:
        """Retrieve a single secret value."""
        return await self._secret_store.get_secret(app_id, key)

    async def set_secret(self, app_id: str, key: str, value: str) -> None:
        """Store (or overwrite) a secret."""
        await self._secret_store.set_secret(app_id, key, value)

    async def delete_secret(self, app_id: str, key: str) -> bool:
        """Delete a secret. Returns True if it existed."""
        return await self._secret_store.delete_secret(app_id, key)

    # -- Session event store delegation ----------------------------------

    async def load_session_events(
        self, app_id: str, session_id: str, *, user_id: str = "local",
    ) -> list[dict[str, Any]]:
        """Load persisted events for a session, seq-ordered, real-time.

        Reads directly from the ``session_events`` DB table via the
        session bus - same source as Socket.IO ``join_session`` replay.
        This guarantees:

        - No lag: events written during an in-progress turn are
          returned immediately (the KV turn log used to be flushed
          only at turn end, which hid the in-progress state).
        - Single source of truth: REST ``/history`` and Socket.IO
          replay return the exact same envelopes in the exact same
          order, so the client never sees divergent timelines.
        - Includes fast-path ``user_message``: the old KV log was
          captured via a per-turn bus handler that was installed
          *after* the fast-path user_message was published, dropping
          it from the history.
        """
        bus = self.event_bus
        if bus is None:
            return []
        try:
            return await bus.async_replay(
                user_id or "local", 0, session_id=session_id,
            )
        except Exception as exc:
            logger.debug("load_session_events_failed: %s", exc)
            return []

    # -- Job store delegation --------------------------------------------

    @property
    def job_store(self) -> JobStore:
        """Public access to the job store (used by the API for buffer draining)."""
        return self._job_store

    async def cleanup_session(self, app_id: str, session_id: str) -> None:
        """Clean up all session-scoped resources (agents, notifications, tasks, metrics)."""
        deployed = self._deployed.get(app_id)
        if deployed is None:
            return

        # Clean agent_spawn
        for mod in deployed.modules.values():
            if hasattr(mod, "cleanup_session"):
                try:
                    await mod.cleanup_session(session_id)
                except Exception:
                    logger.debug("cleanup_session failed for module %s", mod, exc_info=True)

        # Clean context_builder resources
        cb = deployed.entry_context.context_builder
        if cb is not None:
            if hasattr(cb, "cleanup_session_queue"):
                cb.cleanup_session_queue(session_id)
            if hasattr(cb, "cleanup_session_bg_tasks"):
                try:
                    await cb.cleanup_session_bg_tasks(session_id)
                except Exception:
                    logger.debug("cleanup_session_bg_tasks failed", exc_info=True)
        # Clean session metrics - prevent unbounded memory growth
        try:
            from digitorn.core.runtime.session_metrics import remove_session_metrics
            remove_session_metrics(app_id, session_id)
        except Exception:
            pass

        # Clean image store - prevent disk leak from session image directories
        try:
            from digitorn.core.image_store import get_image_store
            get_image_store().cleanup_session(session_id)
        except Exception:
            logger.debug("image_store_cleanup_failed session=%s", session_id, exc_info=True)

    async def undeploy(
        self, app_id: str, *, user_id: str | None = None,
    ) -> bool:
        """Undeploy an app - graceful shutdown of all its modules.

        Scope-aware: when ``user_id`` is passed, targets the user-
        scoped deploy belonging to that user. Without it, targets
        the system-scoped deploy. Falls back to legacy bare key
        lookup for backwards compat.

        Returns True if the app was deployed and is now removed.
        Built-in apps cannot be undeployed.
        """
        # Resolve which key to pop
        if user_id:
            key = self._deployed_key(app_id, "user", user_id)
        else:
            key = self._deployed_key(app_id, "system")
        deployed = self._deployed.get(key)
        if deployed is None:
            # Legacy bare key fallback
            deployed = self._deployed.get(app_id)
            if deployed is None:
                return False
            key = app_id
        if getattr(deployed, "builtin", False):
            raise RuntimeError(f"Cannot undeploy built-in app '{app_id}'")
        self._deployed.pop(key, None)

        # Stop the hot reloader if present - must run before the
        # other shutdowns so it doesn't try to redeploy mid-undeploy.
        if getattr(deployed, "hot_reloader", None) is not None:
            try:
                await deployed.hot_reloader.stop()
            except Exception as exc:
                logger.warning(
                    "hot_reloader_stop_failed app=%s: %s", app_id, exc,
                )

        # Stop the preview dev server if present.
        if getattr(deployed, "preview_manager", None) is not None:
            try:
                await deployed.preview_manager.stop()
            except Exception as exc:
                logger.warning(
                    "preview_manager_stop_failed app=%s: %s", app_id, exc,
                )

        # Shutdown sandbox: pool or single worker
        if deployed.sandbox_pool is not None:
            try:
                await deployed.sandbox_pool.shutdown()
            except Exception as exc:
                logger.warning("sandbox_pool_shutdown_failed app=%s: %s", app_id, exc)
        if deployed.sandbox_worker is not None:
            try:
                await deployed.sandbox_worker.stop()
            except Exception as exc:
                logger.warning("sandbox_worker_stop_failed app=%s: %s", app_id, exc)

        # Drain: warn about active sessions and cancel pending approvals
        active_keys = [k for k in self._active_sessions if k.startswith(f"{app_id}:")]
        if active_keys:
            logger.warning(
                "Undeploying '%s' with %d active session(s): %s",
                app_id, len(active_keys), active_keys,
            )
        if deployed.approval_queue is not None:
            try:
                deployed.approval_queue.cancel_all()
            except Exception as exc:
                logger.warning("Failed to cancel pending approvals for '%s': %s", app_id, exc, exc_info=True)

        await asyncio.to_thread(self._session_store.delete_for_app, app_id)

        # Clear circuit breaker state for providers used by this app
        from digitorn.core.runtime.agent_loop import clear_circuit_breakers
        provider_ids = set()
        for ctx in deployed.contexts.values():
            pid = getattr(ctx.provider, "provider_id", None)
            if pid:
                provider_ids.add(str(pid))
        if provider_ids:
            clear_circuit_breakers(*provider_ids)

        for module_id, module in deployed.modules.items():
            try:
                await module.on_stop()
            except Exception as exc:
                logger.warning("Module '%s' on_stop failed: %s", module_id, exc, exc_info=True)

        if deployed.context_builder:
            try:
                await deployed.context_builder.on_stop()
            except Exception as exc:
                logger.warning("context_builder on_stop failed: %s", exc, exc_info=True)

        for ctx in deployed.contexts.values():
            if hasattr(ctx, "tool_index"):
                ctx.tool_index = None

        self._llm_channel.unregister_context_builder(app_id)
        self._scheduler.unregister_app_executor(app_id)
        self._scheduler.unregister_wake_handler(app_id)

        try:
            await self._channel_registry.stop_and_remove_for_app(app_id)
        except Exception as exc:
            logger.warning("channel_cleanup_failed app=%s: %s", app_id, exc, exc_info=True)

        if self._runtime_store:
            self._runtime_store.unregister(app_id)

        logger.info("App '%s' undeployed", app_id)
        return True

    async def delete_app(
        self,
        app_id: str,
        *,
        user_id: str | None = None,
        scope: str | None = None,
        delete_history: bool = True,
    ) -> dict[str, Any]:
        """Permanently remove a scoped app install - memory, bundles, DB rows, secrets.

        **Multi-tenant scoping** (identifies which install to remove):

        - Pass ``user_id="alice"`` to remove Alice's private install.
          Bob's install of the same ``app_id`` is untouched; so is any
          system install.
        - Pass ``scope="system"`` (admin path) to force removal of the
          system install even when a user_id is available.
        - Pass nothing (default): the caller is acting on a system
          install - matches legacy behaviour.

        Pipeline (hard delete):

        1. ``undeploy(app_id, user_id=...)`` - stops the scoped in-memory
           instance, shuts down sandbox, cancels approvals, drains
           sessions.
        2. Wipe the app's scoped directory on disk (scope-aware: system
           stays at ``~/.digitorn/apps/{app_id}/``, user installs use
           ``~/.digitorn/apps/_@{uid}__{app_id}/`` - see
           ``_scoped_slug``). Other scopes of the same app_id are
           **NOT** touched.
        3. Delete the single matching ``Application`` row. SQLAlchemy's
           cascade removes its ``AppProfile``, ``AppModuleGrant``,
           ``AppModuleConfig``, ``AppBundle`` and (when
           ``delete_history=True``) sessions/messages/activations.
        4. Purge the secret store for this scope.

        Built-in apps raise ``RuntimeError``.

        Returns::

            {
                "app_id": "...",
                "scope": "system" | "user",
                "owner_user_id": "" | "<uid>",
                "deployed": bool,
                "bundles_deleted": int,
                "disk_removed": bool,
                "secrets_deleted": int,
                "db_removed": bool,
                "history_preserved": bool,
            }
        """
        from digitorn.core.app.bundle_store import BundleStoreError

        # Resolve the (scope, owner) tuple once - every step below uses it.
        resolved_scope, resolved_owner = _normalize_scope(user_id, scope)

        # Guard: built-in apps are off-limits (any scope).
        deployed = self.get(app_id, user_id=resolved_owner or None)
        if deployed is not None and getattr(deployed, "builtin", False):
            raise RuntimeError(
                f"Cannot delete built-in app '{app_id}' - "
                f"it will be re-created on the next boot anyway."
            )

        scoped_slug = _scoped_slug(app_id, resolved_scope, resolved_owner)

        result: dict[str, Any] = {
            "app_id": app_id,
            "scope": resolved_scope,
            "owner_user_id": resolved_owner,
            "deployed": False,
            "bundles_deleted": 0,
            "disk_removed": False,
            "secrets_deleted": 0,
            "db_removed": False,
            "history_preserved": not delete_history,
        }

        # Step 1 - undeploy from memory (scope-aware; idempotent).
        try:
            was_deployed = await self.undeploy(
                app_id, user_id=resolved_owner or None,
            )
            result["deployed"] = bool(was_deployed)
        except RuntimeError:
            raise  # built-in - propagate
        except Exception as exc:
            logger.warning(
                "undeploy failed during delete_app '%s' scope=%s: %s",
                app_id, resolved_scope, exc, exc_info=True,
            )

        # Step 2 - delete bundles from disk for THIS scope.
        # The scoped_slug isolates user installs so Bob's copy survives
        # when Alice runs delete.
        try:
            bundle_count = self._bundle_store.delete_app(scoped_slug)
            result["bundles_deleted"] = bundle_count
        except BundleStoreError as exc:
            logger.warning("bundle cleanup failed for '%s': %s", scoped_slug, exc)
        except Exception as exc:
            logger.warning(
                "bundle cleanup raised unexpected error for '%s': %s",
                scoped_slug, exc, exc_info=True,
            )

        # Wipe any leftover files inside the scoped app dir.
        import shutil
        app_dir = Path.home() / ".digitorn" / "apps" / scoped_slug
        try:
            if app_dir.exists():
                shutil.rmtree(app_dir, ignore_errors=False)
                result["disk_removed"] = True
            else:
                # Previously reported True here, which caused the API to
                # tell callers "disk_removed: true" even when there was
                # nothing to remove (BUG-048 - user deletes a built-in
                # system app they never installed, gets a success dict
                # detailing fictional cleanup).
                result["disk_removed"] = False
        except Exception as exc:
            logger.warning(
                "disk wipe failed for '%s' (%s): %s",
                scoped_slug, app_dir, exc, exc_info=True,
            )

        # Step 3 - delete DB rows.
        # Use explicit SQL via `get_session_factory` so we blow up
        # loudly (instead of silently no-op) when the DB isn't initialised.
        try:
            from digitorn.core.database import get_session_factory
            sf = get_session_factory()
        except RuntimeError as exc:
            logger.error(
                "delete_app_db_unavailable app=%s: %s", app_id, exc,
            )
            sf = None

        if sf is not None:
            from sqlalchemy import text as _sql_text
            scope_filter = (
                "app_id = :a AND scope = :s AND owner_user_id = :o"
            )
            params = {
                "a": app_id, "s": resolved_scope, "o": resolved_owner,
            }
            try:
                async with sf() as session:
                    async with session.begin():
                        # Break the FK from applications → app_bundles for
                        # THIS scope only. Other scopes stay intact.
                        await session.execute(
                            _sql_text(
                                f"UPDATE applications SET current_bundle_id = NULL "
                                f"WHERE {scope_filter}"
                            ),
                            params,
                        )
                        if delete_history:
                            # Hard delete for THIS scope. ORM cascade
                            # covers AppProfile, AppModuleConfig and
                            # UserSession (those still have FKs). We
                            # explicitly delete app_bundles rows because
                            # we dropped that FK in the scoping refactor
                            # (composite keys can't be single-column FK
                            # in SQLite).
                            await session.execute(
                                _sql_text(
                                    "DELETE FROM app_bundles "
                                    "WHERE app_id = :a "
                                    "  AND scope = :s "
                                    "  AND owner_user_id = :o"
                                ),
                                params,
                            )
                            result_rows = await session.execute(
                                _sql_text(
                                    f"DELETE FROM applications "
                                    f"WHERE {scope_filter}"
                                ),
                                params,
                            )
                            result["db_removed"] = bool(result_rows.rowcount)
                        else:
                            # History-preservation for THIS scope only.
                            from datetime import datetime as _dt, timezone as _tz
                            now = _dt.now(_tz.utc).isoformat()
                            await session.execute(
                                _sql_text(
                                    f"UPDATE applications "
                                    f"SET disabled = :d, "
                                    f"    disabled_at = :t, "
                                    f"    disabled_reason = :r "
                                    f"WHERE {scope_filter}"
                                ),
                                {
                                    **params,
                                    "d": True,
                                    "t": now,
                                    "r": "preserved_after_delete",
                                },
                            )
                            # Delete bundle rows for THIS scoped app row.
                            # app_bundles also carries (scope, owner_user_id)
                            # since the refactor so the filter is direct.
                            await session.execute(
                                _sql_text(
                                    "DELETE FROM app_bundles "
                                    "WHERE app_id = :a "
                                    "  AND scope = :s "
                                    "  AND owner_user_id = :o"
                                ),
                                params,
                            )
                            result["db_removed"] = False
            except Exception as exc:
                logger.error(
                    "DB cleanup failed for '%s' scope=%s owner=%s: %s",
                    app_id, resolved_scope, resolved_owner, exc, exc_info=True,
                )
                raise

        # Step 4 - purge secrets.
        try:
            secret_keys = await self._secret_store.list_secrets(app_id)
            for k in secret_keys:
                try:
                    await self._secret_store.delete_secret(app_id, k)
                    result["secrets_deleted"] += 1
                except Exception as exc:
                    logger.warning(
                        "secret delete failed app=%s key=%s: %s",
                        app_id, k, exc,
                    )
        except Exception as exc:
            logger.debug("secret listing failed for '%s': %s", app_id, exc)

        logger.info(
            "app_deleted app=%s scope=%s owner=%r deployed=%s bundles=%d "
            "disk=%s secrets=%d db=%s history=%s",
            app_id,
            resolved_scope,
            resolved_owner,
            result["deployed"],
            result["bundles_deleted"],
            result["disk_removed"],
            result["secrets_deleted"],
            result["db_removed"],
            "preserved" if result["history_preserved"] else "purged",
        )
        # Truth-check: if absolutely nothing changed on disk, in DB, or
        # in memory, this was a no-op (user asked to delete something
        # that doesn't belong to them / doesn't exist at this scope).
        # Previously the response still said `deleted: true`; callers
        # believed their data was wiped when it wasn't.
        nothing_happened = (
            not result["deployed"]
            and result["bundles_deleted"] == 0
            and not result["disk_removed"]
            and result["secrets_deleted"] == 0
            and not result["db_removed"]
        )
        result["actually_deleted"] = not nothing_happened
        return result

    async def disable_app(
        self,
        app_id: str,
        *,
        user_id: str | None = None,
        scope: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Disable a scoped app install: undeploy + hide from non-admin list/get.

        Differs from ``delete_app`` in that nothing is removed from disk
        or DB - disabling is fully reversible via ``enable_app``. Only
        the install matching ``(app_id, scope, owner_user_id)`` is
        disabled; other scopes of the same app_id stay live.

        Built-in apps cannot be disabled.
        """
        from digitorn.core.database import get_session_factory
        from datetime import datetime as _dt, timezone as _tz
        from sqlalchemy import text as _sql_text

        resolved_scope, resolved_owner = _normalize_scope(user_id, scope)

        deployed = self.get(app_id, user_id=resolved_owner or None)
        if deployed is not None and getattr(deployed, "builtin", False):
            raise RuntimeError(f"Cannot disable built-in app '{app_id}'.")

        was_deployed = False
        try:
            was_deployed = await self.undeploy(
                app_id, user_id=resolved_owner or None,
            )
        except RuntimeError:
            raise
        except Exception as exc:
            logger.warning(
                "undeploy during disable '%s' scope=%s: %s",
                app_id, resolved_scope, exc, exc_info=True,
            )

        try:
            sf = get_session_factory()
        except RuntimeError as exc:
            raise RuntimeError(f"Cannot disable: DB not initialised ({exc})") from exc

        now = _dt.now(_tz.utc).isoformat()
        async with sf() as session:
            async with session.begin():
                r = await session.execute(
                    _sql_text(
                        "UPDATE applications "
                        "SET disabled = :d, "
                        "    disabled_at = :t, "
                        "    disabled_reason = :r "
                        "WHERE app_id = :a AND scope = :s AND owner_user_id = :o"
                    ),
                    {
                        "d": True,
                        "t": now, "r": reason or "", "a": app_id,
                        "s": resolved_scope, "o": resolved_owner,
                    },
                )
                if r.rowcount == 0:
                    raise RuntimeError(
                        f"App '{app_id}' (scope={resolved_scope}, "
                        f"owner={resolved_owner!r}) not found in DB"
                    )

        logger.info(
            "app_disabled app=%s scope=%s owner=%r reason=%r",
            app_id, resolved_scope, resolved_owner, reason,
        )
        return {
            "app_id": app_id,
            "scope": resolved_scope,
            "owner_user_id": resolved_owner,
            "disabled": True,
            "was_deployed": was_deployed,
        }

    async def enable_app(
        self,
        app_id: str,
        *,
        user_id: str | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Re-enable a disabled scoped install and redeploy it.

        Admin-only at the API layer. The single install matching
        ``(app_id, scope, owner_user_id)`` is flipped back to
        ``disabled=False`` and redeployed from its stored bundle.
        Fails if the bundle was wiped (e.g. previous
        ``delete_history=False`` call).
        """
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import AppBundle, Application
        from sqlalchemy import select, text as _sql_text

        resolved_scope, resolved_owner = _normalize_scope(user_id, scope)
        sf = get_session_factory()

        async with sf() as session:
            async with session.begin():
                row = await session.execute(
                    select(Application).where(
                        Application.app_id == app_id,
                        Application.scope == resolved_scope,
                        Application.owner_user_id == resolved_owner,
                    )
                )
                app_row = row.scalar_one_or_none()
                if app_row is None:
                    raise RuntimeError(
                        f"App '{app_id}' (scope={resolved_scope}, "
                        f"owner={resolved_owner!r}) not found"
                    )
                if not app_row.disabled:
                    return {
                        "app_id": app_id,
                        "scope": resolved_scope,
                        "owner_user_id": resolved_owner,
                        "enabled": True,
                        "was_disabled": False,
                    }
                if app_row.current_bundle_id is None:
                    raise RuntimeError(
                        f"App '{app_id}' cannot be re-enabled: no bundle "
                        f"(deleted with delete_history=False)."
                    )
                await session.execute(
                    _sql_text(
                        "UPDATE applications "
                        "SET disabled = :d, "
                        "    disabled_at = NULL, "
                        "    disabled_reason = NULL "
                        "WHERE app_id = :a AND scope = :s AND owner_user_id = :o"
                    ),
                    {
                        "d": False,
                        "a": app_id, "s": resolved_scope, "o": resolved_owner,
                    },
                )

        # Redeploy from the saved bundle (scope-aware bundle path).
        redeployed = False
        try:
            async with sf() as session:
                row = await session.execute(
                    select(Application).where(
                        Application.app_id == app_id,
                        Application.scope == resolved_scope,
                        Application.owner_user_id == resolved_owner,
                    )
                )
                app_row = row.scalar_one_or_none()
                if app_row and app_row.current_bundle_id:
                    br = await session.execute(
                        select(AppBundle).where(AppBundle.id == app_row.current_bundle_id)
                    )
                    bundle_row = br.scalar_one_or_none()
                    if bundle_row is not None:
                        scoped = _scoped_slug(app_id, resolved_scope, resolved_owner)
                        descriptor = self._bundle_store.get_by_path(
                            scoped, bundle_row.bundle_path,
                        )
                        if descriptor is not None:
                            await self._deploy_from_bundle(
                                descriptor,
                                scope=resolved_scope,
                                owner_user_id=resolved_owner or None,
                            )
                            redeployed = True
        except Exception as exc:
            logger.error(
                "enable_app_redeploy_failed app=%s scope=%s: %s",
                app_id, resolved_scope, exc, exc_info=True,
            )

        logger.info(
            "app_enabled app=%s scope=%s owner=%r redeployed=%s",
            app_id, resolved_scope, resolved_owner, redeployed,
        )
        return {
            "app_id": app_id,
            "scope": resolved_scope,
            "owner_user_id": resolved_owner,
            "enabled": True,
            "was_disabled": True,
            "redeployed": redeployed,
        }

    async def reload_app(self, app_id: str) -> dict[str, Any]:
        """Hot-reload a single deployed app from its current bundle.

        Use this when a persistent resource the app depends on has
        changed and the in-memory instance is now stale - typically
        after a secret / API key rotation, a module config tweak, or an
        external dependency swap.

        Pipeline:

        1. Load the ``Application`` row + its ``current_bundle`` from DB.
        2. Stop the currently-running in-memory instance (``undeploy``).
        3. Re-read the bundle from disk via ``BundleStore``.
        4. Recompile using the **fresh** secrets from ``SecretStore`` -
           so a PUT /secrets/{key} made just before this call is picked
           up automatically.
        5. Re-bootstrap the app and put it back in ``_deployed``.

        The DB rows are NOT modified - same ``app_id``, same bundle
        hash, same profile / grants / configs. Only the in-memory state
        is rebuilt. Sessions tied to the app are dropped (they would be
        inconsistent with the new module state anyway).

        Returns a status dict: ``{app_id, reloaded, bundle_hash,
        secrets_applied}``.

        Raises:
            KeyError: if the app is not in the DB.
            FileNotFoundError: if the bundle is missing from disk.
            RuntimeError: if the app is built-in (built-ins are reloaded
                via ``_deploy_builtin_apps`` at daemon startup).
        """
        from sqlalchemy import select as _select
        from sqlalchemy.orm import selectinload as _selectinload

        from digitorn.core.database import get_session_factory
        from digitorn.core.models import AppBundle as _AppBundle
        from digitorn.core.models import Application as _Application

        # Built-in apps own their lifecycle via _deploy_builtin_apps.
        existing = self._deployed.get(app_id)
        if existing is not None and getattr(existing, "builtin", False):
            raise RuntimeError(
                f"Cannot hot-reload built-in app '{app_id}' - "
                f"restart the daemon to pick up changes.",
            )

        # Fetch the app + its current bundle from DB.
        _sf = get_session_factory()
        async with _sf() as session:
            result = await session.execute(
                _select(_Application)
                .options(_selectinload(_Application.current_bundle))
                .where(_Application.app_id == app_id)
            )
            app_row = result.scalar_one_or_none()

        if app_row is None:
            raise KeyError(f"App '{app_id}' not found in database.")

        bundle_row: _AppBundle | None = app_row.current_bundle
        if bundle_row is None:
            # Legacy app without a bundle - fall back to yaml_content
            # reload. Rare: only happens on pre-bundle deploys that
            # never got re-deployed after the bundle refactor.
            if not app_row.yaml_content:
                raise FileNotFoundError(
                    f"App '{app_id}' has no bundle AND no yaml_content. "
                    f"Deploy it again from the source YAML.",
                )
            await self._deploy_from_content(
                app_row.yaml_content,
                source=app_row.yaml_path or app_id,
            )
            return {
                "app_id": app_id,
                "reloaded": True,
                "bundle_hash": None,
                "secrets_applied": 0,
                "source": "legacy_yaml_content",
            }

        descriptor = self._bundle_store.get_by_path(
            app_id, bundle_row.bundle_path,
        )
        if descriptor is None:
            raise FileNotFoundError(
                f"Bundle for '{app_id}' is missing on disk at "
                f"{bundle_row.bundle_path}. Re-deploy the app.",
            )

        # Count secrets for the return payload (so the caller knows
        # how many keys are currently active).
        try:
            current_secrets = await self._secret_store.get_all(app_id)
        except Exception:
            current_secrets = {}

        # _deploy_from_bundle undeploys the old instance, recompiles
        # with fresh secrets from SecretStore, and re-bootstraps.
        await self._deploy_from_bundle(descriptor)

        logger.info(
            "app_reloaded app=%s bundle=%s secrets=%d",
            app_id, descriptor.short_hash, len(current_secrets),
        )

        return {
            "app_id": app_id,
            "reloaded": True,
            "bundle_hash": descriptor.bundle_hash,
            "secrets_applied": len(current_secrets),
            "source": "bundle",
        }

    async def reload_from_db(self, *, parallelism: int = 4) -> list[str]:
        """Reload all apps from the database at daemon startup.

        Priority order for recompilation:
        1. AppBundle on disk (via ``current_bundle_id``) - the primary
           path since the bundle contains the YAML plus every referenced
           asset (skills, prompts, …). The source filesystem is never
           touched.
        2. Legacy fallback: ``yaml_content`` stored directly on the
           Application row (pre-bundle deploys). This path will be
           removed once all existing installs have been migrated.

        Apps are reloaded **concurrently** with a bounded semaphore
        (default width 4) so shared modules like ``rag`` / ``vector``
        don't race on ``on_config_update``. Sequential semantics are
        preserved within each app, only the apps themselves fan out.

        Returns list of app_ids that were successfully reloaded.
        """
        import asyncio as _asyncio

        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from digitorn.core.database import _session_factory
        from digitorn.core.models import Application

        if _session_factory is None:
            logger.warning("Cannot reload apps: database not initialized")
            return []

        async with _session_factory() as session:
            result = await session.execute(
                select(Application).options(selectinload(Application.current_bundle))
            )
            apps = list(result.scalars().all())

        if not apps:
            return []

        sem = _asyncio.Semaphore(max(1, int(parallelism)))

        async def _reload_with_sem(app_row: Any) -> str | None:
            async with sem:
                return await self._reload_one_app(app_row)

        results = await _asyncio.gather(
            *(_reload_with_sem(row) for row in apps),
            return_exceptions=True,
        )

        reloaded: list[str] = []
        for app_row, res in zip(apps, results):
            if isinstance(res, BaseException):
                logger.error(
                    "Failed to reload '%s': %s",
                    app_row.app_id, res, exc_info=res,
                )
                continue
            if res:
                reloaded.append(res)

        if reloaded:
            logger.info("Reloaded %d app(s) from DB: %s", len(reloaded), reloaded)
        return reloaded

    async def _reload_one_app(self, app_row: Any) -> str | None:
        """Reload a single app - the body of the loop extracted so
        ``reload_from_db`` can run them in parallel. Returns the
        ``app_id`` on success, ``None`` on skip / purge, and raises on
        hard failure (the caller logs with ``exc_info``).
        """
        from sqlalchemy import delete as _delete
        from sqlalchemy import update as _update

        from digitorn.core.database import get_session_factory
        from digitorn.core.models import AppBundle, Application

        # Keep the original variable names so the inlined body (copied
        # verbatim from the old ``for`` loop) keeps working unchanged.
        app_id = app_row.app_id
        row_scope = getattr(app_row, "scope", "system") or "system"
        row_owner = getattr(app_row, "owner_user_id", "") or ""

        # Skip disabled apps - they stay registered in DB but are not
        # deployed to memory. Admins re-enable via enable_app which
        # re-reads the bundle and calls _deploy_from_bundle directly.
        if getattr(app_row, "disabled", False):
            logger.info(
                "reload_skip_disabled app=%s scope=%s owner=%r",
                app_id, row_scope, row_owner,
            )
            return None

        # Path A - bundle on disk (preferred)
        if app_row.current_bundle is not None:
            bundle_row: AppBundle = app_row.current_bundle
            scoped = _scoped_slug(app_id, row_scope, row_owner)
            descriptor = self._bundle_store.get_by_path(
                scoped, bundle_row.bundle_path,
            )
            if descriptor is None:
                logger.error(
                    "Bundle for '%s' (scope=%s) missing on disk at %s - "
                    "falling back to legacy yaml_content",
                    app_id, row_scope, bundle_row.bundle_path,
                )
            else:
                # Guard against corrupt bundles (earlier versions
                # of the syncer could write an empty app.yaml
                # when compiling from a legacy yaml_content).
                # If the YAML looks empty or unparseable, drop
                # the bundle and fall through to the legacy path
                # so the next sync rebuilds it correctly.
                try:
                    _yaml_preview = self._bundle_store.load_yaml(descriptor)
                except Exception as exc:
                    logger.error(
                        "Bundle YAML unreadable for '%s' at %s: %s",
                        app_id, bundle_row.bundle_path, exc,
                    )
                    _yaml_preview = ""

                if _yaml_preview.strip():
                    await self._deploy_from_bundle(
                        descriptor,
                        scope=row_scope,
                        owner_user_id=row_owner or None,
                    )
                    return app_id

                logger.warning(
                    "Bundle for '%s' has an empty YAML - likely "
                    "created by a buggy legacy reload. Deleting "
                    "it and falling back to yaml_content so the "
                    "next deploy rebuilds the bundle properly.",
                    app_id,
                )
                try:
                    self._bundle_store.delete_bundle(
                        app_id, descriptor.bundle_hash,
                    )
                except Exception as exc:
                    logger.debug(
                        "failed to delete corrupt bundle %s: %s",
                        descriptor.bundle_path, exc,
                    )
                # Clear the FK so the next sync re-creates a
                # fresh bundle instead of trying to reuse the
                # broken row.
                try:
                    _sf = get_session_factory()
                    async with _sf() as _s:
                        async with _s.begin():
                            await _s.execute(
                                _update(Application)
                                .where(Application.app_id == app_id)
                                .values(current_bundle_id=None)
                            )
                            await _s.execute(
                                AppBundle.__table__.delete().where(
                                    AppBundle.id == bundle_row.id,
                                )
                            )
                except Exception as exc:
                    logger.debug(
                        "failed to clear current_bundle_id for %s: %s",
                        app_id, exc,
                    )

        # Path B - legacy yaml_content (pre-bundle deploys or
        # recovered from a broken bundle above)
        if app_row.yaml_content:
            logger.info(
                "Reloading legacy app '%s' from yaml_content - "
                "bundle will be created on next deploy",
                app_id,
            )
            await self._deploy_from_content(
                app_row.yaml_content,
                source=app_row.yaml_path or app_id,
            )
            return app_id

        # Path C - orphaned row: no bundle AND no yaml_content.
        # Nothing we can reconstruct from. These rows are leftovers
        # from a pre-refactor deploy where the old syncer failed to
        # persist yaml_content (the bug my refactor inherited and
        # then propagated into an empty bundle). They cannot be
        # reloaded and keeping them around just causes the daemon
        # to log errors at every boot. Purge them aggressively.
        logger.warning(
            "Purging orphaned app '%s' - no bundle AND no "
            "yaml_content on disk. Row is unrecoverable.",
            app_id,
        )
        try:
            _sf = get_session_factory()
            async with _sf() as _cleanup_session:
                async with _cleanup_session.begin():
                    # Break any remaining FK loop before delete
                    await _cleanup_session.execute(
                        _update(Application)
                        .where(Application.app_id == app_id)
                        .values(current_bundle_id=None)
                    )
                    await _cleanup_session.execute(
                        _delete(AppBundle).where(
                            AppBundle.app_id == app_id,
                        )
                    )
                    await _cleanup_session.execute(
                        _delete(Application).where(
                            Application.app_id == app_id,
                        )
                    )
            # Also remove any empty bundle directory left on disk.
            try:
                self._bundle_store.delete_app(app_id)
            except Exception:
                pass
            logger.info("orphan_purged app=%s", app_id)
        except Exception as exc:
            logger.error(
                "failed to purge orphan app '%s': %s",
                app_id, exc, exc_info=True,
            )
        return None

    async def _deploy_from_bundle(
        self, descriptor: Any,
        *,
        scope: str = "system",
        owner_user_id: str | None = None,
    ) -> DeployedApp:
        """Recompile and deploy an app directly from its on-disk bundle.

        The compiler reads YAML + assets through the bundle store's
        asset_loader, so the original source filesystem is never
        accessed. This is the standard path used at daemon startup.

        ``scope`` / ``owner_user_id`` propagate to ``_build_and_deploy``
        so per-user and system installs of the same app_id coexist in
        ``self._deployed`` without overwriting each other.
        """
        yaml_content = self._bundle_store.load_yaml(descriptor)
        peek_app_id = descriptor.app_id
        db_secrets: dict[str, str] = {}
        try:
            db_secrets = await self._secret_store.get_all(peek_app_id)
        except Exception as exc:
            logger.warning(
                "Secret store read failed for '%s': %s",
                peek_app_id, exc, exc_info=True,
            )

        compiled = self._compiler.compile_string(
            yaml_content,
            source=f"bundle://{descriptor.app_id}/{descriptor.short_hash}",
            secrets=db_secrets or None,
            asset_loader=self._bundle_store.asset_loader(descriptor),
        )
        app_id = compiled.app_id

        # compile_string cannot set ``source_path`` (no real filesystem
        # path went in) - but features like PreviewManager need the
        # bundle's on-disk install dir to resolve relative paths like
        # ``preview.cwd=./web``. Look it up from the package registry
        # and stamp it onto the compiled app so downstream code can
        # build a correct ``bundle_dir``.
        install_dir = await self._resolve_install_dir(app_id)
        if install_dir is not None:
            compiled.source_path = install_dir / "app.yaml"

        # Only undeploy the SAME scope - other scopes of the same app_id
        # stay live.
        existing_key = self._deployed_key(app_id, scope, owner_user_id)
        if existing_key in self._deployed:
            await self.undeploy(app_id, user_id=owner_user_id)

        logger.info(
            "Deploying app '%s' scope=%s from bundle %s",
            app_id, scope, descriptor.short_hash,
        )
        return await self._build_and_deploy(
            compiled,
            scope=scope,
            owner_user_id=owner_user_id,
        )

    async def _resolve_install_dir(self, app_id: str) -> "Path | None":
        """Return the install dir of a package (system scope) or None.

        Used by ``_deploy_from_bundle`` to anchor bundle-mode compiles
        to a real filesystem path so relative YAML fields (preview cwd,
        skills paths, assets) resolve correctly.
        """
        registry = getattr(self, "_package_registry", None)
        if registry is None:
            return None
        try:
            from digitorn.core.packages.registry import Scope
            row = await registry.get(app_id, scope=Scope.SYSTEM)
            if row is None:
                # Also try user-scoped - covers per-user builtin shadows
                row = await registry.get(app_id)
            if row is None:
                return None
            install_dir = row.get("install_dir") if isinstance(row, dict) else getattr(row, "install_dir", None)
            if not install_dir:
                return None
            from pathlib import Path as _P
            return _P(install_dir)
        except Exception as exc:
            logger.debug("resolve_install_dir_failed app=%s: %s", app_id, exc)
            return None

    async def _deploy_from_content(
        self, yaml_content: str, *, source: str = "<db>"
    ) -> DeployedApp:
        """Deploy an app from stored YAML content (legacy - no bundle).

        Same lifecycle as deploy() but compiles from a string. Used only
        for legacy pre-bundle deploys during reload - new deploys always
        go through ``_deploy_from_bundle``.
        """
        import yaml as _yaml

        raw = _yaml.safe_load(yaml_content)
        peek_app_id = (raw.get("app") or {}).get("app_id", "")
        db_secrets: dict[str, str] = {}
        if peek_app_id:
            try:
                db_secrets = await self._secret_store.get_all(peek_app_id)
            except Exception as exc:
                logger.warning("Secret store read failed for '%s': %s", peek_app_id, exc, exc_info=True)
        compiled = self._compiler.compile_string(
            yaml_content, source=source, secrets=db_secrets or None,
        )
        app_id = compiled.app_id

        if app_id in self._deployed:
            await self.undeploy(app_id)

        logger.info("Deploying app '%s' from stored YAML content", app_id)
        return await self._build_and_deploy(compiled)


    @property
    def _sandbox_enabled(self) -> bool:
        settings = getattr(self, "_settings", None)
        if settings and hasattr(settings, "server"):
            return getattr(settings.server, "sandbox", False)
        return False

    def _should_sandbox(self, compiled: CompiledApp) -> bool:
        """Check if this app should run in a sandboxed worker."""
        if not self._sandbox_enabled or compiled.security_profile is None:
            return False
        return True

    def _should_use_pool(self, compiled: CompiledApp) -> bool:
        """Check if this app needs a WorkerPool (per-session sandbox).

        Pool mode is required when:
        - workspace_mode=required (different workspace per session, Landlock is irreversible)
        - sandbox.level is strict or maximum
        - pool_size is explicitly configured
        """
        ws_mode = getattr(compiled.execution, "workspace_mode", "auto")
        sandbox_cfg = self._get_sandbox_config(compiled)
        if ws_mode == "required":
            return True  # Must use pool - Landlock can't change workspace
        if sandbox_cfg is not None:
            level = getattr(sandbox_cfg, "level", "standard")
            if level in ("strict", "maximum"):
                return True
        return False

    @staticmethod
    def _get_sandbox_config(compiled: CompiledApp) -> Any:
        """Get the sandbox config from execution block, or None."""
        return getattr(compiled.execution, "sandbox", None)

    async def _build_and_deploy(
        self,
        compiled: CompiledApp,
        *,
        scope: str = "system",
        owner_user_id: str | None = None,
    ) -> DeployedApp:
        """Single bootstrap path for all deploy methods.

        Creates per-app module instances, builds agent contexts,
        syncs to DB, and registers the deployed app.

        When sandbox mode is enabled and the app has a security profile,
        the app is forked into an isolated worker subprocess with
        OS-level enforcement (Landlock/seccomp/Seatbelt/Job Objects).
        """
        app_id = compiled.app_id

        from digitorn.core.runtime.bootstrap import bootstrap as build_agent_contexts

        try:
            _skip_emb = False
            try:
                from digitorn.core.config import get_settings
                _skip_emb = get_settings().discovery.skip_embeddings
            except Exception:
                pass
            agent_result = await build_agent_contexts(compiled, self._registry, skip_embeddings=_skip_emb)
        except Exception as exc:
            raise RuntimeError(
                f"Agent context build failed for '{app_id}': {exc}"
            ) from exc

        if self._runtime_store is not None:
            try:
                self._runtime_store.register(compiled)
            except Exception as exc:
                logger.warning("Runtime store registration failed: %s", exc, exc_info=True)

        try:
            from digitorn.core.app.syncer import AppSyncer

            # Reuse the manager's bundle store so every deploy writes
            # to the same on-disk root.
            syncer = AppSyncer(bundle_store=self._bundle_store)
            # Pass the scope/owner so the row is written under the correct
            # (app_id, scope, owner_user_id) composite key. This is what
            # lets two users install the same app_id in parallel.
            synced = await syncer.sync(
                compiled,
                scope=scope,
                owner_user_id=owner_user_id or "",
            )
            if synced:
                logger.debug("app_db_synced: %s", app_id)
        except Exception as exc:
            logger.warning("app_db_sync_failed: %s - %s", app_id, exc, exc_info=True)

        channels_created = 0
        for name, ch_compiled in compiled.channels.items():
            try:
                instance = self._channel_registry.create_instance(
                    name, ch_compiled.channel_type, ch_compiled.config,
                    app_id=app_id,
                    resolver_config=ch_compiled.user_resolver,
                )
                await self._channel_registry.start_instance(name)
                channels_created += 1
            except Exception as exc:
                logger.warning(
                    "channel_create_failed: %s (type=%s) - %s",
                    name, ch_compiled.channel_type, exc, exc_info=True,
                )

        cb = agent_result["context_builder"]
        if cb is not None and hasattr(cb, "set_job_store"):
            try:
                cb.set_job_store(self._job_store)
                cb._app_id = app_id
                cb._scheduler = self._scheduler
                cb._channel_registry = self._channel_registry
                self._llm_channel.register_context_builder(app_id, cb)
                self._scheduler.register_app_executor(app_id, cb)
                self._register_wake_handler(app_id)
            except Exception as exc:
                logger.warning("app_service_wiring_failed app=%s: %s", app_id, exc, exc_info=True)

        app_modules = agent_result.get("modules", {})
        for name in compiled.channels:
            try:
                if app_modules:
                    self._channel_registry.set_resolver_modules(name, app_modules)
                self._channel_registry.set_resolver_user_store(name, self._user_store)
            except Exception as exc:
                logger.warning("channel_resolver_failed app=%s channel=%s: %s", app_id, name, exc)

        mcp_module = app_modules.get("mcp")
        if mcp_module is not None:
            try:
                mcp_module._user_store = self._user_store
                mcp_module._app_id = app_id
                if hasattr(self, "_daemon_mcp_pool") and self._daemon_mcp_pool is not None:
                    mcp_module._daemon_pool = self._daemon_mcp_pool

                mcp_config = compiled.modules.get("mcp")
                if mcp_config is not None and mcp_config.config:
                    try:
                        await mcp_module.on_config_update(mcp_config.config)
                    except Exception as exc:
                        logger.warning(
                            "mcp_reconnect_after_inject app=%s: %s", app_id, exc, exc_info=True,
                        )

                try:
                    await mcp_module._preload_oauth_tokens()
                except Exception as exc:
                    logger.warning(
                        "mcp_oauth_preload_failed app=%s: %s", app_id, exc, exc_info=True,
                    )
            except Exception as exc:
                logger.warning("mcp_setup_failed app=%s: %s", app_id, exc, exc_info=True)

            if cb is not None:
                try:
                    old_count = cb.index.total_tools if cb.index else 0
                    security_profile = getattr(compiled, "security_profile", None)
                    new_index = cb.build_and_set_index(app_modules, security_profile)
                    new_count = new_index.total_tools if new_index else 0
                    if new_count > old_count:
                        self._refresh_agent_tools(
                            compiled, agent_result, cb, new_index,
                        )
                        logger.info(
                            "tool_index_rebuilt_after_preload app=%s tools=%d→%d",
                            app_id, old_count, new_count,
                        )
                except Exception as exc:
                    logger.warning("tool_index_rebuild_failed app=%s: %s", app_id, exc, exc_info=True)

        deployed = DeployedApp(
            app_id=app_id,
            compiled=compiled,
            contexts=agent_result["contexts"],
            modules=agent_result["modules"],
            context_builder=cb,
            bootstrap_result=None,
            hook_runner=agent_result.get("hook_runner"),
            approval_queue=agent_result.get("approval_queue"),
            scope=scope,
            owner_user_id=owner_user_id,
        )
        # Stash the daemon's event bus on the agent context so runtime
        # paths that don't have access to a FastAPI Request (background
        # activations, cron triggers, channel dispatches) can still emit
        # session-scoped events - notably the ``error`` event on turn
        # failure, which otherwise stayed in the activation table and
        # never reached the client's SSE stream.
        try:
            for _agent_ctx in agent_result["contexts"].values():
                setattr(_agent_ctx, "event_bus", self.event_bus)
        except Exception:
            logger.debug("event_bus_attach_to_context_failed", exc_info=True)
        deployed_key = self._deployed_key(
            app_id, scope=scope, owner_user_id=owner_user_id,
        )
        self._deployed[deployed_key] = deployed

        if deployed.approval_queue is not None:
            try:
                deployed.approval_queue._app_id = app_id
                deployed.approval_queue.add_on_request(
                    self._make_approval_publisher(app_id),
                )
                deployed.approval_queue.add_on_resolve(
                    self._approval_resolve_publisher(app_id),
                )
            except Exception as exc:
                logger.warning(
                    "approval_publisher_wire_failed app=%s: %s", app_id, exc,
                )

        # ── Wire Socket.IO bus into preview & widget modules ──
        # So their _publish() also emits events to Socket.IO rooms,
        # enabling Flutter clients to receive preview/widget events
        # without opening a separate SSE connection.
        for mod_name in ("preview", "widget"):
            mod = app_modules.get(mod_name)
            if mod is not None and hasattr(mod, "_event_bus"):
                mod._event_bus = self.event_bus
                mod._bus_app_id = app_id
                logger.info(
                    "bus_wired module=%s app=%s sio=%s",
                    mod_name, app_id, self.event_bus._sio is not None,
                )

        # ── Wire bg notification relay for real-time SSE updates ──
        # The context_builder fires this on every push_module_notification
        # so the frontend sees bg_task_update and memory_update events
        # immediately without polling.
        if cb is not None and hasattr(cb, "_on_notification_relay"):
            _bus = self.event_bus
            _aid = app_id

            # Map internal memory event types to frontend action names
            _MEMORY_EVENT_MAP = {
                "todo_added": "add_todo",
                "todo_updated": "update_todo",
                "goal_set": "set_goal",
                "fact_added": "remember",
                "fact_removed": "forget",
            }

            # Map internal agent event types to frontend action names
            _AGENT_EVENT_MAP = {
                "agent_spawn": "spawn_agent",
                "agent_progress": "agent_progress",
                "agent_completed": "agent_result",
                "agent_failed": "agent_result",
                "agent_timeout": "agent_result",
                "agent_cancelled": "agent_result",
                "agent_cancel": "agent_cancel",
                "agent_retrying": "agent_progress",
            }

            def _relay(session_id: str, notification: dict) -> None:
                try:
                    import asyncio as _aio
                    loop = _aio.get_running_loop()
                    bus_key = _bus.session_key(_aid, session_id)
                except RuntimeError:
                    return  # No event loop - standalone CLI mode

                # Route by type first - agent events have "type" starting with "agent_"
                event_type = notification.get("type", "")

                # Background task events (shell, context_builder bg tasks)
                # Discriminate: bg tasks have task_id+tool_name, agent events have agent_id
                status = notification.get("status")
                from digitorn.core.events.envelope import (
                    SessionEvent, OpType, OpState, gen_op_id,
                )
                _uid_for_event = notification.get("user_id") or "system"
                if (
                    status in ("progress", "completed", "failed", "cancelled")
                    and not event_type.startswith("agent_")
                    and notification.get("task_id")
                ):
                    task_id = notification.get("task_id") or gen_op_id("bg")
                    _state_map = {
                        "progress": OpState.RUNNING,
                        "completed": OpState.COMPLETED,
                        "failed": OpState.FAILED,
                        "cancelled": OpState.CANCELLED,
                    }
                    loop.create_task(_bus.emit(SessionEvent.build(
                        type="bg_task_update",
                        app_id=_aid,
                        session_id=session_id,
                        user_id=_uid_for_event,
                        op_id=task_id,
                        op_type=OpType.TOOL,
                        op_state=_state_map[status],
                        payload={
                            "task_id": task_id,
                            "tool_name": notification.get("tool_name"),
                            "status": status,
                            "elapsed_seconds": notification.get("elapsed_seconds", 0),
                            "result_preview": notification.get("result_preview", "")[:500],
                            "hint": notification.get("hint", ""),
                            "error": notification.get("error", ""),
                        },
                    )))
                    return
                agent_action = _AGENT_EVENT_MAP.get(event_type)
                if agent_action is not None:
                    agent_payload = dict(notification)
                    agent_payload["action"] = agent_action
                    agent_payload.pop("type", None)
                    # op_id = the spawned agent's id so every event of
                    # ONE sub-agent (spawn → progress* → result) lands
                    # under one op_id on the client. op_parent_id is
                    # the coordinator that spawned it, allowing the
                    # client to draw the parent→child tree.
                    agent_id = (
                        notification.get("agent_id")
                        or gen_op_id("agent")
                    )
                    parent_agent = notification.get("parent_agent")
                    # Map the internal status → contract OpState.
                    _agent_state_map = {
                        "agent_spawn": OpState.RUNNING,
                        "agent_progress": OpState.RUNNING,
                        "agent_retrying": OpState.RUNNING,
                        "agent_completed": OpState.COMPLETED,
                        "agent_failed": OpState.FAILED,
                        "agent_timeout": OpState.TIMEOUT,
                        "agent_cancelled": OpState.CANCELLED,
                        "agent_cancel": OpState.CANCELLED,
                    }
                    op_state = _agent_state_map.get(event_type, OpState.RUNNING)
                    agent_payload["op_id"] = agent_id
                    loop.create_task(_bus.emit(SessionEvent.build(
                        type="agent_event",
                        app_id=_aid,
                        session_id=session_id,
                        user_id=_uid_for_event,
                        op_id=agent_id,
                        op_type=OpType.AGENT,
                        op_state=op_state,
                        op_parent_id=parent_agent if isinstance(parent_agent, str) else None,
                        payload=agent_payload,
                    )))
                    return

                # Memory events (todos, goal, facts)
                frontend_action = _MEMORY_EVENT_MAP.get(event_type)
                if frontend_action is not None:
                    payload: dict = {"action": frontend_action}
                    if event_type in ("todo_added", "todo_updated"):
                        payload["result"] = {
                            "todos": notification.get("todos", []),
                            "todo": notification.get("todo"),
                            "goal": notification.get("goal", ""),
                            "progress": notification.get("progress", {}),
                        }
                    elif event_type == "goal_set":
                        payload["result"] = {"goal": notification.get("goal", "")}
                    elif event_type == "fact_added":
                        payload["result"] = {
                            "id": notification.get("id"),
                            "content": notification.get("content"),
                        }
                    elif event_type == "fact_removed":
                        payload["result"] = {"id": notification.get("id")}
                    loop.create_task(_bus.publish(bus_key, {
                        "type": "memory_update",
                        "data": payload,
                    }))

            cb._on_notification_relay = _relay

        # ── Hot reload (dev only) ─────────────────────────────
        # When enabled, watch the bundle's prompts/skills/assets
        # dirs and auto-redeploy on changes. Default off.
        try:
            settings = getattr(self, "_settings", None)
            hot_reload_enabled = bool(
                settings and getattr(settings.app, "hot_reload", False)
            )
        except Exception:
            hot_reload_enabled = False
        if hot_reload_enabled and compiled.source_path:
            try:
                from pathlib import Path
                from digitorn.core.app.hot_reload import BundleHotReloader
                bundle_dir = Path(compiled.source_path).parent
                async def _on_reload():
                    try:
                        await self.redeploy(app_id)
                    except Exception as exc:
                        logger.warning(
                            "hot_reload redeploy failed app=%s: %s",
                            app_id, exc,
                        )
                reloader = BundleHotReloader(
                    app_id=app_id,
                    bundle_dir=bundle_dir,
                    on_change=_on_reload,
                )
                await reloader.start()
                deployed.hot_reloader = reloader
            except Exception as exc:
                logger.debug(
                    "hot_reload start skipped for %s: %s", app_id, exc,
                )

        restored = 0
        if cb is not None and hasattr(cb, "restore_watchers"):
            try:
                restored = await cb.restore_watchers(app_id)
            except Exception as exc:
                logger.warning("watcher_restore_failed app=%s: %s", app_id, exc, exc_info=True)

        if compiled.execution.scheduler and not self._scheduler._running:
            try:
                await self._scheduler.start()
            except Exception as exc:
                logger.warning("scheduler_start_failed: %s", exc, exc_info=True)

        if self._should_sandbox(compiled):
            if self._should_use_pool(compiled):
                pool = await self._deploy_pool(compiled, agent_result)
                if pool is not None:
                    deployed.sandbox_pool = pool
            else:
                worker = await self._deploy_sandboxed(compiled, agent_result)
                if worker is not None:
                    deployed.sandbox_worker = worker

        sandbox_mode = "pool" if deployed.sandbox_pool else ("worker" if deployed.sandbox_worker else "none")
        logger.info(
            "App '%s' deployed: %d agents, %d tools, sandbox=%s",
            app_id,
            len(deployed.contexts),
            deployed.index.total_tools if deployed.index else 0,
            sandbox_mode,
        )

        # ── Preview dev server (deferred warm-up) ───────────────────
        # Apps with a ``preview:`` block get a PreviewManager that
        # supervises the dev server (e.g. ``npm run dev``). The
        # reverse-proxy route in api/apps.py serves traffic via
        # /api/apps/{id}/preview-server/proxy/*.
        #
        # IMPORTANT: warm-up runs in a **background task** - the first
        # ``npm install`` can take 30-90s, and we must NOT block the
        # FastAPI lifespan startup (or every daemon reboot freezes the
        # whole API for a minute while packages download). The
        # ``/preview-server/status`` route surfaces the install/start
        # progress to clients, and the PreviewManager state machine
        # already handles every transition (installing → starting →
        # running / crashed) cleanly.
        preview_cfg = getattr(compiled, "preview", None)
        if preview_cfg is not None and getattr(preview_cfg, "enabled", False):
            try:
                from digitorn.core.preview import PreviewManager
                from pathlib import Path
                bundle_dir = (
                    Path(compiled.source_path).parent
                    if compiled.source_path
                    else Path.cwd()
                )
                pm = PreviewManager(
                    preview_cfg,
                    bundle_dir=bundle_dir,
                    app_id=app_id,
                    owner_user_id=getattr(deployed, "owner_user_id", None),
                )
                # Attach to deployed BEFORE spawning the background
                # task so /preview-server/status answers immediately
                # with state="installing" or "starting" while warm-up
                # is in flight.
                deployed.preview_manager = pm

                async def _warmup(pm_ref=pm, aid=app_id, port=preview_cfg.port):
                    try:
                        await pm_ref.install()
                        await pm_ref.start()
                        logger.info(
                            "preview_deployed app=%s port=%d (background)",
                            aid, port,
                        )
                    except Exception as exc:
                        logger.warning(
                            "preview_warmup_failed app=%s: %s", aid, exc,
                            exc_info=True,
                        )

                asyncio.create_task(_warmup())
                logger.info(
                    "preview_warmup_scheduled app=%s port=%d",
                    app_id, preview_cfg.port,
                )
            except Exception as exc:
                logger.warning(
                    "preview_deploy_failed app=%s: %s", app_id, exc,
                    exc_info=True,
                )

        # Auto-start background mode apps - triggers start listening immediately.
        # Keep a strong reference to the task in self._bg_start_tasks to prevent
        # Python's GC from collecting the pending coroutine (which produces
        # "Task was destroyed but it is pending!" warnings at startup).
        if compiled.execution.mode == "background":
            _bg_task = asyncio.create_task(
                self._auto_start_background(deployed, compiled)
            )
            self._bg_start_tasks.add(_bg_task)
            _bg_task.add_done_callback(self._bg_start_tasks.discard)

        return deployed

    async def _auto_start_background(self, deployed: Any, compiled: Any) -> None:
        """Auto-start a background mode app after deployment.

        Launches trigger listeners (cron, watch, http) or channels module
        listeners. Runs indefinitely until the app is undeployed.

        IMPORTANT: we MUST pass ``runtime_app=deployed`` to
        ``run_background`` so it can locate the ``channels`` module and
        call ``start_listeners()``. Without this, apps that declare their
        triggers under ``modules.channels.config.providers`` (every new
        background app does) never activate - the cron tick stays at
        "ready" and never fires. ``DeployedApp`` has the same ``.modules``
        shape that ``run_background`` expects, so duck typing works.
        """
        import copy
        from digitorn.core.runtime.types import apply_workspace_override

        app_id = compiled.app_id
        try:
            from digitorn.core.runtime.modes.background import run_background

            # Create a proper context copy with session and workspace
            ctx = copy.copy(deployed.entry_context)
            ctx.session_id = f"background-{app_id}"
            ctx.app_id = app_id

            yaml_ws = getattr(compiled.execution, "workspace", "")
            ws = yaml_ws or str(Path.cwd())
            apply_workspace_override(ctx, ws, yaml_ws)

            triggers = compiled.execution.triggers or []

            logger.info(
                "background_auto_start app=%s triggers=%d channels_module=%s",
                app_id, len(triggers),
                "yes" if "channels" in deployed.modules else "no",
            )

            # Wire hook_runner onto the channels module the same way
            # RuntimeApp._wire_channels_module does it, so the pipeline
            # has a reference for agent_turn activations.
            channels_mod = deployed.modules.get("channels")
            if channels_mod is not None:
                try:
                    channels_mod._runtime_app = deployed  # type: ignore[attr-defined]
                    channels_mod._hook_runner = deployed.hook_runner  # type: ignore[attr-defined]
                except Exception:
                    pass

            await run_background(
                ctx,
                triggers=[t for t in triggers],
                max_turns=compiled.execution.max_turns,
                timeout=compiled.execution.timeout,
                app_id=app_id,
                max_concurrent_activations=compiled.execution.max_concurrent_activations,
                runtime_app=deployed,
            )
        except asyncio.CancelledError:
            logger.info("background_app_stopped app=%s", app_id)
        except Exception as exc:
            logger.error("background_auto_start_failed app=%s: %s", app_id, exc, exc_info=True)

    async def _deploy_sandboxed(
        self,
        compiled: CompiledApp,
        bootstrap_result: dict[str, Any],
    ) -> "SandboxWorker | None":
        """Create a sandbox worker for OS-isolated tool execution (standard level).

        The worker only loads modules that touch the OS (filesystem, shell,
        database). The daemon still runs agent_turn and the LLM - the worker
        is just an execution backend for tool calls.
        """
        from digitorn.core.sandbox.worker import SandboxWorker
        from digitorn.core.sandbox.builder import build_sandbox_profile

        app_id = compiled.app_id
        sandboxed_modules = self._get_sandboxed_modules(compiled)
        if not sandboxed_modules:
            return None

        workspace = getattr(compiled.execution, "workspace", "") or ""
        profile = build_sandbox_profile(compiled, workspace_override=workspace or None)

        worker = SandboxWorker(
            app_id=app_id,
            module_ids=sandboxed_modules,
            workspace=workspace,
            allowed_paths=list(profile.writable_paths | profile.readable_paths),
            sandbox_config={
                "allow_exec": profile.allow_exec,
                "allow_fork": profile.allow_fork,
                "allow_network": profile.allow_network,
                "hardening": True,
            },
        )

        try:
            await worker.start()
        except Exception as exc:
            logger.warning("sandbox_worker_failed app=%s: %s", app_id, exc)
            return None

        return worker

    async def _deploy_pool(
        self,
        compiled: CompiledApp,
        bootstrap_result: dict[str, Any],
    ) -> "WorkerPool | None":
        """Create a WorkerPool for per-session OS isolation (strict/maximum level).

        Each session gets its own Landlock sandbox, applied on-demand from a
        warm worker. Workers are recycled after sessions end.

        Resource efficiency for 1000 apps:
        - pool_size=0 by default → no warm workers until first session
        - idle_timeout=60s → workers killed quickly after session ends
        - workspace affinity → sessions sharing workspace share a worker
        - pool_max caps total workers per app
        """
        from digitorn.core.sandbox.pool import WorkerPool

        app_id = compiled.app_id
        sandboxed_modules = self._get_sandboxed_modules(compiled)
        if not sandboxed_modules:
            return None

        sandbox_cfg = self._get_sandbox_config(compiled)
        level = getattr(sandbox_cfg, "level", "strict") if sandbox_cfg else "strict"

        # Resource-efficient defaults for scale
        pool_size = getattr(sandbox_cfg, "pool_size", 0) if sandbox_cfg else 0
        pool_max = getattr(sandbox_cfg, "pool_max", 4) if sandbox_cfg else 4

        # Namespaces based on level
        namespaces: set[str] = set()
        if sandbox_cfg and hasattr(sandbox_cfg, "namespaces"):
            namespaces = set(sandbox_cfg.namespaces)
        elif level == "strict":
            namespaces = {"user", "pid"}
        elif level == "maximum":
            namespaces = {"user", "pid", "net"}

        # Hardening config
        hardening = {"enabled": True, "drop_caps": True, "mdwe": True, "no_dumpable": True}

        # Audit for maximum level only
        audit = level == "maximum"
        workspace_snapshot = getattr(sandbox_cfg, "workspace_snapshot", False) if sandbox_cfg else False

        pool = WorkerPool(
            compiled=compiled,
            app_id=app_id,
            pool_size=pool_size,
            pool_max=pool_max,
            namespaces=namespaces,
            hardening=hardening,
            audit=audit,
            workspace_snapshot=workspace_snapshot,
        )

        try:
            await pool.start()
        except Exception as exc:
            logger.warning("sandbox_pool_failed app=%s: %s", app_id, exc)
            return None

        logger.info(
            "sandbox_pool_deployed app=%s level=%s pool_size=%d pool_max=%d ns=%s audit=%s",
            app_id, level, pool_size, pool_max, namespaces, audit,
        )
        return pool

    @staticmethod
    def _get_sandboxed_modules(compiled: CompiledApp) -> list[str]:
        """Get modules that should run in the sandbox."""
        sandboxed = []
        for mid in compiled.module_ids:
            if mid in ("filesystem", "shell", "database", "git", "notebook"):
                sandboxed.append(mid)
        return sandboxed

    @staticmethod
    def _refresh_agent_tools(
        compiled: CompiledApp,
        agent_result: dict[str, Any],
        cb: Any,
        new_index: Any,
    ) -> None:
        """Rebuild agent tool lists after the tool index changed.

        When MCP servers connect post-bootstrap (e.g. after OAuth token
        preload), the index gains new tools.  This method updates each
        AgentContext.tools so the LLM sees them on the next turn.
        """
        from digitorn.modules.context_builder.builder import build_direct_tools
        from digitorn.modules.context_builder.prompt import build_system_prompt
        from digitorn.core.runtime.bootstrap import (
            _build_meta_tools_schema,
            _build_primitive_tools_schema,
            _choose_tool_injection,
        )

        direct_tools = build_direct_tools(new_index)
        meta_tools = _build_meta_tools_schema(cb)

        contexts: dict[str, AgentContext] = agent_result["contexts"]
        for agent_id, ctx in contexts.items():
            tool_injection = _choose_tool_injection(
                total_tools=new_index.total_tools,
                context_window=ctx.context_config.max_tokens,
                direct_tools=direct_tools,
            )

            if tool_injection == "direct":
                primitive_tools = _build_primitive_tools_schema(
                    cb,
                    watchers_enabled=ctx.watchers_enabled,
                    scheduler_enabled=compiled.execution.scheduler,
                    channels_enabled=bool(compiled.channels),
                )
                agent_tools = direct_tools + primitive_tools
            else:
                agent_tools = meta_tools

            ctx.tools = agent_tools
            ctx.tool_injection = tool_injection

            agent_def = next(
                (a for a in compiled.agents if a.agent_id == agent_id), None,
            )
            if agent_def is not None:
                ctx.system_prompt = build_system_prompt(
                    agent_id=agent_id,
                    role=ctx.role,
                    user_prompt=agent_def.system_prompt,
                    index=new_index,
                    native_tool_use=ctx.native_tool_use,
                    tool_injection=tool_injection,
                    tools=agent_tools,
                    plan_first=ctx.plan_first,
                    setup_summary=ctx.setup_summary,
                    channels_info=ctx.channels_info,
                    default_channel=ctx.default_channel,
                )

            logger.debug(
                "agent_tools_refreshed agent=%s mode=%s tools=%d",
                agent_id, tool_injection, len(agent_tools),
            )

    async def _on_mcp_event(
        self, event: "MCPServerEvent", server_id: str,
    ) -> None:
        """Called by daemon pool when a server changes state.

        Handles:
        - CONNECTED: rebuild tool index (tools may have changed)
        - DISCONNECTED: rebuild tool index (tools removed)
        - CONFIG_UPDATED: reconnect server with new config, then rebuild
        """
        from digitorn.core.mcp_pool import MCPServerEvent

        if event == MCPServerEvent.CONFIG_UPDATED:
            await self._handle_mcp_config_updated(server_id)
            return

        # Snapshot to avoid "dict changed during iteration" if undeploy races
        for app_id, deployed in list(self._deployed.items()):
            mcp_module = deployed.modules.get("mcp")
            if mcp_module is None:
                continue
            if server_id not in getattr(mcp_module, "_daemon_server_ids", set()):
                continue
            self._rebuild_app_tool_index(app_id, deployed, server_id, event.value)

    async def _handle_mcp_config_updated(self, server_id: str) -> None:
        """Reconnect a daemon-managed server after its config changed in DB."""
        pool = getattr(self, "_daemon_mcp_pool", None)
        if pool is None:
            return

        entry = pool.get_server(server_id)
        if entry is None:
            return

        try:
            from digitorn.core.mcp_store import (
                get_server as db_get_server,
                _build_connect_kwargs,
            )

            async with pool._session_factory() as session:
                server = await db_get_server(session, server_id)
            if server is None:
                return

            kwargs = _build_connect_kwargs(server)
            await pool._pool.disconnect(server_id)
            await pool._pool.connect(server_id, server.transport, **kwargs)
            logger.info("mcp_config_reconnect_ok server=%s", server_id)

            # Snapshot to avoid "dict changed during iteration" if undeploy races
            for app_id, deployed in list(self._deployed.items()):
                mcp_module = deployed.modules.get("mcp")
                if mcp_module is None:
                    continue
                if server_id not in getattr(mcp_module, "_daemon_server_ids", set()):
                    continue
                self._rebuild_app_tool_index(app_id, deployed, server_id, "config_updated")

        except Exception as exc:
            logger.error("mcp_config_reconnect_fail server=%s: %s", server_id, exc, exc_info=True)

    def _rebuild_app_tool_index(
        self,
        app_id: str,
        deployed: DeployedApp,
        server_id: str,
        reason: str,
    ) -> None:
        """Rebuild tool index for a single deployed app."""
        cb = deployed.context_builder
        if cb is None:
            return

        old_count = cb.index.total_tools if cb.index else 0
        security_profile = getattr(deployed.compiled, "security_profile", None)
        new_index = cb.build_and_set_index(deployed.modules, security_profile)
        new_count = new_index.total_tools if new_index else 0

        if new_count != old_count:
            self._refresh_agent_tools(
                deployed.compiled,
                {"contexts": deployed.contexts},
                cb,
                new_index,
            )
            logger.info(
                "tool_index_rebuilt app=%s server=%s reason=%s tools=%d→%d",
                app_id, server_id, reason, old_count, new_count,
            )

    def _get_deployed(
        self,
        app_id: str,
        user_id: str | None = None,
    ) -> DeployedApp:
        """Get a deployed app or raise - scope-aware.

        Resolves via the public ``get(app_id, user_id=...)`` which
        walks user-scoped → system-scoped → legacy bare key. Callers
        should pass ``user_id`` whenever they have one so a user's
        private deploy shadows the system one.
        """
        deployed = self.get(app_id, user_id=user_id)
        if deployed is None:
            available = list(self._deployed.keys())
            raise RuntimeError(
                f"App '{app_id}' not deployed (available: {available})"
            )
        return deployed

"""Module layer - BaseModule interface.

Every module - built-in or community - must subclass ``BaseModule``.

Architectural principles enforced here:
  - **DRY**: ``@action`` decorator is the single declaration point for
    handler, spec, and params - no parallel re-declarations.
  - **Typed**: ``ActionResult[T]`` is generic; ``ExecutionContext`` is a
    frozen, typed dataclass - plain ``dict`` is never acceptable.
  - **Platform-safe**: ``Platform`` uses ``StrEnum`` so values serialize
    without custom encoders and compare directly with strings.
  - **Fail-fast**: ``_check_dependencies()`` runs in ``__init__``, so a
    module with missing dependencies never enters the registry.
  - **Stateless by default**: modules track no mutable state between calls
    unless they explicitly manage sessions or caches.
  - All errors are raised as ``ActionExecutionError`` or a subclass.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import logging
import platform as _platform_module
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Optional L2 cache backend ────────────────────────────────────────
# ``digitorn.cache`` is a deferred / optional package - when it's not
# installed, every cacheable action would otherwise pay the cost of a
# failed ``import`` on every call PLUS a DEBUG log line. We resolve the
# imports once here; ``_cache_unavailable=True`` short-circuits all the
# call-site try/imports below. The ``modules/__init__.py`` decorator
# fallback already keeps ``@cacheable`` / ``@invalidates_cache`` valid
# (no-op) at module load.
_cache_get_client: Any = None
_cache_make_key: Any = None
_cache_make_invalidation_patterns: Any = None
_cache_unavailable = False
try:
    from digitorn.cache.client import (  # type: ignore[import-not-found]
        get_cache_client as _cache_get_client,
    )
    from digitorn.cache.decorators import (  # type: ignore[import-not-found]
        make_cache_key as _cache_make_key,
        make_invalidation_patterns as _cache_make_invalidation_patterns,
    )
except ImportError:
    _cache_unavailable = True


from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Generic, TypeVar

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from digitorn.modules.service_bus import ServiceBus

from digitorn.modules.exceptions import (
    ActionExecutionError,
    ActionNotFoundError,
    ApprovalRequiredError,
    PermissionDeniedError,
    PermissionNotGrantedError,
    PolicyViolationError,
    RateLimitExceededError,
)
from digitorn.modules.manifest import ActionSpec, ModuleManifest
from digitorn.modules.protocol import IModule


async def _invoke_handler_async(handler: Any, params: Any) -> Any:
    """Universal action dispatch entry point - the no-block contract.

    Every action handler routes through here: native ``@action`` methods,
    dynamically registered tools, MCP tool wrappers, community plugins,
    anything in the future. The contract: the call NEVER blocks the loop.

    - **Async handler** (``async def`` or anything ``iscoroutinefunction``
      reports True for): awaited directly. Any sync I/O inside the
      coroutine is the author's responsibility - it will be flagged by
      the loop-block watchdog in ``tool_exec.execute_tool`` and surface
      as a WARNING with the tool name (look for
      ``tool_blocked_event_loop``).
    - **Sync handler** (plain ``def``): dispatched via
      ``asyncio.to_thread`` automatically. This is what makes the future
      MCP marketplace safe - a community module can declare a plain
      ``def my_tool(params)`` and we still won't stall the loop.

    The decorator-bound ``_bound_async`` / ``_bound_sync`` closures
    produced by ``BaseModule._get_handler`` preserve their async/sync
    nature, so ``iscoroutinefunction`` correctly distinguishes them.
    """
    if inspect.iscoroutinefunction(handler):
        return await handler(params)
    return await asyncio.to_thread(handler, params)


def _collect_handler_cache_meta(handler: Any) -> dict[str, Any]:
    """Extract ``_cache_meta`` from a handler method, traversing wrappers."""
    fn = handler
    for _ in range(10):
        meta = getattr(fn, "_cache_meta", None)
        if meta is not None:
            return dict(meta)
        fn = getattr(fn, "__wrapped__", None)
        if fn is None:
            break
    return {}


def _auto_coerce_params(params: dict, params_model: Any) -> dict:
    """Auto-coerce common LLM parameter mistakes before Pydantic validation.

    Fixes:
    - String integers → int ("40" → 40)
    - String booleans → bool ("true" → True, "false" → False)
    - String floats → float ("1.5" → 1.5)
    - Wrong parameter names → closest match (e.g. "text" → "content")
    """
    if not isinstance(params, dict) or params_model is None:
        return params

    try:
        schema = params_model.model_json_schema()
    except Exception:
        return params

    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    coerced = dict(params)

    # Fix wrong parameter names - map to closest valid name by similarity
    valid_names = set(props.keys())
    unknown_keys = [k for k in coerced if k not in valid_names and not k.startswith("_")]
    if unknown_keys and required:
        missing = list(required - set(coerced.keys()))
        if missing and len(unknown_keys) <= len(missing):
            from difflib import SequenceMatcher
            used_missing: set[str] = set()
            for unk in unknown_keys:
                best, best_score = "", 0.0
                for miss in missing:
                    if miss in used_missing:
                        continue
                    score = SequenceMatcher(None, unk.lower(), miss.lower()).ratio()
                    if score > best_score:
                        best, best_score = miss, score
                # Only remap if similarity is above 0.4 (avoids nonsense mappings)
                if best and best_score >= 0.4:
                    coerced[best] = coerced.pop(unk)
                    used_missing.add(best)

    # Fix type mismatches
    for name, prop in props.items():
        if name not in coerced:
            continue
        val = coerced[name]
        ptype = prop.get("type", "")

        # Handle anyOf (optional fields like "string | null")
        if not ptype and "anyOf" in prop:
            for opt in prop["anyOf"]:
                if opt.get("type") and opt["type"] != "null":
                    ptype = opt["type"]
                    break

        if ptype == "integer" and isinstance(val, str):
            try:
                coerced[name] = int(val)
            except ValueError:
                # Try extracting number from string like "line 40"
                import re
                m = re.search(r'\d+', val)
                if m:
                    coerced[name] = int(m.group())

        elif ptype == "number" and isinstance(val, str):
            try:
                coerced[name] = float(val)
            except ValueError:
                pass

        elif ptype == "boolean" and isinstance(val, str):
            coerced[name] = val.lower() in ("true", "1", "yes", "on")

        elif ptype == "boolean" and isinstance(val, int):
            coerced[name] = bool(val)

    return coerced


def _format_validation_error(ve: Any, action_name: str, params_model: Any = None) -> str:
    """Format a Pydantic ValidationError into a clear, agent-friendly message.

    Shows the exact JSON the model should send, not just parameter names.
    This is critical for weaker models that struggle with tool-use correction.
    """
    lines = [f"ERROR: Invalid parameters for '{action_name}'."]
    for err in ve.errors():
        loc = ".".join(str(part) for part in err["loc"])
        lines.append(f"  - {loc}: {err['msg']}")

    if params_model is not None:
        try:
            schema = params_model.model_json_schema()
            props = schema.get("properties", {})
            required = schema.get("required", [])
            if props:
                # Build a concrete JSON example
                example = {}
                for name, prop in props.items():
                    if name in required:
                        ptype = prop.get("type", "string")
                        if ptype == "string":
                            example[name] = "your value here"
                        elif ptype == "array":
                            example[name] = []
                        elif ptype == "integer":
                            example[name] = 0
                        elif ptype == "number":
                            example[name] = 0.0
                        elif ptype == "boolean":
                            example[name] = True
                        else:
                            example[name] = "..."
                if example:
                    import json as _json
                    lines.append(f"  You MUST send these parameters as JSON:")
                    lines.append(f"  {_json.dumps(example, ensure_ascii=False)}")
                    lines.append(f"  DO NOT omit required fields. Retry with the correct format.")
        except Exception as exc:
            logger.debug("Schema introspection failed for '%s': %s", action_name, exc)

    return "\n".join(lines)


class Platform(StrEnum):
    """Supported operating system platforms.

    Uses ``StrEnum`` so ``Platform.LINUX == "linux"`` is ``True``,
    enabling direct comparison against config strings without custom encoders.
    """

    ALL          = "all"
    LINUX        = "linux"
    WINDOWS      = "windows"
    MACOS        = "macos"
    RASPBERRY_PI = "raspberry_pi"


_T = TypeVar("_T")


@dataclass(frozen=True)
class ExecutionContext:
    """Immutable, typed gateway of runtime services passed to every action.

    Context concerns (service bus, stream, user profile) are *separated* from
    business parameters.  Actions that need system services declare ``ctx:
    ExecutionContext`` as an explicit parameter - never via ``params``.

    Attributes:
        plan_id:      The IML plan being executed.
        action_id:    The specific action step within the plan.
        service_bus:  Inter-module call gateway (optional - None in unit tests).
        stream:       Live progress stream (None if action does not stream).
        session_id:   Optional session token for stateful modules.
        watcher_service: Source watcher service (optional - None in unit tests).
        metadata:     Arbitrary key-value bag for tracing / middleware.
    """

    plan_id: str
    action_id: str
    service_bus: "ServiceBus | None" = field(default=None, compare=False)
    stream: Any | None = field(default=None, compare=False)
    session_id: str | None = None
    user_id: str = "admin"
    workspace: str | None = None
    security_profile: Any | None = field(default=None, compare=False)
    policy_enforcer: Any | None = field(default=None, compare=False)
    watcher_service: Any | None = field(default=None, compare=False)
    user: Any | None = field(default=None, compare=False)
    constraints: dict[str, Any] = field(default_factory=dict, compare=False)
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)
    approval_queue: Any | None = field(default=None, compare=False)


@dataclass
class ActionResult(Generic[_T]):
    """Generic, structured result envelope returned by every module action.

    Using ``ActionResult[T]`` instead of a bare ``dict`` ensures callers know
    the exact shape of the data they are consuming.  The generic ``data``
    field should be a Pydantic model for full type safety.

    Attributes:
        success:     ``True`` when the action completed without error.
        data:        The typed action output (set on success).
        error:       Human-readable error message (set on failure).
        metadata:    Internal bookkeeping (timing, cache hits, etc.).
    """

    success: bool
    data: _T | None = None
    output: Any = field(default=None, repr=False)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # TEMP DEBUG: capture stack of suspicious empty-error failure
        # ActionResult(success=False, error='' or None, data=None) - this is
        # the exact shape leaking into Bash tool_call events with no diagnostic.
        if (
            self.success is False
            and (self.error is None or self.error == "")
            and self.data is None
        ):
            try:
                import traceback as _tb
                from pathlib import Path as _P
                _logp = _P.home() / ".digitorn" / "logs" / "empty_failure_trace.log"
                _logp.parent.mkdir(parents=True, exist_ok=True)
                with open(_logp, "a", encoding="utf-8") as _f:
                    _f.write(
                        "=" * 70 + "\n"
                        f"ActionResult(success=False, error={self.error!r}, data=None)\n"
                        + "".join(_tb.format_stack()[-12:-1])
                        + "\n"
                    )
            except Exception:
                pass

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data if self.data is not None else self.output,
            "error": self.error,
            "metadata": self.metadata,
        }


@dataclass
class ResourceEstimate:
    """Pre-execution cost estimation for an action.

    Used by the executor to schedule actions intelligently (e.g. avoid
    running multiple GPU-heavy vision parses simultaneously).

    The defaults are conservative non-zero values so the scheduler has
    a meaningful baseline even when a module does not override
    ``estimate_cost()``.  Override with measured values for precision.
    """

    estimated_duration_seconds: float = 1.0
    estimated_memory_mb: float = 10.0
    estimated_cpu_percent: float = 10.0
    estimated_io_operations: int = 0
    confidence: float = 0.5


@dataclass
class ModulePolicy:
    """Runtime policy constraints declared by a module.

    The PolicyEnforcer checks these constraints before dispatching
    actions to the module.
    """

    max_parallel_calls: int = 0
    cooldown_seconds: float = 0.0
    allow_remote_invocation: bool = True
    execution_timeout: float = 0.0
    max_memory_mb: int = 0
    retry_on_failure: bool = False


class BaseModule(ABC, IModule):
    """Abstract base class for all Digitorn modules.

    Explicitly inherits from :class:`~digitorn.module.protocol.IModule` so
    that:

    - ``issubclass(MyModule, IModule)`` is ``True`` without needing an
      instance.
    - Static type checkers (mypy / pyright) verify all Protocol members are
      implemented at class-definition time.
    - The relationship between the contract and its primary implementation
      is explicit in the code, not just implicit by structural match.

    Subclasses must:
      1. Set ``MODULE_ID`` (snake_case, unique, e.g. ``"filesystem"``)
      2. Set ``VERSION`` (semver string, e.g. ``"1.0.0"``)
      3. Set ``SUPPORTED_PLATFORMS`` (list of ``Platform`` values)
      4. Implement :meth:`get_manifest` returning a ``ModuleManifest``
      5. Expose actions via ``@action``-decorated methods (recommended -
         single source of truth) **or** ``_action_<name>(params)`` methods
         (legacy convention)
      6. Optionally implement :meth:`_check_dependencies` to gate loading

    **DRY principle**: use the ``@action`` decorator to declare a handler
    alongside its ``ActionSpec`` in one place.  This eliminates the drift
    risk of maintaining a parallel list of specs in ``get_manifest()``.
    ``ModuleManifest.from_module(self)`` then auto-derives the manifest.

    The :meth:`execute` method is **not** abstract - it dispatches to
    the ``_action_registry`` dict first (O(1) ``@action`` path), then
    dynamic actions, then falls back to the ``_action_<name>`` naming
    convention.  Override only for fully custom dispatch logic.
    """

    MODULE_ID: str = ""
    VERSION: str = "0.0.0"
    SUPPORTED_PLATFORMS: list[Platform] = [Platform.ALL]
    MODULE_TYPE: str = "user"
    CONFIG_MODEL: type | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Auto-wrap ``get_manifest()`` to enrich with decorator metadata."""
        super().__init_subclass__(**kwargs)
        original = cls.__dict__.get("get_manifest")
        if original is not None and not getattr(original, "__isabstractmethod__", False):

            @functools.wraps(original)
            def _wrapped(self: "BaseModule", _orig: Any = original) -> "ModuleManifest":
                manifest = _orig(self)
                return self._enrich_manifest_metadata(manifest)

            cls.get_manifest = _wrapped  # type: ignore[assignment]

    # Per-task execution context - safe under asyncio concurrency
    _context_var: contextvars.ContextVar[ExecutionContext | None] = contextvars.ContextVar(
        "_module_exec_context", default=None,
    )

    def __init__(self) -> None:
        self._security: Any | None = None
        self._ctx: Any | None = None
        # Merge action registries from all classes in MRO (handles mixins)
        merged: dict[str, Any] = {}
        for cls in reversed(type(self).__mro__):
            merged.update(getattr(cls, "_action_registry", {}))
        self._action_registry: dict[str, Any] = merged
        self._dynamic_actions: dict[str, Callable[..., Any]] = {}
        self._dynamic_specs: dict[str, ActionSpec] = {}
        self._config: Any | None = None
        self._bg_notify: Callable[[dict[str, Any]], None] | None = None
        self._middleware_pipeline: Any | None = None
        self._workspace: str | None = None
        self._check_dependencies()

    @property
    def workspace(self) -> str | None:
        """Per-execution workspace, session-scoped.

        Reads from the current ``ExecutionContext`` first (set per-session
        in manager.py), then falls back to ``self._workspace`` (set once
        at bootstrap via ``on_config_update``).

        This ensures that when multiple sessions share a module instance,
        each action execution sees its own session's workspace.
        """
        ctx = self._context_var.get()
        if ctx is not None and ctx.workspace:
            return ctx.workspace
        return self._workspace

    @property
    def stream(self) -> Any | None:
        """Access the execution stream for real-time agent feedback.

        Available only during action execution. Returns None outside
        of an execute() call or when no event bus is configured.

        Usage in a module action::

            @action(description="Long running task")
            async def process(self, params):
                if self.stream:
                    await self.stream.progress(1, 3, "Starting...")
                if self.stream:
                    await self.stream.partial_result({"intermediate": data})
                if self.stream:
                    await self.stream.progress(3, 3, "Done")
                return ActionResult(success=True, data=result)
        """
        ctx = self._context_var.get()
        if ctx is not None:
            return ctx.stream
        return None

    @property
    def _context(self) -> ExecutionContext | None:
        """Current execution context (concurrency-safe via ContextVar)."""
        return self._context_var.get()

    @_context.setter
    def _context(self, value: ExecutionContext | None) -> None:
        """Set execution context (for tests and backward compat)."""
        self._context_var.set(value)

    def _notify_bg(self, notification: dict[str, Any]) -> None:
        """Push a background task notification for the agent.

        Called by modules when a long-running background task completes or
        fails (e.g. http.download, shell.background_run).  The notification
        is routed to the context_builder's queue and delivered to the LLM
        automatically - either before the next LLM call or proactively
        while waiting for user input.

        Args:
            notification: Dict with keys like task_id, tool_name, status,
                elapsed_seconds, result/error.
        """
        if self._bg_notify is not None:
            self._bg_notify(notification)

    def set_security(self, security: Any) -> None:
        """Inject the SecurityManager into this module.

        Called by the server startup after constructing the SecurityManager.
        Decorators on ``_action_*`` methods access it via ``self._security``.
        """
        self._security = security

    def _collect_security_metadata(self) -> dict[str, dict[str, Any]]:
        """Introspect decorated ``_action_*`` methods and return security metadata.

        Returns a dict keyed by action name (without the ``_action_`` prefix),
        with values from legacy ``collect_security_metadata``.  With the new
        ``@action`` decorator, metadata lives in ``_action_registry`` and this
        method is only needed for backward-compatible legacy modules.
        """
        try:
            from digitorn.security.decorators import collect_security_metadata
        except ImportError:
            return {}

        result: dict[str, dict[str, Any]] = {}
        for attr_name in dir(self):
            if not attr_name.startswith("_action_"):
                continue
            handler = getattr(self, attr_name, None)
            if handler is None or not callable(handler):
                continue
            action_name = attr_name.removeprefix("_action_")
            meta = collect_security_metadata(handler)
            if meta:
                result[action_name] = meta
        return result

    def is_supported_on_current_platform(self) -> bool:
        """Return ``True`` if this module runs on the current OS."""
        if Platform.ALL in self.SUPPORTED_PLATFORMS:
            return True
        current = _platform_module.system().lower()
        mapping: dict[str, Platform] = {
            "linux":   Platform.LINUX,
            "windows": Platform.WINDOWS,
            "darwin":  Platform.MACOS,
        }
        current_platform = mapping.get(current)
        if current_platform is None:
            return False
        return current_platform in self.SUPPORTED_PLATFORMS

    @abstractmethod
    def get_manifest(self) -> ModuleManifest:
        """Return the Capability Manifest for this module.

        The manifest is used to:
          - Generate LangChain tools
          - Populate the /modules API endpoint
          - Validate params schemas
        """
        ...

    def _check_dependencies(self) -> None:
        """Raise ``ModuleLoadError`` if a required dependency is missing.

        Called in ``__init__``.  Default implementation does nothing.
        """

    def get_context_snippet(self) -> str | None:
        """Return dynamic context for the LLM system prompt, or ``None``.

        Modules that manage stateful resources (e.g. database connections)
        can override this to inject live context (schemas, session info)
        into the system prompt that guides the LLM.

        Default: ``None`` (no dynamic context).
        """
        return None


    def set_context(self, ctx: Any) -> None:
        """Inject the ModuleContext into this module.

        Called by the server startup after constructing the ServiceBus
        and LifecycleManager.  Provides structured access to inter-module
        communication, events, and system services.
        """
        self._ctx = ctx

    @property
    def ctx(self) -> Any | None:
        """Return the ModuleContext, or None if not yet injected."""
        return getattr(self, "_ctx", None)

    @property
    def config(self) -> Any | None:
        """Return the current validated config, or None if not configured."""
        return self._config

    def _collect_config_schema(self) -> dict[str, Any] | None:
        """Generate config_schema from CONFIG_MODEL if defined."""
        if self.CONFIG_MODEL is not None:
            return self.CONFIG_MODEL.to_config_schema()
        return None

    def _collect_streaming_metadata(self) -> dict[str, dict[str, Any]]:
        """Introspect decorated ``_action_*`` methods for streaming metadata.

        Returns a dict keyed by action name (without ``_action_`` prefix),
        with values from :func:`collect_streaming_metadata`.
        """
        try:
            from digitorn.orchestration.streaming_decorators import collect_streaming_metadata
        except ImportError:
            return {}

        result: dict[str, dict[str, Any]] = {}
        for attr_name in dir(self):
            if not attr_name.startswith("_action_"):
                continue
            handler = getattr(self, attr_name, None)
            if handler is None or not callable(handler):
                continue
            action_name = attr_name.removeprefix("_action_")
            meta = collect_streaming_metadata(handler)
            if meta:
                result[action_name] = meta
        return result


    _SECURITY_SPEC_KEYS = frozenset(
        {"permissions", "risk_level", "irreversible", "data_classification"}
    )

    _HIGH_RISK_KEYWORDS = frozenset(
        {"delete", "kill", "admin", "credentials", "personal", "actuator"}
    )
    _MEDIUM_RISK_KEYWORDS = frozenset(
        {"write", "execute", "send", "external", "screen", "camera",
         "microphone", "keyboard", "browser", "gpio.write"}
    )

    def _enrich_manifest_metadata(self, manifest: ModuleManifest) -> ModuleManifest:
        """Auto-enrich manifest actions with security + streaming decorator metadata.

        Called automatically via ``__init_subclass__`` wrapping of ``get_manifest()``.
        Only fills fields that are still at their default (empty/falsy) values, so
        modules that already set them explicitly in ``get_manifest()`` are unaffected.
        """
        security_meta = self._collect_security_metadata()
        streaming_meta = self._collect_streaming_metadata()

        for action in manifest.actions:
            if action.name in security_meta:
                meta = security_meta[action.name]
                for key in self._SECURITY_SPEC_KEYS:
                    if key in meta and not getattr(action, key, None):
                        setattr(action, key, meta[key])

            if not action.risk_level and action.permissions:
                action.risk_level = self._infer_risk_from_permissions(
                    action.permissions
                )

            if not action.risk_level:
                action.risk_level = "low"

            if action.name in streaming_meta:
                meta = streaming_meta[action.name]
                if meta.get("streams_progress") and not action.streams_progress:
                    action.streams_progress = True

        return manifest

    @classmethod
    def _infer_risk_from_permissions(cls, permissions: list[str]) -> str:
        """Infer a risk level from permission strings when no explicit level is set."""
        joined = " ".join(permissions).lower()
        if any(kw in joined for kw in cls._HIGH_RISK_KEYWORDS):
            return "high"
        if any(kw in joined for kw in cls._MEDIUM_RISK_KEYWORDS):
            return "medium"
        return "low"


    async def on_start(self) -> None:
        """Called when the module transitions to ACTIVE state.

        Override to initialise connections, load models, etc.
        """

    async def on_stop(self) -> None:
        """Called when the module is being stopped/disabled.

        Override to close connections, save state, release resources.
        """

    def get_prompt_sections(self) -> list[dict[str, Any]]:
        """Return prompt sections to inject into the agent's system prompt.

        Override to contribute context to the LLM's system prompt.
        Each section is a dict with:
        - ``id`` (str): unique section identifier
        - ``title`` (str): section heading (e.g. "Database Context")
        - ``content`` (str): the text to inject
        - ``priority`` (int): lower = earlier (default 50)
        - ``position`` (str): "before_tools", "after_tools", or "end" (default "end")

        The context_builder collects sections from all active modules,
        sorts by priority, and inserts them at the specified positions.

        Returns an empty list by default - modules opt in by overriding.
        """
        return []

    # ── Workspace Provider (opt-in) ──────────────────────────────────

    def workspace_card(self) -> dict[str, Any] | None:
        """Return a workspace card for the UI panel, or None to opt out.

        Override this to give your module a presence in the workspace panel.
        The card is refreshed after every tool call involving this module.

        Return a dict with:
        - ``id`` (str): unique card identifier (defaults to MODULE_ID)
        - ``label`` (str): display name (e.g. "Database", "HTTP")
        - ``icon`` (str): emoji or symbol for the card header
        - ``sections`` (list): list of RendererSection dicts
          (table, list, stats, tree, code, kv, progress, outline, log, diff, image, etc.)
        - ``priority`` (int): sort order, lower = higher (default 50)

        Example::

            def workspace_card(self):
                return {
                    "id": "database",
                    "label": "Database",
                    "icon": "🗄️",
                    "priority": 30,
                    "sections": [
                        section_stats([{"label": "queries", "value": len(self._history)}]),
                        section_table(
                            columns=["Query", "Time", "Status"],
                            rows=[[q.sql[:50], f"{q.ms}ms", q.status] for q in self._history[-5:]],
                            title="Recent queries",
                        ),
                    ],
                }

        Returns None by default - modules opt in by overriding.
        """
        return None

    async def widget_interact(
        self,
        widget: str,
        action: str,
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Handle a bidirectional widget interaction from the frontend.

        Called when the user clicks an action button on an interactive section
        in the workspace panel. Override this to process user actions.

        Args:
            widget: Widget type (e.g. "sql_editor", "chart_editor").
            action: Action name (e.g. "execute", "explain", "format").
            state: Current widget state from the frontend.

        Returns:
            A dict with the result, or None.

        Example::

            async def widget_interact(self, widget, action, state):
                if widget == "sql_editor" and action == "execute":
                    result = await self._execute_query(state["query"])
                    return {"rows": len(result), "success": True}
        """
        return None

    async def on_pause(self) -> None:
        """Called when the module is paused (ACTIVE -> PAUSED).

        Override to suspend background tasks or release non-critical resources.
        """

    async def on_resume(self) -> None:
        """Called when the module resumes (PAUSED -> ACTIVE).

        Override to re-acquire resources suspended during pause.
        """

    async def on_config_update(self, config: dict[str, Any]) -> None:
        """Called when module configuration is updated at runtime.

        If CONFIG_MODEL is set, validates the incoming dict against the
        Pydantic model before storing. Subclasses can override to add
        custom logic after validation.

        Workspace is extracted automatically so every module has access
        to ``self._workspace`` without needing to parse config manually.
        """
        if isinstance(config, dict):
            ws = config.get("workspace")
            if ws:
                # ``{WORKSPACE}`` is a deferred placeholder - the real
                # path is injected per-session by ``manager._chat_locked``
                # via ``str.replace(WORKSPACE_PLACEHOLDER, ...)``. Calling
                # ``Path(ws).resolve()`` on the literal placeholder treats
                # it as a relative path and prefixes it with the daemon's
                # cwd, yielding a mangled ``<daemon cwd>\{WORKSPACE}``
                # that breaks the later substitution.
                from digitorn.core.runtime.types import WORKSPACE_PLACEHOLDER
                if ws == WORKSPACE_PLACEHOLDER:
                    self._workspace = WORKSPACE_PLACEHOLDER
                else:
                    self._workspace = str(Path(ws).resolve())
        if self.CONFIG_MODEL is not None:
            self._config = self.CONFIG_MODEL.model_validate(config)


    async def health_check(self) -> dict[str, Any]:
        """Return health status of this module.

        Override to add connectivity checks, model load status, etc.
        """
        return {
            "status": "ok",
            "module_id": self.MODULE_ID,
            "version": self.VERSION,
        }

    def metrics(self) -> dict[str, Any]:
        """Return operational metrics for this module.

        Override to expose action counts, latencies, cache hit rates, etc.
        """
        return {}

    def state_snapshot(self) -> dict[str, Any]:
        """Return a snapshot of this module's internal state.

        Override to expose active sessions, loaded models, connections, etc.
        """
        return {}


    def register_services(self) -> list[Any]:
        """Return ServiceDescriptor instances for services this module provides.

        Override to declare services on the ServiceBus during startup.
        Default: no services provided.
        """
        return []


    async def on_event(self, topic: str, event: dict[str, Any]) -> None:
        """Called when an event is emitted on a topic this module subscribes to.

        Modules declare subscribed topics via ``subscribes_events`` in their
        manifest.  The lifecycle manager auto-subscribes modules on start and
        auto-unsubscribes on stop.

        Override to react to system events (e.g. react to security events,
        perception changes, other module state changes).
        """


    async def restore_state(self, state: dict[str, Any]) -> None:
        """Restore module state after crash/restart.

        Receives the dict previously returned by :meth:`state_snapshot`.
        Override to restore connections, sessions, loaded models, etc.
        """


    async def on_install(self) -> None:
        """Called when module is first installed from the hub.

        Override to perform one-time setup (download models, create DB tables).
        """

    async def on_update(self, old_version: str) -> None:
        """Called when module is upgraded to a new version.

        Override to perform migrations between versions.
        """


    async def on_resource_pressure(self, level: str) -> None:
        """Called when system detects memory/CPU pressure.

        Args:
            level: ``"warning"`` (>75% usage) or ``"critical"`` (>90%).

        Override to release caches, unload models, close idle connections.
        """

    async def estimate_cost(
        self, action: str, params: dict[str, Any]
    ) -> ResourceEstimate:
        """Pre-execution cost estimation for the given action.

        Override to provide module-specific estimates.
        Default: returns a generic low-confidence estimate.
        """
        return ResourceEstimate()


    def policy_rules(self) -> ModulePolicy:
        """Declare runtime policy constraints for this module.

        Override to set max_parallel_calls, cooldowns, etc.
        Default: no constraints.
        """
        return ModulePolicy()

    def describe(self) -> dict[str, Any]:
        """Dynamic self-description for LLM introspection.

        Beyond the static manifest, this provides live context:
        loaded models, active connections, available capabilities
        based on current state.

        Default: returns the manifest as a dict.
        """
        return self.get_manifest().to_dict()


    def register_action(
        self,
        name: str,
        handler: Callable[..., Any],
        spec: ActionSpec | None = None,
    ) -> None:
        """Register a dynamic action at runtime.

        The handler must have signature: ``async (params: dict) -> Any``.
        Dynamic actions take precedence over ``_action_`` methods.
        """
        self._dynamic_actions[name] = handler
        if spec is not None:
            self._dynamic_specs[name] = spec

    def unregister_action(self, name: str) -> None:
        """Remove a dynamically registered action."""
        self._dynamic_actions.pop(name, None)
        self._dynamic_specs.pop(name, None)


    def _get_action_spec(self, action: str) -> "ActionSpec | None":
        """Return the ActionSpec for an action, or None if not declared."""
        entry = self._action_registry.get(action)
        if entry is not None:
            return entry.spec
        if action in self._dynamic_specs:
            return self._dynamic_specs[action]
        return None

    def _get_handler(self, action: str) -> Any:
        """Look up an action handler method and wrap it for the correct execution tier.

        Dispatch priority (first match wins):

        1. ``_action_registry`` - dict populated by ``@action`` at class
           definition time.  **O(1) lookup**, preferred path.
        2. ``_dynamic_actions`` - runtime-registered handlers (via
           :meth:`register_action`).
        3. ``_action_<name>`` naming convention - legacy fallback for
           modules that don't yet use the ``@action`` decorator.

        Execution mode (only applied to registry path):
        - ``"async"`` (default): handler is an ``async def``; runs on the
          event loop - completely non-blocking for I/O-bound work.
        - ``"threaded"``: handler is a blocking ``def``; wrapped in
          ``asyncio.to_thread()`` so the event loop stays responsive while
          the thread runs.  Ideal for blocking C-extension calls or legacy
          sync libraries.
        - ``"sync"``: like ``"threaded"``; kept as an alias for backwards
          compatibility.

        Raises:
            ActionNotFoundError: If the action is not found via any path.
        """
        import inspect as _inspect

        entry = self._action_registry.get(action)
        if entry is not None:
            unbound = entry.handler
            mode = entry.spec.execution_mode
            module_self = self

            if mode in ("threaded", "sync") and not _inspect.iscoroutinefunction(unbound):
                async def _threaded(params: dict[str, Any]) -> Any:
                    return await asyncio.to_thread(unbound, module_self, params)
                return _threaded

            elif _inspect.iscoroutinefunction(unbound):
                async def _bound_async(params: dict[str, Any]) -> Any:
                    return await unbound(module_self, params)
                return _bound_async

            else:
                def _bound_sync(params: dict[str, Any]) -> Any:
                    return unbound(module_self, params)
                return _bound_sync

        if action in self._dynamic_actions:
            return self._dynamic_actions[action]

        method_name = f"_action_{action}"
        handler = getattr(self, method_name, None)
        if handler is None:
            raise ActionNotFoundError(module_id=self.MODULE_ID, action=action)
        return handler


    async def execute(
        self, action: str, params: dict[str, Any], context: ExecutionContext | None = None
    ) -> Any:
        """Dispatch *action* to its registered handler.

        Handler resolution order (via :meth:`_get_handler`):

        1. ``_action_registry`` dict - ``@action``-decorated methods (O(1))
        2. ``_dynamic_actions`` dict - runtime-registered handlers
        3. ``_action_<name>`` method naming convention - legacy fallback

        Applies the two-level cache automatically when the handler is decorated
        with ``@cacheable`` or ``@invalidates_cache``:

        - **L2 cache check** (before execution): if the action is cacheable and
          a matching entry exists in Redis/fakeredis, the cached value is
          returned immediately without calling the handler.
        - **L2 invalidation** (before execution): if the action declares
          ``@invalidates_cache("other_action", ...)``, all matching Redis keys
          for those actions are deleted before the write executes.
        - **L2 cache store** (after successful execution): cacheable action
          results are stored in Redis with the configured TTL.

        Subclasses that need custom dispatch logic (e.g. stateful session
        management) may override this method.

        Args:
            action:  Action name (e.g. ``"read_file"``).
            params:  Already-resolved and schema-validated parameters.
            context: Optional execution context for tracing.

        Returns:
            Any JSON-serialisable value.  Will be sanitised by OutputSanitizer.

        Raises:
            ActionNotFoundError: If no ``_action_<action>`` method exists.
            ActionExecutionError: If the handler raises any unexpected exception.
        """
        handler = self._get_handler(action)
        cache_meta = _collect_handler_cache_meta(handler)

        action_spec = self._get_action_spec(action)
        if context and context.security_profile:
            from digitorn.core.security import security_gate

            security_gate(
                profile=context.security_profile,
                module_id=self.MODULE_ID,
                action=action,
                required_permissions=action_spec.permissions if action_spec else [],
                risk_level=action_spec.risk_level if action_spec else "",
                irreversible=action_spec.irreversible if action_spec else False,
                params=params,
                agent_id=getattr(context, "plan_id", "").removeprefix("agent:"),
                session_id=getattr(context, "session_id", "") or "",
            )

        _policy = context.policy_enforcer if context else None
        if _policy:
            await _policy.check_and_acquire(self.MODULE_ID, action)

        no_cache = bool(params.pop("_no_cache", False))

        entry = self._action_registry.get(action)
        if entry is not None and getattr(entry, "params_model", None) is not None:
            # Auto-coerce common LLM mistakes before validation
            params = _auto_coerce_params(params, entry.params_model)
            try:
                from pydantic import ValidationError as _VE
                params = entry.params_model.model_validate(params)
            except _VE as ve:
                return ActionResult(
                    success=False,
                    error=_format_validation_error(ve, action, entry.params_model),
                )

        invalidates = cache_meta.get("invalidates")
        if invalidates and not _cache_unavailable:
            try:
                l2 = await _cache_get_client()
                if l2.enabled:
                    for pattern in _cache_make_invalidation_patterns(
                        self.MODULE_ID, tuple(invalidates)
                    ):
                        removed = await l2.delete_pattern(pattern)
                        if removed:
                            logger.debug(
                                "cache_l2_invalidate module=%s action=%s pattern=%s removed=%d",
                                self.MODULE_ID, action, pattern, removed,
                            )
            except Exception as exc:
                logger.debug("cache L2 invalidation error: %s", exc)

        if not no_cache and cache_meta.get("cacheable") and cache_meta.get("shared", True) and not _cache_unavailable:
            try:
                l2 = await _cache_get_client()
                if l2.enabled:
                    key = _cache_make_key(
                        self.MODULE_ID, action, params, cache_meta.get("key_params")
                    )
                    cached = await l2.get(key)
                    if cached is not None:
                        logger.debug(
                            "cache_l2_hit module=%s action=%s", self.MODULE_ID, action
                        )
                        return cached
            except Exception as exc:
                logger.debug("cache L2 get error: %s", exc)

        _ctx_token = self._context_var.set(context)
        try:
            if self._middleware_pipeline is not None:
                async def _handler_dispatch(_action: str, _params: Any) -> Any:
                    return await _invoke_handler_async(handler, _params)
                result = await self._middleware_pipeline.execute(
                    self.MODULE_ID, action, params, _handler_dispatch,
                )
            else:
                result = await _invoke_handler_async(handler, params)
        except (
            ActionNotFoundError,
            ActionExecutionError,
            PermissionDeniedError,
            PermissionNotGrantedError,
            ApprovalRequiredError,
            PolicyViolationError,
            RateLimitExceededError,
        ):
            raise
        except Exception as exc:
            raise ActionExecutionError(
                module_id=self.MODULE_ID, action=action, cause=exc
            ) from exc
        finally:
            self._context_var.reset(_ctx_token)
            if _policy:
                _policy.release(self.MODULE_ID)

        if cache_meta.get("cacheable") and cache_meta.get("shared", True) and not _cache_unavailable:
            try:
                l2 = await _cache_get_client()
                if l2.enabled:
                    key = _cache_make_key(
                        self.MODULE_ID, action, params, cache_meta.get("key_params")
                    )
                    await l2.set(key, result, ttl=cache_meta.get("ttl") or None)
                    logger.debug(
                        "cache_l2_store module=%s action=%s ttl=%s",
                        self.MODULE_ID, action, cache_meta.get("ttl"),
                    )
            except Exception as exc:
                logger.debug("cache L2 set error: %s", exc)

        return result

"""Module layer - BaseModule interface."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import logging
import platform as _platform_module
from pathlib import Path

logger = logging.getLogger(__name__)

# `digitorn.cache` is optional; resolve imports once and short-circuit
# call-sites via `_cache_unavailable` when it isn't installed.
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
    if inspect.iscoroutinefunction(handler):
        return await handler(params)
    return await asyncio.to_thread(handler, params)

def _collect_handler_cache_meta(handler: Any) -> dict[str, Any]:
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
    """Supported operating system platforms."""

    ALL          = "all"
    LINUX        = "linux"
    WINDOWS      = "windows"
    MACOS        = "macos"
    RASPBERRY_PI = "raspberry_pi"

_T = TypeVar("_T")

@dataclass(frozen=True)
class ExecutionContext:
    """Immutable, typed gateway of runtime services passed to every action."""

    plan_id: str
    action_id: str
    app_id: str | None = None
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
    # Workdir-scoped sandbox shared by every agent-facing module; None in CLI / tests.
    path_policy: Any | None = field(default=None, compare=False)

@dataclass
class ActionResult(Generic[_T]):
    """Generic, structured result envelope returned by every module action."""

    success: bool
    data: _T | None = None
    output: Any = field(default=None, repr=False)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Capture stack when a failure result has no diagnostic, for postmortem.
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
            except Exception as exc:
                logger.debug("base best-effort block failed: %s", exc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data if self.data is not None else self.output,
            "error": self.error,
            "metadata": self.metadata,
        }

@dataclass
class ResourceEstimate:
    """Pre-execution cost estimation for an action."""

    estimated_duration_seconds: float = 1.0
    estimated_memory_mb: float = 10.0
    estimated_cpu_percent: float = 10.0
    estimated_io_operations: int = 0
    confidence: float = 0.5

@dataclass
class ModulePolicy:
    """Runtime policy constraints declared by a module."""

    max_parallel_calls: int = 0
    cooldown_seconds: float = 0.0
    allow_remote_invocation: bool = True
    execution_timeout: float = 0.0
    max_memory_mb: int = 0
    retry_on_failure: bool = False

class BaseModule(ABC, IModule):
    """Abstract base class for all Digitorn modules."""

    MODULE_ID: str = ""
    VERSION: str = "0.0.0"
    SUPPORTED_PLATFORMS: list[Platform] = [Platform.ALL]
    MODULE_TYPE: str = "user"
    CONFIG_MODEL: type | None = None
    # When True, every app deployment shares the daemon-wide singleton instance.
    MODULE_SINGLETON: bool = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
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
        """Per-execution workspace, session-scoped."""
        ctx = self._context_var.get()
        if ctx is not None and ctx.workspace:
            return ctx.workspace
        return self._workspace

    @property
    def stream(self) -> Any | None:
        """Access the execution stream for real-time agent feedback."""
        ctx = self._context_var.get()
        if ctx is not None:
            return ctx.stream
        return None

    @property
    def _context(self) -> ExecutionContext | None:
        return self._context_var.get()

    @_context.setter
    def _context(self, value: ExecutionContext | None) -> None:
        self._context_var.set(value)

    def _notify_bg(self, notification: dict[str, Any]) -> None:
        if self._bg_notify is not None:
            self._bg_notify(notification)

    def set_security(self, security: Any) -> None:
        """Inject the SecurityManager into this module."""
        self._security = security

    def _collect_security_metadata(self) -> dict[str, dict[str, Any]]:
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
        """Return `True` if this module runs on the current OS."""
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
        """Return the Capability Manifest for this module."""
        ...

    def _check_dependencies(self) -> None:
        pass

    def get_context_snippet(self) -> str | None:
        """Return dynamic context for the LLM system prompt, or `None`."""
        return None

    def set_context(self, ctx: Any) -> None:
        """Inject the ModuleContext into this module."""
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
        if self.CONFIG_MODEL is not None:
            return self.CONFIG_MODEL.to_config_schema()
        return None

    def _collect_streaming_metadata(self) -> dict[str, dict[str, Any]]:
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
        joined = " ".join(permissions).lower()
        if any(kw in joined for kw in cls._HIGH_RISK_KEYWORDS):
            return "high"
        if any(kw in joined for kw in cls._MEDIUM_RISK_KEYWORDS):
            return "medium"
        return "low"

    async def on_start(self) -> None:
        """Called when the module transitions to ACTIVE state."""

    async def on_stop(self) -> None:
        """Called when the module is being stopped/disabled."""

    def get_prompt_sections(self) -> list[dict[str, Any]]:
        """Return prompt sections to inject into the agent's system prompt."""
        return []

    def workspace_card(self) -> dict[str, Any] | None:
        """Return a workspace card for the UI panel, or None to opt out."""
        return None

    async def widget_interact(
        self,
        widget: str,
        action: str,
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Handle a bidirectional widget interaction from the frontend."""
        return None

    async def on_pause(self) -> None:
        """Called when the module is paused (ACTIVE -> PAUSED)."""

    async def on_resume(self) -> None:
        """Called when the module resumes (PAUSED -> ACTIVE)."""

    async def on_config_update(self, config: dict[str, Any]) -> None:
        """Called when module configuration is updated at runtime."""
        if isinstance(config, dict):
            ws = config.get("workspace")
            if ws:
                # WORKSPACE_PLACEHOLDER must stay literal; manager substitutes it per-session.
                from digitorn.core.runtime.types import WORKSPACE_PLACEHOLDER
                if ws == WORKSPACE_PLACEHOLDER:
                    self._workspace = WORKSPACE_PLACEHOLDER
                else:
                    self._workspace = str(Path(ws).resolve())
        if self.CONFIG_MODEL is not None:
            self._config = self.CONFIG_MODEL.model_validate(config)

    async def health_check(self) -> dict[str, Any]:
        """Return health status of this module."""
        return {
            "status": "ok",
            "module_id": self.MODULE_ID,
            "version": self.VERSION,
        }

    def metrics(self) -> dict[str, Any]:
        """Return operational metrics for this module."""
        return {}

    def state_snapshot(self) -> dict[str, Any]:
        """Return a snapshot of this module's internal state."""
        return {}

    def register_services(self) -> list[Any]:
        """Return ServiceDescriptor instances for services this module provides."""
        return []

    async def on_event(self, topic: str, event: dict[str, Any]) -> None:
        """Called when an event is emitted on a topic this module subscribes."""

    async def restore_state(self, state: dict[str, Any]) -> None:
        """Restore module state after crash/restart."""

    async def on_install(self) -> None:
        """Called when module is first installed from the hub."""

    async def on_update(self, old_version: str) -> None:
        """Called when module is upgraded to a new version."""

    async def on_resource_pressure(self, level: str) -> None:
        """Called when system detects memory/CPU pressure."""

    async def estimate_cost(
        self, action: str, params: dict[str, Any]
    ) -> ResourceEstimate:
        """Pre-execution cost estimation for the given action."""
        return ResourceEstimate()

    def policy_rules(self) -> ModulePolicy:
        """Declare runtime policy constraints for this module."""
        return ModulePolicy()

    def describe(self) -> dict[str, Any]:
        """Dynamic self-description for LLM introspection."""
        return self.get_manifest().to_dict()

    def register_action(
        self,
        name: str,
        handler: Callable[..., Any],
        spec: ActionSpec | None = None,
    ) -> None:
        """Register a dynamic action at runtime."""
        self._dynamic_actions[name] = handler
        if spec is not None:
            self._dynamic_specs[name] = spec

    def unregister_action(self, name: str) -> None:
        """Remove a dynamically registered action."""
        self._dynamic_actions.pop(name, None)
        self._dynamic_specs.pop(name, None)

    def _get_action_spec(self, action: str) -> "ActionSpec | None":
        entry = self._action_registry.get(action)
        if entry is not None:
            return entry.spec
        if action in self._dynamic_specs:
            return self._dynamic_specs[action]
        return None

    def _get_handler(self, action: str) -> Any:
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
        """Dispatch *action* to its registered handler."""
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

        # When no context is passed, inherit the surrounding execute()'s context
        # so inter-module calls keep workspace / session / user_id.
        effective_ctx = (
            context if context is not None else self._context_var.get()
        )
        _ctx_token = self._context_var.set(effective_ctx)
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

"""Digitorn Middleware System - pluggable pipeline at every level."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class AppMiddlewareContext:
    """Context for app-level middleware, passed before each LLM call."""

    agent_id: str
    system_prompt: str
    messages: list[dict[str, Any]]
    turn: int
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AppMiddleware(Protocol):
    """Protocol for app-level middleware."""

    async def before(self, ctx: AppMiddlewareContext) -> str | None:
        """Called before the LLM call."""
        ...

    async def after(
        self, ctx: AppMiddlewareContext, response: str, tool_calls: list[Any],
    ) -> str:
        """Called after the LLM response."""
        ...


class SecretMaskMiddleware:
    """Mask sensitive patterns in user messages before sending to LLM."""

    def __init__(
        self,
        patterns: list[str] | None = None,
        replacement: str = "[MASKED]",
        mask_values: bool = True,
    ) -> None:
        default_patterns = [
            r"(?i)(password|passwd|pwd)\s*(?:[:=]|is)\s*\S+",
            r"(?i)(api[_-]?key|apikey)\s*(?:[:=]|is)\s*\S+",
            r"(?i)(secret[_-]?key|secret)\s*(?:[:=]|is)\s*\S+",
            r"(?i)(token|auth[_-]?token|access[_-]?token)\s*(?:[:=]|is)\s*\S+",
            r"(?i)(bearer\s+)\S+",
            r"sk-[a-zA-Z0-9]{20,}",
            r"ghp_[a-zA-Z0-9]{36}",
            r"glpat-[a-zA-Z0-9\-]{20,}",
        ]
        custom = [
            rf"(?i)({p})\s*[:=]\s*\S+" for p in (patterns or [])
        ]
        self._patterns = [re.compile(p) for p in default_patterns + custom]
        self._replacement = replacement
        self._mask_values = mask_values
        self._masked_count = 0

    async def before(self, ctx: AppMiddlewareContext) -> str | None:
        for msg in ctx.messages:
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                original = msg["content"]
                masked = self._mask(original)
                if masked != original:
                    msg["content"] = masked
                    self._masked_count += 1
        return None

    async def after(self, ctx: AppMiddlewareContext, response: str, tool_calls: list) -> str:
        """Mask secrets in LLM response as well."""
        if response:
            masked = self._mask(response)
            if masked != response:
                self._masked_count += 1
            return masked
        return response

    def _mask(self, text: str) -> str:
        for pattern in self._patterns:
            text = pattern.sub(self._replacement, text)
        return text

    @property
    def stats(self) -> dict[str, int]:
        return {"masked_messages": self._masked_count}


class PromptInjectMiddleware:
    """Dynamically inject content into the system prompt."""

    def __init__(
        self,
        system: str = "",
        position: str = "append",
    ) -> None:
        self.system = system
        self.position = position

    async def before(self, ctx: AppMiddlewareContext) -> str | None:
        if not self.system:
            return None
        if self.position == "prepend":
            ctx.system_prompt = self.system + "\n\n" + ctx.system_prompt
        else:
            ctx.system_prompt = ctx.system_prompt + "\n\n" + self.system
        return None

    async def after(self, ctx: AppMiddlewareContext, response: str, tool_calls: list) -> str:
        return response


class ContentFilterMiddleware:
    """Block messages containing dangerous patterns."""

    def __init__(
        self,
        block_patterns: list[str] | None = None,
        rejection_message: str = "This request has been blocked by content filter.",
    ) -> None:
        self._patterns = [
            re.compile(p, re.IGNORECASE) for p in (block_patterns or [])
        ]
        self._rejection = rejection_message
        self._blocked_count = 0

    async def before(self, ctx: AppMiddlewareContext) -> str | None:
        for msg in reversed(ctx.messages):
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                for pattern in self._patterns:
                    if pattern.search(msg["content"]):
                        self._blocked_count += 1
                        logger.warning(
                            "content_filter_blocked agent=%s pattern=%s",
                            ctx.agent_id, pattern.pattern,
                        )
                        return self._rejection
                break
        return None

    async def after(self, ctx: AppMiddlewareContext, response: str, tool_calls: list) -> str:
        return response

    @property
    def stats(self) -> dict[str, int]:
        return {"blocked_count": self._blocked_count}


class RagInjectMiddleware:
    """Inject RAG (retrieval-augmented generation) context."""

    def __init__(
        self,
        max_chunks: int = 5,
        max_chars: int = 2000,
        position: str = "append",
        knowledge_base: str = "",
        collection: str = "",
    ) -> None:
        self.max_chunks = max_chunks
        self.max_chars = max_chars
        self.position = position
        self.knowledge_base = knowledge_base or collection
        self._retriever: Any | None = None
        self._inject_count = 0

    def set_retriever(self, retriever: Any) -> None:
        """Set the retrieval function: async fn(query: str) -> list[str]."""
        self._retriever = retriever

    async def before(self, ctx: AppMiddlewareContext) -> str | None:
        if self._retriever is None:
            return None

        query = ""
        for msg in reversed(ctx.messages):
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                query = msg["content"]
                break

        if not query:
            return None

        try:
            chunks = await self._retriever(query)
        except Exception as exc:
            logger.warning("rag_inject_error: %s", exc)
            return None

        if not chunks:
            return None

        selected = chunks[:self.max_chunks]
        total_chars = 0
        formatted: list[str] = []
        for i, chunk in enumerate(selected, 1):
            if total_chars + len(chunk) > self.max_chars:
                chunk = chunk[:self.max_chars - total_chars]
                formatted.append(f"[{i}] {chunk}")
                break
            formatted.append(f"[{i}] {chunk}")
            total_chars += len(chunk)

        rag_block = (
            "\n\n--- Relevant context (retrieved automatically) ---\n"
            + "\n".join(formatted)
            + "\n--- End of context ---"
        )

        if self.position == "prepend":
            ctx.system_prompt = rag_block + "\n\n" + ctx.system_prompt
        else:
            ctx.system_prompt = ctx.system_prompt + rag_block

        self._inject_count += 1
        return None

    async def after(self, ctx: AppMiddlewareContext, response: str, tool_calls: list) -> str:
        return response

    @property
    def stats(self) -> dict[str, int]:
        return {"inject_count": self._inject_count}


class ResponseFilterMiddleware:
    """Filter or transform the LLM's response before returning to user."""

    def __init__(
        self,
        max_length: int = 0,
        mask_secrets: bool = False,
    ) -> None:
        self.max_length = max_length
        self.mask_secrets = mask_secrets
        self._secret_masker = SecretMaskMiddleware() if mask_secrets else None

    async def before(self, ctx: AppMiddlewareContext) -> str | None:
        return None

    async def after(self, ctx: AppMiddlewareContext, response: str, tool_calls: list) -> str:
        if self._secret_masker:
            response = self._secret_masker._mask(response)
        if self.max_length > 0 and len(response) > self.max_length:
            response = response[:self.max_length] + "\n\n[Response truncated]"
        return response


@dataclass
class ModuleCallContext:
    """Context for module-level middleware."""

    module_id: str
    action: str
    params: dict[str, Any]
    attempt: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ModuleMiddleware(Protocol):
    """Protocol for module-level middleware."""

    async def __call__(
        self, ctx: ModuleCallContext, next_: Any,
    ) -> Any: ...


class ModuleAuditMiddleware:
    """Audit logging for module calls."""

    def __init__(self, log_params: bool = False, log_result: bool = False) -> None:
        self.log_params = log_params
        self.log_result = log_result
        self._total_calls = 0
        self._total_errors = 0
        self._total_duration_ms = 0.0

    async def __call__(self, ctx: ModuleCallContext, next_: Any) -> Any:
        self._total_calls += 1
        start = time.monotonic()
        extra = f" params={ctx.params!r}" if self.log_params else ""
        try:
            result = await next_(ctx)
            duration = (time.monotonic() - start) * 1000
            self._total_duration_ms += duration
            logger.info(
                "module_audit module=%s action=%s duration_ms=%.0f ok=true%s",
                ctx.module_id, ctx.action, duration, extra,
            )
            return result
        except Exception as exc:
            duration = (time.monotonic() - start) * 1000
            self._total_duration_ms += duration
            self._total_errors += 1
            logger.info(
                "module_audit module=%s action=%s duration_ms=%.0f ok=false error=%s%s",
                ctx.module_id, ctx.action, duration, exc, extra,
            )
            raise

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "total_calls": self._total_calls,
            "total_errors": self._total_errors,
            "total_duration_ms": round(self._total_duration_ms),
        }


class ModuleRetryMiddleware:
    """Retry failed module calls with backoff."""

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        backoff: str = "exponential",
    ) -> None:
        self.max_attempts = max(1, max_attempts)
        self.base_delay = base_delay
        self.backoff = backoff

    async def __call__(self, ctx: ModuleCallContext, next_: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            ctx.attempt = attempt
            try:
                return await next_(ctx)
            except Exception as exc:
                last_exc = exc
                if attempt == self.max_attempts:
                    break
                delay = self.base_delay * (2 ** (attempt - 1)) if self.backoff == "exponential" else self.base_delay
                delay = min(delay, 30.0)
                logger.warning(
                    "module_retry module=%s action=%s attempt=%d/%d delay=%.1fs",
                    ctx.module_id, ctx.action, attempt, self.max_attempts, delay,
                )
                await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]


class ModuleTimeoutMiddleware:
    """Per-call timeout for module execution."""

    def __init__(self, seconds: float = 30.0) -> None:
        self.seconds = seconds

    async def __call__(self, ctx: ModuleCallContext, next_: Any) -> Any:
        return await asyncio.wait_for(next_(ctx), timeout=self.seconds)


class AppMiddlewarePipeline:
    """Pipeline for app-level middleware."""

    def __init__(self, middlewares: list[AppMiddleware] | None = None) -> None:
        self._middlewares: list[AppMiddleware] = middlewares or []

    @property
    def middlewares(self) -> list[AppMiddleware]:
        return self._middlewares

    def add(self, mw: AppMiddleware) -> None:
        self._middlewares.append(mw)

    async def run_before(self, ctx: AppMiddlewareContext) -> str | None:
        """Run all before() hooks."""
        for mw in self._middlewares:
            result = await mw.before(ctx)
            if result is not None:
                return result
        return None

    async def run_after(
        self, ctx: AppMiddlewareContext, response: str, tool_calls: list[Any],
    ) -> str:
        """Run all after() hooks in reverse order."""
        for mw in reversed(self._middlewares):
            response = await mw.after(ctx, response, tool_calls)
        return response

    def get_middleware(self, cls: type) -> Any | None:
        for mw in self._middlewares:
            if isinstance(mw, cls):
                return mw
        return None


class ModuleMiddlewarePipeline:
    """Pipeline for module-level middleware (same mechanics as MCP pipeline)."""

    def __init__(self, middlewares: list[ModuleMiddleware] | None = None) -> None:
        self._middlewares: list[ModuleMiddleware] = middlewares or []

    @property
    def middlewares(self) -> list[ModuleMiddleware]:
        return self._middlewares

    def add(self, mw: ModuleMiddleware) -> None:
        self._middlewares.append(mw)

    async def execute(
        self,
        module_id: str,
        action: str,
        params: dict[str, Any],
        handler: Any,
    ) -> Any:
        ctx = ModuleCallContext(module_id=module_id, action=action, params=params)

        async def terminal(c: ModuleCallContext) -> Any:
            return await handler(c.action, c.params)

        chain = terminal
        for mw in reversed(self._middlewares):
            chain = _make_module_link(mw, chain)

        return await chain(ctx)

    def get_middleware(self, cls: type) -> Any | None:
        for mw in self._middlewares:
            if isinstance(mw, cls):
                return mw
        return None


def _make_module_link(mw: ModuleMiddleware, next_fn: Any) -> Any:
    async def link(ctx: ModuleCallContext) -> Any:
        return await mw(ctx, next_fn)
    return link


_APP_MIDDLEWARE_FALLBACK: dict[str, type] = {
    "mask_secrets": SecretMaskMiddleware,
    "prompt_inject": PromptInjectMiddleware,
    "content_filter": ContentFilterMiddleware,
    "rag_inject": RagInjectMiddleware,
    "response_filter": ResponseFilterMiddleware,
}

_MODULE_MIDDLEWARE_FALLBACK: dict[str, type] = {
    "audit": ModuleAuditMiddleware,
    "retry": ModuleRetryMiddleware,
    "timeout": ModuleTimeoutMiddleware,
}


def _resolve_middleware(name: str, kwargs: dict, level: str) -> Any | None:
    """Resolve a middleware by name: TOML registry first, then inline fallback."""
    try:
        from digitorn.core.middleware_store import get_middleware_registry
        registry = get_middleware_registry()
        if not registry.list_all():
            registry.discover()
        instance = registry.instantiate(name, kwargs or None)
        if instance is not None:
            return instance
    except Exception as exc:
        logger.debug("Middleware instantiation failed for '%s': %s", name, exc)

    fallback = _APP_MIDDLEWARE_FALLBACK if level == "app" else _MODULE_MIDDLEWARE_FALLBACK
    cls = fallback.get(name)
    if cls is not None:
        try:
            return cls(**(kwargs or {}))
        except TypeError as exc:
            logger.warning("%s_middleware_config_error name=%s: %s", level, name, exc)
    return None


def build_app_pipeline(
    config_list: list[dict[str, Any]] | None,
    *,
    custom_base_path: str | None = None,
) -> AppMiddlewarePipeline | None:
    """Build an app-level middleware pipeline from YAML config."""
    if not config_list:
        return None

    middlewares: list[AppMiddleware] = []
    for entry in config_list:
        if isinstance(entry, str):
            entry = {entry: {}}
        if not isinstance(entry, dict):
            logger.warning("app_middleware_invalid_entry: %s", entry)
            continue

        for name, kwargs in entry.items():
            if kwargs is None:
                kwargs = {}

            mw = _resolve_middleware(name, kwargs, "app")
            if mw is not None:
                middlewares.append(mw)
            else:
                logger.warning("app_middleware_unknown: %s", name)

    if not middlewares:
        return None

    return AppMiddlewarePipeline(middlewares)


def build_module_pipeline(
    config_list: list[dict[str, Any]] | None,
    *,
    custom_base_path: str | None = None,
) -> ModuleMiddlewarePipeline | None:
    """Build a module-level middleware pipeline from YAML config."""
    if not config_list:
        return None

    middlewares: list[ModuleMiddleware] = []
    for entry in config_list:
        if isinstance(entry, str):
            entry = {entry: {}}
        if not isinstance(entry, dict):
            logger.warning("module_middleware_invalid_entry: %s", entry)
            continue

        for name, kwargs in entry.items():
            if kwargs is None:
                kwargs = {}

            mw = _resolve_middleware(name, kwargs, "module")
            if mw is not None:
                middlewares.append(mw)
            else:
                logger.warning("module_middleware_unknown: %s", name)

    if not middlewares:
        return None

    return ModuleMiddlewarePipeline(middlewares)


def _load_custom_middleware(
    config: dict[str, Any],
    base_path: str | None,
    level: str,
) -> Any | None:
    """Load a custom middleware class."""
    file_path = config.get("path")
    module_import = config.get("module")
    class_name = config.get("class")
    init_kwargs = config.get("config", {})

    if not class_name:
        logger.warning(
            "custom_%s_middleware: 'class' is required", level,
        )
        return None

    if not file_path and not module_import:
        logger.warning(
            "custom_%s_middleware: 'path' or 'module' is required", level,
        )
        return None

    if module_import:
        return _load_from_package(module_import, class_name, init_kwargs, level)

    p = Path(file_path)

    if not p.is_absolute() and base_path:
        candidate = (Path(base_path) / p).resolve()
        if candidate.exists():
            p = candidate
        else:
            mw_dir = Path(base_path) / "middleware"
            candidate2 = (mw_dir / p.name).resolve()
            if candidate2.exists():
                p = candidate2
            else:
                p = candidate

    p = p.resolve()

    if not p.exists():
        logger.warning("custom_%s_middleware: file not found: %s", level, p)
        return None

    return _load_from_file(p, class_name, init_kwargs, level)


def _load_from_file(
    p: Path, class_name: str, init_kwargs: dict, level: str,
) -> Any | None:
    """Load a middleware class from a Python file."""
    try:
        spec = importlib.util.spec_from_file_location(
            f"digitorn_custom_middleware_{p.stem}", str(p),
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load module from {p}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls = getattr(module, class_name, None)
        if cls is None:
            logger.warning(
                "custom_%s_middleware: class '%s' not found in %s",
                level, class_name, p,
            )
            return None
        instance = cls(**init_kwargs) if init_kwargs else cls()
        logger.info(
            "custom_%s_middleware loaded: %s from %s",
            level, class_name, p,
        )
        return instance
    except Exception as exc:
        logger.warning(
            "custom_%s_middleware: failed to load %s from %s: %s",
            level, class_name, p, exc,
        )
        return None


def _load_from_package(
    module_path: str, class_name: str, init_kwargs: dict, level: str,
) -> Any | None:
    """Load a middleware class from an installed Python package."""
    try:
        import importlib as _il
        mod = _il.import_module(module_path)
        cls = getattr(mod, class_name, None)
        if cls is None:
            logger.warning(
                "custom_%s_middleware: class '%s' not found in module '%s'",
                level, class_name, module_path,
            )
            return None
        instance = cls(**init_kwargs) if init_kwargs else cls()
        logger.info(
            "custom_%s_middleware loaded: %s from package %s",
            level, class_name, module_path,
        )
        return instance
    except ImportError as exc:
        logger.warning(
            "custom_%s_middleware: cannot import '%s': %s",
            level, module_path, exc,
        )
        return None
    except Exception as exc:
        logger.warning(
            "custom_%s_middleware: failed to load %s from %s: %s",
            level, class_name, module_path, exc,
        )
        return None

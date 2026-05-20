"""MCP Middleware Pipeline - pluggable layers around MCP tool calls."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

@dataclass
class MCPCallContext:
    """Immutable context for a single MCP tool call flowing through the pipeline."""

    server_id: str
    tool_name: str
    params: dict[str, Any]
    attempt: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

@runtime_checkable
class MCPMiddleware(Protocol):
    """Protocol for MCP middleware."""

    async def __call__(
        self,
        ctx: MCPCallContext,
        next_: Any,
    ) -> Any: ...

class RetryMiddleware:
    """Retry failed calls with exponential backoff."""

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        backoff: str = "exponential",
        max_delay: float = 30.0,
    ) -> None:
        self.max_attempts = max(1, max_attempts)
        self.base_delay = base_delay
        self.backoff = backoff
        self.max_delay = max_delay

    async def __call__(self, ctx: MCPCallContext, next_: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            ctx.attempt = attempt
            try:
                return await next_(ctx)
            except Exception as exc:
                last_exc = exc
                if attempt == self.max_attempts:
                    break
                delay = self._compute_delay(attempt)
                logger.warning(
                    "mcp_middleware_retry server=%s tool=%s attempt=%d/%d "
                    "delay=%.1fs error=%s",
                    ctx.server_id, ctx.tool_name, attempt, self.max_attempts,
                    delay, exc,
                )
                await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]

    def _compute_delay(self, attempt: int) -> float:
        if self.backoff == "fixed":
            return min(self.base_delay, self.max_delay)
        return min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)

class TimeoutMiddleware:
    """Enforce a per-call timeout."""

    def __init__(self, seconds: float = 30.0) -> None:
        self.seconds = seconds

    async def __call__(self, ctx: MCPCallContext, next_: Any) -> Any:
        try:
            return await asyncio.wait_for(next_(ctx), timeout=self.seconds)
        except asyncio.TimeoutError:
            logger.warning(
                "mcp_middleware_timeout server=%s tool=%s timeout=%.1fs",
                ctx.server_id, ctx.tool_name, self.seconds,
            )
            raise

class AuditMiddleware:
    """Structured audit logging for every MCP call."""

    def __init__(
        self,
        log_params: bool = False,
        log_result: bool = False,
    ) -> None:
        self.log_params = log_params
        self.log_result = log_result
        self._total_calls: int = 0
        self._total_errors: int = 0
        self._total_duration_ms: float = 0.0

    async def __call__(self, ctx: MCPCallContext, next_: Any) -> Any:
        self._total_calls += 1
        start = time.monotonic()
        extra = ""
        if self.log_params:
            extra = f" params={ctx.params!r}"

        try:
            result = await next_(ctx)
            duration_ms = (time.monotonic() - start) * 1000
            self._total_duration_ms += duration_ms
            result_info = ""
            if self.log_result and hasattr(result, "text"):
                text = result.text or ""
                result_info = f" result={text[:200]}"
            logger.info(
                "mcp_audit server=%s tool=%s duration_ms=%.0f ok=true%s%s",
                ctx.server_id, ctx.tool_name, duration_ms, extra, result_info,
            )
            ctx.metadata["duration_ms"] = round(duration_ms)
            return result
        except Exception as exc:
            duration_ms = (time.monotonic() - start) * 1000
            self._total_duration_ms += duration_ms
            self._total_errors += 1
            logger.info(
                "mcp_audit server=%s tool=%s duration_ms=%.0f ok=false "
                "error=%s%s",
                ctx.server_id, ctx.tool_name, duration_ms, exc, extra,
            )
            raise

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "total_calls": self._total_calls,
            "total_errors": self._total_errors,
            "total_duration_ms": round(self._total_duration_ms),
            "avg_duration_ms": (
                round(self._total_duration_ms / self._total_calls)
                if self._total_calls > 0 else 0
            ),
        }

class TransformMiddleware:
    """Input/output transformations."""

    def __init__(
        self,
        input_transforms: list[Any] | None = None,
        output_transforms: list[Any] | None = None,
    ) -> None:
        self._input_transforms = input_transforms or []
        self._output_transforms = output_transforms or []

    async def __call__(self, ctx: MCPCallContext, next_: Any) -> Any:
        for transform in self._input_transforms:
            ctx.params = transform(ctx.params)

        result = await next_(ctx)

        for transform in self._output_transforms:
            result = transform(result)

        return result

class BudgetMiddleware:
    """Per-server and global call budget enforcement."""

    def __init__(
        self,
        max_calls_per_hour: int = 0,
        server_limits: dict[str, int] | None = None,
        alert_threshold: float = 0.8,
        cost_per_call: float = 0.0,
        max_cost_per_hour: float = 0.0,
    ) -> None:
        self.max_calls_per_hour = max_calls_per_hour
        self.server_limits = server_limits or {}
        self.alert_threshold = alert_threshold
        self.cost_per_call = cost_per_call
        self.max_cost_per_hour = max_cost_per_hour

        self._call_log: list[tuple[float, str, float]] = []
        self._alerts_fired: set[str] = set()
        self._alert_callback: Any | None = None

    def set_alert_callback(self, callback: Any) -> None:
        """Set a callback for budget alerts: fn(alert_type, message, stats)."""
        self._alert_callback = callback

    async def __call__(self, ctx: MCPCallContext, next_: Any) -> Any:
        now = time.monotonic()
        self._purge_old_entries(now)

        if self.max_calls_per_hour > 0:
            total_calls = len(self._call_log)
            if total_calls >= self.max_calls_per_hour:
                raise BudgetExceededError(
                    f"Global budget exceeded: {total_calls}/{self.max_calls_per_hour} "
                    f"calls/hour"
                )
            self._check_alert("global", total_calls, self.max_calls_per_hour)

        srv_limit = self.server_limits.get(ctx.server_id)
        if srv_limit is not None and srv_limit > 0:
            srv_calls = sum(1 for _, sid, _ in self._call_log if sid == ctx.server_id)
            if srv_calls >= srv_limit:
                raise BudgetExceededError(
                    f"Server '{ctx.server_id}' budget exceeded: "
                    f"{srv_calls}/{srv_limit} calls/hour"
                )
            self._check_alert(f"server:{ctx.server_id}", srv_calls, srv_limit)

        if self.max_cost_per_hour > 0:
            total_cost = sum(c for _, _, c in self._call_log)
            if total_cost + self.cost_per_call > self.max_cost_per_hour:
                raise BudgetExceededError(
                    f"Cost budget exceeded: ${total_cost:.4f}/"
                    f"${self.max_cost_per_hour:.4f} per hour"
                )

        result = await next_(ctx)

        self._call_log.append((now, ctx.server_id, self.cost_per_call))
        ctx.metadata["budget_calls_remaining"] = self._remaining_calls(ctx.server_id)
        ctx.metadata["budget_cost_total"] = round(
            sum(c for _, _, c in self._call_log), 6,
        )

        return result

    def _purge_old_entries(self, now: float) -> None:
        cutoff = now - 3600.0
        while self._call_log and self._call_log[0][0] < cutoff:
            self._call_log.pop(0)
        if not self._call_log:
            self._alerts_fired.clear()

    def _remaining_calls(self, server_id: str) -> dict[str, int | None]:
        total = len(self._call_log)
        srv_calls = sum(1 for _, sid, _ in self._call_log if sid == server_id)
        return {
            "global": (self.max_calls_per_hour - total) if self.max_calls_per_hour > 0 else None,
            "server": (
                (self.server_limits[server_id] - srv_calls)
                if server_id in self.server_limits else None
            ),
        }

    def _check_alert(self, scope: str, current: int, limit: int) -> None:
        if scope in self._alerts_fired:
            return
        ratio = current / limit if limit > 0 else 0
        if ratio >= self.alert_threshold:
            self._alerts_fired.add(scope)
            msg = (
                f"Budget alert ({scope}): {current}/{limit} calls/hour "
                f"({ratio:.0%} used)"
            )
            logger.warning("mcp_budget_alert %s", msg)
            if self._alert_callback:
                try:
                    self._alert_callback("budget_alert", msg, self.stats)
                except Exception as exc:
                    logger.debug("middleware best-effort block failed: %s", exc)

    @property
    def stats(self) -> dict[str, Any]:
        """Current budget usage statistics."""
        total = len(self._call_log)
        per_server: dict[str, int] = {}
        total_cost = 0.0
        for _, sid, cost in self._call_log:
            per_server[sid] = per_server.get(sid, 0) + 1
            total_cost += cost
        return {
            "total_calls_this_hour": total,
            "max_calls_per_hour": self.max_calls_per_hour or "unlimited",
            "per_server": per_server,
            "server_limits": self.server_limits,
            "total_cost": round(total_cost, 6),
            "max_cost_per_hour": self.max_cost_per_hour or "unlimited",
        }

class AutoHealMiddleware:
    """Suggest alternative tools when a call fails."""

    def __init__(
        self,
        max_suggestions: int = 3,
        include_cross_server: bool = True,
    ) -> None:
        self.max_suggestions = max_suggestions
        self.include_cross_server = include_cross_server
        self._tool_resolver: Any | None = None
        self._heal_count: int = 0

    def set_tool_resolver(self, resolver: Any) -> None:
        """Set a callback: fn(server_id, tool_name) -> list[(sid, name, desc)]."""
        self._tool_resolver = resolver

    async def __call__(self, ctx: MCPCallContext, next_: Any) -> Any:
        try:
            result = await next_(ctx)
        except Exception:
            raise

        if not getattr(result, "is_error", False):
            return result

        if self._tool_resolver is None:
            return result

        self._heal_count += 1
        try:
            suggestions = self._tool_resolver(ctx.server_id, ctx.tool_name)
        except Exception:
            return result

        if not suggestions:
            return result

        same_server = [
            s for s in suggestions if s[0] == ctx.server_id
        ][:self.max_suggestions]
        cross_server = []
        if self.include_cross_server and len(same_server) < self.max_suggestions:
            remaining = self.max_suggestions - len(same_server)
            cross_server = [
                s for s in suggestions if s[0] != ctx.server_id
            ][:remaining]

        all_suggestions = same_server + cross_server
        if not all_suggestions:
            return result

        hint_lines = ["\n\n💡 Suggested alternatives:"]
        for sid, tname, desc in all_suggestions:
            prefix = f"[{sid}] " if sid != ctx.server_id else ""
            hint_lines.append(f"  • {prefix}{tname}: {desc}")

        original_text = getattr(result, "text", "") or ""
        result.text = original_text + "\n".join(hint_lines)

        ctx.metadata["auto_heal"] = {
            "suggestions": [
                {"server": s[0], "tool": s[1], "description": s[2]}
                for s in all_suggestions
            ],
        }

        return result

    @property
    def stats(self) -> dict[str, int]:
        return {"heal_attempts": self._heal_count}

class BudgetExceededError(Exception):
    """Raised when an MCP call would exceed the configured budget."""

class CrossServerContextMiddleware:
    """Share context between MCP servers automatically."""

    def __init__(
        self,
        max_entries: int = 20,
        include_servers: list[str] | None = None,
        exclude_servers: list[str] | None = None,
        summary_max_chars: int = 500,
    ) -> None:
        self.max_entries = max(1, max_entries)
        self.include_servers = set(include_servers) if include_servers else None
        self.exclude_servers = set(exclude_servers) if exclude_servers else set()
        self.summary_max_chars = summary_max_chars
        self._entries: list[dict[str, Any]] = []

    async def __call__(self, ctx: MCPCallContext, next_: Any) -> Any:
        if self._entries:
            ctx.metadata["recent_context"] = list(self._entries)

        result = await next_(ctx)

        if self._should_track(ctx.server_id):
            self._record(ctx, result)

        return result

    def _should_track(self, server_id: str) -> bool:
        if server_id in self.exclude_servers:
            return False
        if self.include_servers is not None:
            return server_id in self.include_servers
        return True

    def _record(self, ctx: MCPCallContext, result: Any) -> None:
        output = ""
        if hasattr(result, "text") and result.text:
            output = result.text
        elif hasattr(result, "content") and result.content:
            texts = []
            for item in result.content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
                elif hasattr(item, "text"):
                    texts.append(item.text or "")
            output = "\n".join(texts)

        if len(output) > self.summary_max_chars:
            output = output[:self.summary_max_chars] + "…"

        entry = {
            "server": ctx.server_id,
            "tool": ctx.tool_name,
            "output_summary": output,
            "timestamp": time.monotonic(),
        }

        self._entries.append(entry)
        while len(self._entries) > self.max_entries:
            self._entries.pop(0)

    @property
    def entries(self) -> list[dict[str, Any]]:
        return list(self._entries)

    def clear(self) -> None:
        self._entries.clear()

class SemanticCacheMiddleware:
    """Cache MCP results by semantic similarity, not exact match."""

    def __init__(
        self,
        similarity_threshold: float = 0.85,
        ttl: float = 300.0,
        max_entries: int = 100,
    ) -> None:
        self.similarity_threshold = similarity_threshold
        self.ttl = ttl
        self.max_entries = max_entries
        self._entries: list[tuple[Any, str, str, Any, float]] = []
        self._hits: int = 0
        self._misses: int = 0
        self._model_loaded: bool = False

    def _get_embedding(self, text: str) -> Any:
        try:
            from digitorn.modules.context_builder.embeddings import _get_model
            import numpy as np
            model = _get_model()
            embeddings = list(model.embed([text]))
            self._model_loaded = True
            return np.array(embeddings[0])
        except Exception as exc:
            logger.debug("semantic_cache embedding error: %s", exc)
            return None

    def _cosine_similarity(self, a: Any, b: Any) -> float:
        import numpy as np
        dot = np.dot(a, b)
        norm = np.linalg.norm(a) * np.linalg.norm(b)
        return float(dot / norm) if norm > 0 else 0.0

    def _build_query(self, ctx: MCPCallContext) -> str:
        parts = [ctx.tool_name.replace("_", " ")]
        for k, v in ctx.params.items():
            if isinstance(v, str) and len(v) < 200:
                parts.append(f"{k}: {v}")
            elif isinstance(v, (int, float, bool)):
                parts.append(f"{k}={v}")
        return " ".join(parts)

    async def __call__(self, ctx: MCPCallContext, next_: Any) -> Any:
        from digitorn.modules.context_builder.builder import _infer_mcp_tool_risk
        risk = _infer_mcp_tool_risk(ctx.tool_name)

        if risk != "low":
            before = len(self._entries)
            self._entries = [
                e for e in self._entries if e[2] != ctx.server_id
            ]
            if len(self._entries) < before:
                logger.debug(
                    "semantic_cache_invalidated server=%s cleared=%d",
                    ctx.server_id, before - len(self._entries),
                )
            return await next_(ctx)

        query = self._build_query(ctx)
        embedding = self._get_embedding(query)

        if embedding is not None:
            now = time.monotonic()
            best_sim = 0.0
            best_result = None
            for emb, tool, sid, result, ts in self._entries:
                if now - ts > self.ttl:
                    continue
                if sid != ctx.server_id:
                    continue
                sim = self._cosine_similarity(embedding, emb)
                if sim > best_sim:
                    best_sim = sim
                    best_result = (result, sim)

            if best_result is not None and best_sim >= self.similarity_threshold:
                self._hits += 1
                ctx.metadata["semantic_cache_hit"] = True
                ctx.metadata["semantic_similarity"] = round(best_sim, 3)
                logger.debug(
                    "semantic_cache_hit server=%s tool=%s sim=%.3f",
                    ctx.server_id, ctx.tool_name, best_sim,
                )
                return best_result[0]

        self._misses += 1

        result = await next_(ctx)

        if embedding is not None:
            self._entries.append(
                (embedding, ctx.tool_name, ctx.server_id, result, time.monotonic())
            )
            now = time.monotonic()
            self._entries = [
                e for e in self._entries if now - e[4] <= self.ttl
            ]
            if len(self._entries) > self.max_entries:
                self._entries = self._entries[-self.max_entries:]

            ctx.metadata["semantic_cache_hit"] = False
            ctx.metadata["semantic_cache_stored"] = True

        return result

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "entries": len(self._entries),
            "model_loaded": self._model_loaded,
        }

class CircuitBreakerMiddleware:
    """Protect against cascading failures from unhealthy MCP servers."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
        half_open_calls: int = 1,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_calls = half_open_calls
        self._states: dict[str, str] = {}
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._half_open_ok: dict[str, int] = {}

    async def __call__(self, ctx: MCPCallContext, next_: Any) -> Any:
        sid = ctx.server_id
        state = self._states.get(sid, self.CLOSED)

        if state == self.OPEN:
            elapsed = time.monotonic() - self._opened_at.get(sid, 0)
            if elapsed < self.recovery_timeout:
                raise CircuitOpenError(
                    f"Circuit breaker OPEN for server '{sid}' - "
                    f"retrying in {self.recovery_timeout - elapsed:.0f}s"
                )
            self._states[sid] = self.HALF_OPEN
            self._half_open_ok[sid] = 0
            state = self.HALF_OPEN
            logger.info("circuit_breaker half_open server=%s", sid)

        try:
            result = await next_(ctx)
            if state == self.HALF_OPEN:
                self._half_open_ok[sid] = self._half_open_ok.get(sid, 0) + 1
                if self._half_open_ok[sid] >= self.half_open_calls:
                    self._states[sid] = self.CLOSED
                    self._failures[sid] = 0
                    logger.info("circuit_breaker closed server=%s", sid)
            else:
                self._failures[sid] = 0
            return result
        except Exception as exc:
            self._failures[sid] = self._failures.get(sid, 0) + 1
            if self._failures[sid] >= self.failure_threshold:
                self._states[sid] = self.OPEN
                self._opened_at[sid] = time.monotonic()
                logger.warning(
                    "circuit_breaker OPEN server=%s failures=%d",
                    sid, self._failures[sid],
                )
            raise

    def get_state(self, server_id: str) -> str:
        return self._states.get(server_id, self.CLOSED)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "states": dict(self._states),
            "failures": dict(self._failures),
        }

class CircuitOpenError(Exception):
    """Raised when a call is blocked by an open circuit breaker."""

class DeduplicationMiddleware:
    """Prevent duplicate calls within the same agent turn."""

    def __init__(
        self,
        window_seconds: float = 5.0,
        max_entries: int = 50,
    ) -> None:
        self.window_seconds = window_seconds
        self.max_entries = max_entries
        self._cache: dict[str, tuple[Any, float]] = {}
        self._dedup_count: int = 0

    async def __call__(self, ctx: MCPCallContext, next_: Any) -> Any:
        import hashlib
        import json as _json

        key_data = _json.dumps(
            {"t": ctx.tool_name, "p": ctx.params},
            sort_keys=True, default=str,
        )
        key = hashlib.sha256(key_data.encode()).hexdigest()[:16]

        now = time.monotonic()

        if key in self._cache:
            cached_result, cached_at = self._cache[key]
            if now - cached_at < self.window_seconds:
                self._dedup_count += 1
                ctx.metadata["deduplicated"] = True
                logger.debug(
                    "dedup_hit server=%s tool=%s age=%.1fs",
                    ctx.server_id, ctx.tool_name, now - cached_at,
                )
                return cached_result

        result = await next_(ctx)

        self._cache[key] = (result, now)

        if len(self._cache) > self.max_entries:
            oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]

        return result

    @property
    def stats(self) -> dict[str, int]:
        return {"deduplicated_calls": self._dedup_count}

class StreamingMiddleware:
    """Enable progressive result streaming for long MCP operations."""

    def __init__(
        self,
        slow_threshold: float = 5.0,
        notify_interval: float = 2.0,
    ) -> None:
        self.slow_threshold = slow_threshold
        self.notify_interval = notify_interval
        self._slow_calls: int = 0
        self._total_time_ms: float = 0.0
        self._progress_callback: Any | None = None

    def set_progress_callback(self, callback: Any) -> None:
        """Set callback: fn(server_id, tool_name, elapsed_seconds)."""
        self._progress_callback = callback

    async def __call__(self, ctx: MCPCallContext, next_: Any) -> Any:
        start = time.monotonic()

        done = asyncio.Event()
        progress_task = None

        if self._progress_callback is not None:
            async def _notify_progress():
                elapsed = 0.0
                while not done.is_set():
                    try:
                        await asyncio.wait_for(
                            done.wait(), timeout=self.notify_interval,
                        )
                    except asyncio.TimeoutError:
                        elapsed = time.monotonic() - start
                        try:
                            self._progress_callback(
                                ctx.server_id, ctx.tool_name, elapsed,
                            )
                        except Exception as exc:
                            logger.debug("middleware best-effort block failed: %s", exc)

            progress_task = asyncio.create_task(_notify_progress())

        try:
            result = await next_(ctx)
        finally:
            done.set()
            if progress_task is not None:
                await progress_task

        duration = time.monotonic() - start
        duration_ms = duration * 1000
        self._total_time_ms += duration_ms

        if duration > self.slow_threshold:
            self._slow_calls += 1
            ctx.metadata["slow_call"] = True
            ctx.metadata["duration_seconds"] = round(duration, 1)
            logger.warning(
                "streaming_slow_call server=%s tool=%s duration=%.1fs",
                ctx.server_id, ctx.tool_name, duration,
            )

        return result

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "slow_calls": self._slow_calls,
            "total_time_ms": round(self._total_time_ms),
        }

class MCPPipeline:
    """Compose middlewares into a callable pipeline."""

    def __init__(self, middlewares: list[MCPMiddleware] | None = None) -> None:
        self._middlewares: list[MCPMiddleware] = middlewares or []

    @property
    def middlewares(self) -> list[MCPMiddleware]:
        return self._middlewares

    def add(self, middleware: MCPMiddleware) -> None:
        self._middlewares.append(middleware)

    async def execute(
        self,
        server_id: str,
        tool_name: str,
        params: dict[str, Any],
        call_fn: Any,
    ) -> Any:
        """Run the pipeline and return the raw MCP result."""
        ctx = MCPCallContext(
            server_id=server_id,
            tool_name=tool_name,
            params=params,
        )

        async def terminal(c: MCPCallContext) -> Any:
            return await call_fn(c.server_id, c.tool_name, c.params)

        chain = terminal
        for mw in reversed(self._middlewares):
            chain = _make_link(mw, chain)

        return await chain(ctx)

    def get_middleware(self, cls: type) -> MCPMiddleware | None:
        """Find the first middleware instance of a given type."""
        for mw in self._middlewares:
            if isinstance(mw, cls):
                return mw
        return None

def _make_link(mw: MCPMiddleware, next_fn: Any) -> Any:
    async def link(ctx: MCPCallContext) -> Any:
        return await mw(ctx, next_fn)
    return link

_MIDDLEWARE_REGISTRY: dict[str, type] = {
    "retry": RetryMiddleware,
    "timeout": TimeoutMiddleware,
    "audit": AuditMiddleware,
    "transform": TransformMiddleware,
    "budget": BudgetMiddleware,
    "context": CrossServerContextMiddleware,
    "auto_heal": AutoHealMiddleware,
    "circuit_breaker": CircuitBreakerMiddleware,
    "semantic_cache": SemanticCacheMiddleware,
    "dedup": DeduplicationMiddleware,
    "streaming": StreamingMiddleware,
}

def build_pipeline(config_list: list[dict[str, Any]] | None) -> MCPPipeline | None:
    """Build a pipeline from YAML middleware config."""
    if not config_list:
        return None

    middlewares: list[MCPMiddleware] = []
    for entry in config_list:
        if isinstance(entry, str):
            entry = {entry: {}}
        if not isinstance(entry, dict):
            logger.warning("mcp_middleware_invalid_entry: %s", entry)
            continue

        for name, kwargs in entry.items():
            cls = _MIDDLEWARE_REGISTRY.get(name)
            if cls is None:
                logger.warning("mcp_middleware_unknown: %s", name)
                continue
            if kwargs is None:
                kwargs = {}
            try:
                middlewares.append(cls(**kwargs))
            except TypeError as exc:
                logger.warning(
                    "mcp_middleware_config_error name=%s: %s", name, exc,
                )

    if not middlewares:
        return None

    return MCPPipeline(middlewares)

"""Per-session real-time metrics - isolated, detailed, streamable.

Each session gets its own SessionMetrics instance that tracks everything:
tokens, latencies, tool calls, context usage, memory state, errors.

Metrics are:
- Updated in-place (cheap, no DB writes on hot path)
- Snapshotable at any time (JSON dict for API/SSE/Socket.IO)
- Publishable to MetricsCollector for Prometheus aggregation
- Isolated per session (no cross-session interference)

Integration: created in agent_loop, updated via callbacks, exposed via API.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Shared tokenizer - accurate when tiktoken is installed, char-based
# fallback otherwise. Never raises. The encoding cl100k_base matches
# GPT-4/3.5 and is close enough for Claude/Anthropic and most open models
# (Ollama, DeepSeek) - better than `len(s) // 4` for code, JSON, and CJK.
_TIKTOKEN_ENC: Any | None = None
_TIKTOKEN_TRIED = False


def _get_tiktoken_enc() -> Any | None:
    global _TIKTOKEN_ENC, _TIKTOKEN_TRIED
    if _TIKTOKEN_TRIED:
        return _TIKTOKEN_ENC
    _TIKTOKEN_TRIED = True
    try:
        import tiktoken  # type: ignore
        _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _TIKTOKEN_ENC = None
        logger.debug("tiktoken unavailable - falling back to char/4 heuristic")
    return _TIKTOKEN_ENC


def _count_tokens(text: str) -> int:
    """Count tokens with tiktoken cl100k_base, or char/4 fallback."""
    if not text:
        return 0
    enc = _get_tiktoken_enc()
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:
            pass
    return len(text) // 4


@dataclass
class ContextBreakdown:
    """Detailed context window usage breakdown."""

    max_tokens: int = 128_000
    output_reserved: int = 4096
    effective_max: int = 123_904
    system_prompt_tokens: int = 0
    tools_schema_tokens: int = 0
    message_history_tokens: int = 0
    total_estimated_tokens: int = 0
    pressure: float = 0.0
    compactions: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "output_reserved": self.output_reserved,
            "effective_max": self.effective_max,
            "system_prompt_tokens": self.system_prompt_tokens,
            "system_prompt_pct": round(self.system_prompt_tokens / max(self.effective_max, 1) * 100, 1),
            "tools_schema_tokens": self.tools_schema_tokens,
            "tools_schema_pct": round(self.tools_schema_tokens / max(self.effective_max, 1) * 100, 1),
            "message_history_tokens": self.message_history_tokens,
            "message_history_pct": round(self.message_history_tokens / max(self.effective_max, 1) * 100, 1),
            "total_estimated_tokens": self.total_estimated_tokens,
            "pressure": round(self.pressure, 3),
            "available_tokens": max(0, self.effective_max - self.total_estimated_tokens),
            "compactions": self.compactions,
        }


@dataclass
class ToolMetrics:
    """Per-tool execution metrics."""

    name: str
    calls: int = 0
    successes: int = 0
    failures: int = 0
    total_duration_ms: float = 0.0
    last_duration_ms: float = 0.0
    last_error: str = ""

    @property
    def avg_duration_ms(self) -> float:
        return self.total_duration_ms / max(self.calls, 1)

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "calls": self.calls,
            "successes": self.successes,
            "failures": self.failures,
            "avg_duration_ms": round(self.avg_duration_ms, 1),
            "last_duration_ms": round(self.last_duration_ms, 1),
            "last_error": self.last_error,
        }


@dataclass
class SessionMetrics:
    """Complete real-time metrics for a single session."""

    app_id: str
    session_id: str
    agent_id: str = "main"
    user_id: str = ""
    channel: str = "web"
    model: str = ""
    provider: str = ""

    # Lifecycle
    status: str = "active"
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)

    # Turns
    turn: int = 0
    max_turns: int = 50

    # Tokens
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    # LLM latency
    llm_calls: int = 0
    llm_total_ms: float = 0.0
    llm_last_ms: float = 0.0

    # Tool calls
    tool_calls_total: int = 0
    tool_calls_success: int = 0
    tool_calls_failed: int = 0
    tool_metrics: dict[str, ToolMetrics] = field(default_factory=dict)

    # Context
    context: ContextBreakdown = field(default_factory=ContextBreakdown)

    # Memory
    memory_goal: str = ""
    memory_facts_count: int = 0
    memory_todos_count: int = 0
    memory_todos_done: int = 0

    # Errors
    errors: int = 0
    last_error: str = ""
    retries: int = 0

    # ── Update methods (called from agent_loop callbacks) ────────

    def record_llm_call(self, duration_ms: float, prompt_tokens: int, completion_tokens: int) -> None:
        self.llm_calls += 1
        self.llm_total_ms += duration_ms
        self.llm_last_ms = duration_ms
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.total_tokens = self.prompt_tokens + self.completion_tokens
        self.last_active_at = time.time()

    def record_tool_call(self, name: str, duration_ms: float, success: bool, error: str = "") -> None:
        self.tool_calls_total += 1
        if success:
            self.tool_calls_success += 1
        else:
            self.tool_calls_failed += 1

        if name not in self.tool_metrics:
            self.tool_metrics[name] = ToolMetrics(name=name)

        tm = self.tool_metrics[name]
        tm.calls += 1
        tm.total_duration_ms += duration_ms
        tm.last_duration_ms = duration_ms
        if success:
            tm.successes += 1
        else:
            tm.failures += 1
            tm.last_error = error

        self.last_active_at = time.time()

    def record_turn(self, turn: int) -> None:
        self.turn = turn
        self.last_active_at = time.time()

    def record_error(self, error: str) -> None:
        self.errors += 1
        self.last_error = error

    def record_retry(self) -> None:
        self.retries += 1

    def update_context(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str = "",
        tools: list | None = None,
        config: Any = None,
        *,
        native_tool_use: bool = True,
    ) -> None:
        """Recalculate context breakdown from current state."""
        ctx = self.context

        if config:
            ctx.max_tokens = getattr(config, "max_tokens", 128_000)
            ctx.output_reserved = getattr(config, "output_reserved", 4096)
            ctx.effective_max = ctx.max_tokens - ctx.output_reserved

        # Guard: effective_max must be positive and reasonable
        if ctx.effective_max <= 0:
            ctx.effective_max = max(ctx.max_tokens - ctx.output_reserved, 1)

        # System prompt tokens - prefer tiktoken (cl100k_base) when available
        ctx.system_prompt_tokens = _count_tokens(system_prompt) if system_prompt else 0

        # Tools schema tokens - count the actual JSON payload, not a flat
        # per-tool estimate. A realistic tool schema (name + description +
        # params) is 150-500 tokens; 90 was grossly low for anything
        # beyond a trivial tool.
        #
        # When native_tool_use is False (provider doesn't support OpenAI
        # tool calling - e.g. Ollama qwen2.5-7b), the context_builder
        # renders the tool descriptions INTO the system prompt text. The
        # provider then receives NO `tools` field on the wire. In that
        # case, counting tools_schema_tokens separately would double-count:
        # the cost is already in system_prompt_tokens. So we zero it out.
        import json as _json
        if tools and native_tool_use:
            tools_payload = _json.dumps(tools, ensure_ascii=False, default=str)
            # Add ~4 token overhead per tool for the wrapper fields the provider
            # injects (type: "function", strict flag, etc.) - a small constant
            # bounded by the tool count.
            ctx.tools_schema_tokens = _count_tokens(tools_payload) + 4 * len(tools)
        else:
            ctx.tools_schema_tokens = 0

        # Message history tokens - SKIP role=="system" messages: the system
        # prompt is already accounted for separately above, including it again
        # from session.messages would double-count and inflate the breakdown
        # (observed empirically: a 7k system prompt appeared as 14k in UI).
        msg_text_total = ""
        tool_call_payload = ""
        tool_result_count = 0
        for m in messages:
            if m.get("role") == "system":
                continue
            content = m.get("content", "")
            if isinstance(content, str):
                msg_text_total += content + "\n"
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        msg_text_total += (part.get("text", "") or "") + "\n"
            if m.get("tool_calls"):
                tool_call_payload += _json.dumps(m["tool_calls"], ensure_ascii=False, default=str)
            if m.get("role") == "tool":
                tool_result_count += 1
        ctx.message_history_tokens = (
            _count_tokens(msg_text_total)
            + _count_tokens(tool_call_payload)
            # small fixed overhead per tool result: role/tool_call_id wrappers
            + tool_result_count * 8
        )

        ctx.total_estimated_tokens = (
            ctx.system_prompt_tokens + ctx.tools_schema_tokens + ctx.message_history_tokens
        )
        ctx.pressure = min(ctx.total_estimated_tokens / max(ctx.effective_max, 1), 1.0)

    def update_memory(self, memory_module: Any) -> None:
        if not memory_module:
            return
        store = getattr(memory_module, "store", None)
        if not store:
            return
        working = getattr(store, "working", None)
        if working:
            self.memory_goal = getattr(working, "goal", "") or ""
            self.memory_facts_count = len(getattr(working, "key_facts", []))
            todos = getattr(working, "todos", [])
            self.memory_todos_count = len(todos)
            self.memory_todos_done = sum(1 for t in todos if getattr(t, "done", False))

    # ── Snapshot (JSON-safe dict for API/SSE) ────────────────────

    def snapshot(self) -> dict[str, Any]:
        """Complete metrics snapshot - suitable for JSON serialization."""
        now = time.time()
        return {
            "identity": {
                "app_id": self.app_id,
                "session_id": self.session_id,
                "agent_id": self.agent_id,
                "user_id": self.user_id,
                "channel": self.channel,
                "model": self.model,
                "provider": self.provider,
            },
            "lifecycle": {
                "status": self.status,
                "created_at": self.created_at,
                "last_active_at": self.last_active_at,
                "duration_s": round(now - self.created_at, 1),
                "idle_s": round(now - self.last_active_at, 1),
            },
            "turns": {
                "current": self.turn,
                "max": self.max_turns,
            },
            "tokens": {
                "prompt": self.prompt_tokens,
                "completion": self.completion_tokens,
                "total": self.total_tokens,
            },
            "llm": {
                "calls": self.llm_calls,
                "avg_latency_ms": round(self.llm_total_ms / max(self.llm_calls, 1), 1),
                "last_latency_ms": round(self.llm_last_ms, 1),
                "total_latency_ms": round(self.llm_total_ms, 1),
            },
            "tools": {
                "total": self.tool_calls_total,
                "success": self.tool_calls_success,
                "failed": self.tool_calls_failed,
                "by_tool": {
                    name: tm.snapshot()
                    for name, tm in sorted(self.tool_metrics.items(), key=lambda x: -x[1].calls)
                },
            },
            "context": self.context.snapshot(),
            "memory": {
                "goal": self.memory_goal,
                "facts": self.memory_facts_count,
                "todos": self.memory_todos_count,
                "todos_done": self.memory_todos_done,
            },
            "errors": {
                "total": self.errors,
                "last": self.last_error,
                "retries": self.retries,
            },
        }

    # ── Prometheus metrics emission ──────────────────────────────

    def emit_to_collector(self) -> None:
        """Push metrics to the global MetricsCollector for Prometheus."""
        try:
            from digitorn.core.metrics import get_metrics
            m = get_metrics()
            if m is None:
                return

            labels = {"app_id": self.app_id, "agent_id": self.agent_id, "model": self.model}

            m.set_gauge("session_turn", self.turn, **labels, session_id=self.session_id)
            m.set_gauge("session_tokens_total", self.total_tokens, **labels, session_id=self.session_id)
            m.set_gauge("session_context_pressure", self.context.pressure, **labels, session_id=self.session_id)

            if self.llm_last_ms > 0:
                m.observe("llm_latency_seconds", self.llm_last_ms / 1000, **labels)

            for name, tm in self.tool_metrics.items():
                if tm.last_duration_ms > 0:
                    m.observe("tool_execution_seconds", tm.last_duration_ms / 1000, tool=name, **labels)
        except Exception:
            pass


# ── Registry: one SessionMetrics per active session ─────────────────


_sessions: dict[str, SessionMetrics] = {}


def get_session_metrics(app_id: str, session_id: str, agent_id: str = "main") -> SessionMetrics:
    """Get or create metrics for a session."""
    key = f"{app_id}:{session_id}:{agent_id}"
    if key not in _sessions:
        _sessions[key] = SessionMetrics(app_id=app_id, session_id=session_id, agent_id=agent_id)
    return _sessions[key]


def remove_session_metrics(app_id: str, session_id: str, agent_id: str = "main") -> None:
    key = f"{app_id}:{session_id}:{agent_id}"
    _sessions.pop(key, None)


def list_active_metrics(app_id: str | None = None) -> list[dict[str, Any]]:
    """List all active session metrics (optionally filtered by app_id)."""
    result = []
    for sm in _sessions.values():
        if app_id and sm.app_id != app_id:
            continue
        result.append(sm.snapshot())
    return result


def app_summary(app_id: str) -> dict[str, Any]:
    """Aggregate metrics for an entire app."""
    sessions = [sm for sm in _sessions.values() if sm.app_id == app_id]
    if not sessions:
        return {"app_id": app_id, "sessions": 0}

    total_tokens = sum(s.total_tokens for s in sessions)
    total_tools = sum(s.tool_calls_total for s in sessions)
    total_errors = sum(s.errors for s in sessions)
    llm_latencies = [s.llm_last_ms for s in sessions if s.llm_last_ms > 0]

    return {
        "app_id": app_id,
        "sessions_active": len(sessions),
        "tokens_total": total_tokens,
        "tokens_prompt": sum(s.prompt_tokens for s in sessions),
        "tokens_completion": sum(s.completion_tokens for s in sessions),
        "tool_calls_total": total_tools,
        "tool_calls_failed": sum(s.tool_calls_failed for s in sessions),
        "errors_total": total_errors,
        "llm_latency_avg_ms": round(sum(llm_latencies) / max(len(llm_latencies), 1), 1),
        "llm_latency_p95_ms": round(sorted(llm_latencies)[int(len(llm_latencies) * 0.95)] if llm_latencies else 0, 1),
    }


def global_summary() -> dict[str, Any]:
    """Aggregate metrics across ALL active sessions and apps."""
    all_sessions = list(_sessions.values())
    if not all_sessions:
        return {"apps": 0, "sessions": 0, "tokens_total": 0}

    app_ids = {s.app_id for s in all_sessions}
    total_tokens = sum(s.total_tokens for s in all_sessions)
    total_prompt = sum(s.prompt_tokens for s in all_sessions)
    total_completion = sum(s.completion_tokens for s in all_sessions)
    total_tools = sum(s.tool_calls_total for s in all_sessions)
    total_errors = sum(s.errors for s in all_sessions)

    return {
        "apps": len(app_ids),
        "sessions_active": len(all_sessions),
        "tokens_total": total_tokens,
        "tokens_prompt": total_prompt,
        "tokens_completion": total_completion,
        "tool_calls_total": total_tools,
        "tool_calls_failed": sum(s.tool_calls_failed for s in all_sessions),
        "errors_total": total_errors,
    }

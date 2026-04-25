"""Lightweight distributed tracing for Digitorn.

Propagates trace_id + span_id through the request lifecycle:
    API → agent_loop → LLM call → tool execution → response

No external dependency required. Compatible with OpenTelemetry if installed.

Usage::

    from digitorn.core.tracing import Tracer, current_trace

    tracer = Tracer()
    with tracer.span("agent_turn", app_id="my-app") as span:
        span.set("turns", 3)
        with tracer.span("llm_call", model="deepseek") as child:
            ...

    current_trace()  # → {"trace_id": "...", "spans": [...]}
"""

from __future__ import annotations

import contextvars
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator

logger = logging.getLogger(__name__)

_current_trace: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "_current_trace", default=None,
)


@dataclass
class SpanRecord:
    name: str
    span_id: str
    parent_id: str | None
    start: float
    end: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"

    @property
    def duration_ms(self) -> float:
        elapsed = round((self.end - self.start) * 1000, 2)
        # A completed span (end > 0) always reports at least 0.01 ms so that
        # callers can distinguish "completed instantly" from "not finished".
        if self.end > 0 and elapsed == 0.0:
            return 0.01
        return elapsed

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "duration_ms": self.duration_ms,
            "status": self.status,
            **self.attributes,
        }


@dataclass
class TraceContext:
    trace_id: str
    spans: list[SpanRecord] = field(default_factory=list)
    _active_span: SpanRecord | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "total_ms": sum(s.duration_ms for s in self.spans if s.parent_id is None),
            "span_count": len(self.spans),
            "spans": [s.to_dict() for s in self.spans],
        }


class Tracer:
    @staticmethod
    def start_trace(trace_id: str | None = None) -> TraceContext:
        ctx = TraceContext(trace_id=trace_id or uuid.uuid4().hex[:16])
        _current_trace.set(ctx)
        return ctx

    @staticmethod
    def end_trace() -> TraceContext | None:
        ctx = _current_trace.get()
        _current_trace.set(None)
        return ctx

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Generator[SpanRecord, None, None]:
        ctx = _current_trace.get()
        if ctx is None:
            ctx = self.start_trace()

        parent = ctx._active_span
        record = SpanRecord(
            name=name,
            span_id=uuid.uuid4().hex[:12],
            parent_id=parent.span_id if parent else None,
            start=time.perf_counter(),
            attributes=attrs,
        )
        ctx._active_span = record
        try:
            yield record
        except Exception as exc:
            record.status = "error"
            record.attributes["error"] = str(exc)
            raise
        finally:
            record.end = time.perf_counter()
            ctx.spans.append(record)
            ctx._active_span = parent


def current_trace() -> TraceContext | None:
    return _current_trace.get()


tracer = Tracer()

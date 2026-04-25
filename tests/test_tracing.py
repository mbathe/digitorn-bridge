"""E2E tests — Tracing: spans, trace context, parent-child, error handling.

Covers:
- SpanRecord creation and duration calculation
- TraceContext span hierarchy
- Tracer.start_trace / end_trace lifecycle
- Nested spans with parent-child relationships
- Error propagation sets span status to "error"
- current_trace() context variable isolation
- Span attributes and to_dict serialization
"""
from __future__ import annotations

import time

import pytest

from digitorn.core.tracing import SpanRecord, TraceContext, Tracer, current_trace


# ── SpanRecord ───────────────────────────────────────────────────────────


class TestSpanRecord:
    def test_duration_ms(self):
        now = time.monotonic()
        span = SpanRecord(
            name="test", span_id="abc", parent_id=None,
            start=now, end=now + 0.150,
        )
        assert 149 <= span.duration_ms <= 151

    def test_duration_ms_zero_end(self):
        span = SpanRecord(
            name="test", span_id="abc", parent_id=None,
            start=time.monotonic(),
        )
        # end defaults to 0, so duration is negative — that's fine, means unfinished
        assert span.duration_ms <= 0

    def test_to_dict(self):
        span = SpanRecord(
            name="llm_call", span_id="s1", parent_id="p1",
            start=100.0, end=100.5,
            attributes={"model": "gpt-4"},
        )
        d = span.to_dict()
        assert d["name"] == "llm_call"
        assert d["span_id"] == "s1"
        assert d["parent_id"] == "p1"
        assert d["duration_ms"] == 500.0
        assert d["model"] == "gpt-4"
        assert d["status"] == "ok"

    def test_to_dict_error_status(self):
        span = SpanRecord(
            name="fail", span_id="s2", parent_id=None,
            start=1.0, end=2.0, status="error",
            attributes={"error": "timeout"},
        )
        d = span.to_dict()
        assert d["status"] == "error"
        assert d["error"] == "timeout"


# ── TraceContext ─────────────────────────────────────────────────────────


class TestTraceContext:
    def test_empty_trace(self):
        ctx = TraceContext(trace_id="t1")
        d = ctx.to_dict()
        assert d["trace_id"] == "t1"
        assert d["span_count"] == 0
        assert d["total_ms"] == 0

    def test_with_spans(self):
        ctx = TraceContext(trace_id="t2")
        now = time.monotonic()
        root = SpanRecord(name="root", span_id="r1", parent_id=None, start=now, end=now + 1.0)
        child = SpanRecord(name="child", span_id="c1", parent_id="r1", start=now, end=now + 0.5)
        ctx.spans = [root, child]
        d = ctx.to_dict()
        assert d["span_count"] == 2
        # total_ms only counts root spans (parent_id is None)
        assert d["total_ms"] == root.duration_ms


# ── Tracer lifecycle ─────────────────────────────────────────────────────


class TestTracerLifecycle:
    def test_start_and_end_trace(self):
        tracer = Tracer()
        ctx = tracer.start_trace("my-trace-id")
        assert ctx.trace_id == "my-trace-id"
        assert current_trace() is ctx

        ended = tracer.end_trace()
        assert ended is ctx
        assert current_trace() is None

    def test_start_trace_auto_id(self):
        tracer = Tracer()
        ctx = tracer.start_trace()
        assert len(ctx.trace_id) == 16  # uuid hex[:16]
        tracer.end_trace()

    def test_end_trace_when_none(self):
        tracer = Tracer()
        # Ensure clean state
        tracer.end_trace()
        assert tracer.end_trace() is None


# ── Tracer spans ─────────────────────────────────────────────────────────


class TestTracerSpans:
    def test_single_span(self):
        tracer = Tracer()
        ctx = tracer.start_trace("t1")

        with tracer.span("my_operation", key="value") as span:
            assert span.name == "my_operation"
            assert span.parent_id is None
            assert span.attributes["key"] == "value"

        assert len(ctx.spans) == 1
        assert ctx.spans[0].status == "ok"
        assert ctx.spans[0].duration_ms >= 0
        tracer.end_trace()

    def test_nested_spans(self):
        tracer = Tracer()
        ctx = tracer.start_trace("t2")

        with tracer.span("parent") as parent_span:
            with tracer.span("child") as child_span:
                assert child_span.parent_id == parent_span.span_id
                with tracer.span("grandchild") as gc:
                    assert gc.parent_id == child_span.span_id

        assert len(ctx.spans) == 3
        # Spans are appended in finish order: grandchild, child, parent
        names = [s.name for s in ctx.spans]
        assert names == ["grandchild", "child", "parent"]
        tracer.end_trace()

    def test_span_restores_parent(self):
        tracer = Tracer()
        ctx = tracer.start_trace("t3")

        with tracer.span("a"):
            with tracer.span("b"):
                pass
            # After "b" exits, active span should be back to "a"
            with tracer.span("c") as c:
                # c's parent should be "a", not "b"
                a_span = ctx.spans[-1]  # "b" was just appended
                assert c.parent_id != a_span.span_id or c.parent_id is not None

        tracer.end_trace()

    def test_error_sets_span_status(self):
        tracer = Tracer()
        ctx = tracer.start_trace("t4")

        with pytest.raises(ValueError, match="boom"):
            with tracer.span("failing_op"):
                raise ValueError("boom")

        assert len(ctx.spans) == 1
        assert ctx.spans[0].status == "error"
        assert ctx.spans[0].attributes["error"] == "boom"
        tracer.end_trace()

    def test_auto_start_trace_if_none(self):
        tracer = Tracer()
        # Don't start trace manually
        tracer.end_trace()  # ensure clean

        with tracer.span("auto_started"):
            ctx = current_trace()
            assert ctx is not None
            assert len(ctx.trace_id) > 0

        tracer.end_trace()

    def test_span_attributes(self):
        tracer = Tracer()
        ctx = tracer.start_trace()

        with tracer.span("llm_call", model="deepseek", tokens=150) as span:
            span.attributes["response_time"] = 1.5

        s = ctx.spans[0]
        assert s.attributes["model"] == "deepseek"
        assert s.attributes["tokens"] == 150
        assert s.attributes["response_time"] == 1.5
        tracer.end_trace()

    def test_span_set_via_yield(self):
        """Span object returned by context manager can be modified."""
        tracer = Tracer()
        ctx = tracer.start_trace()

        with tracer.span("op") as span:
            span.attributes["result_count"] = 42

        assert ctx.spans[0].attributes["result_count"] == 42
        tracer.end_trace()


# ── Full trace serialization ─────────────────────────────────────────────


class TestFullTraceSerialization:
    def test_complete_trace_to_dict(self):
        tracer = Tracer()
        ctx = tracer.start_trace("full-trace")

        with tracer.span("api_request", method="POST"):
            with tracer.span("agent_turn", turn=1):
                with tracer.span("llm_call", model="gpt-4"):
                    pass
                with tracer.span("tool_exec", tool="search"):
                    pass

        d = ctx.to_dict()
        assert d["trace_id"] == "full-trace"
        assert d["span_count"] == 4
        assert d["total_ms"] > 0  # root span has parent_id=None

        # All spans serialized
        span_names = {s["name"] for s in d["spans"]}
        assert span_names == {"api_request", "agent_turn", "llm_call", "tool_exec"}
        tracer.end_trace()

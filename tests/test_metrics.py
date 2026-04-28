"""E2E tests - MetricsCollector: counters, gauges, histograms, snapshots.

Covers:
- Counter increment with labels
- Gauge set/increment
- Histogram observe + percentile calculations
- Snapshot structure and correctness
- Thread safety (concurrent increments)
- Reset clears all state
- Scoped labels produce separate metric series
"""
from __future__ import annotations

import threading

from digitorn.core.metrics import MetricsCollector, _Histogram


# ── _Histogram unit ──────────────────────────────────────────────────────


class TestHistogram:
    def test_observe_increments_count(self):
        h = _Histogram()
        h.observe(0.5)
        h.observe(1.0)
        assert h.count == 2
        assert h.total == 1.5

    def test_observe_fills_buckets(self):
        h = _Histogram()
        h.observe(0.03)  # fits in 0.05, 0.1, 0.25, ...
        assert h.buckets.get(0.05, 0) >= 1
        assert h.buckets.get(0.01, 0) == 0  # too small

    def test_percentile_empty(self):
        h = _Histogram()
        assert h.percentile(0.5) == 0.0
        assert h.percentile(0.99) == 0.0

    def test_percentile_single_value(self):
        h = _Histogram()
        h.observe(0.5)
        p50 = h.percentile(0.50)
        assert p50 == 0.5  # lands in 0.5 bucket

    def test_percentile_distribution(self):
        h = _Histogram()
        # All fast requests - p50 should be in smallest fitting bucket
        for _ in range(100):
            h.observe(0.02)
        assert h.percentile(0.50) == 0.05  # smallest bucket >= 0.02

        # All slow requests - p50 should be in large bucket
        h2 = _Histogram()
        for _ in range(100):
            h2.observe(8.0)
        assert h2.percentile(0.50) == 10.0  # smallest bucket >= 8.0

    def test_to_dict_empty(self):
        h = _Histogram()
        d = h.to_dict()
        assert d == {"count": 0, "avg": 0, "p50": 0, "p95": 0, "p99": 0}

    def test_to_dict_with_data(self):
        h = _Histogram()
        h.observe(1.0)
        h.observe(2.0)
        d = h.to_dict()
        assert d["count"] == 2
        assert d["avg"] == 1.5
        assert "p50" in d
        assert "p95" in d
        assert "p99" in d


# ── MetricsCollector - counters ──────────────────────────────────────────


class TestMetricsCounters:
    def test_inc_default_delta(self):
        m = MetricsCollector()
        m.inc("requests_total")
        snap = m.snapshot()
        assert snap["counters"]["requests_total"]["__global__"] == 1

    def test_inc_custom_delta(self):
        m = MetricsCollector()
        m.inc("errors", delta=5)
        assert m.snapshot()["counters"]["errors"]["__global__"] == 5

    def test_inc_with_labels(self):
        m = MetricsCollector()
        m.inc("requests_total", app_id="app1")
        m.inc("requests_total", app_id="app2")
        m.inc("requests_total", app_id="app1")
        counters = m.snapshot()["counters"]["requests_total"]
        assert counters["app_id=app1"] == 2
        assert counters["app_id=app2"] == 1

    def test_inc_multiple_labels(self):
        m = MetricsCollector()
        m.inc("api_calls", app_id="myapp", user_id="u1")
        scope = m._scope(app_id="myapp", user_id="u1")
        assert m.snapshot()["counters"]["api_calls"][scope] == 1

    def test_inc_none_label_ignored(self):
        m = MetricsCollector()
        m.inc("test", app_id=None)
        assert m.snapshot()["counters"]["test"]["__global__"] == 1


# ── MetricsCollector - gauges ────────────────────────────────────────────


class TestMetricsGauges:
    def test_set_gauge(self):
        m = MetricsCollector()
        m.set_gauge("active_sessions", 42)
        assert m.snapshot()["gauges"]["active_sessions"]["__global__"] == 42

    def test_set_gauge_overwrites(self):
        m = MetricsCollector()
        m.set_gauge("connections", 10)
        m.set_gauge("connections", 20)
        assert m.snapshot()["gauges"]["connections"]["__global__"] == 20

    def test_inc_gauge(self):
        m = MetricsCollector()
        m.set_gauge("in_flight", 5)
        m.inc_gauge("in_flight", delta=3)
        assert m.snapshot()["gauges"]["in_flight"]["__global__"] == 8

    def test_inc_gauge_negative(self):
        m = MetricsCollector()
        m.set_gauge("queue", 10)
        m.inc_gauge("queue", delta=-4)
        assert m.snapshot()["gauges"]["queue"]["__global__"] == 6

    def test_gauge_with_labels(self):
        m = MetricsCollector()
        m.set_gauge("memory_mb", 100, app_id="app1")
        m.set_gauge("memory_mb", 200, app_id="app2")
        gauges = m.snapshot()["gauges"]["memory_mb"]
        assert gauges["app_id=app1"] == 100
        assert gauges["app_id=app2"] == 200


# ── MetricsCollector - histograms ────────────────────────────────────────


class TestMetricsHistograms:
    def test_observe(self):
        m = MetricsCollector()
        m.observe("latency", 0.5)
        m.observe("latency", 1.0)
        hist = m.snapshot()["histograms"]["latency"]["__global__"]
        assert hist["count"] == 2
        assert hist["avg"] == 0.75

    def test_observe_with_labels(self):
        m = MetricsCollector()
        m.observe("llm_latency", 1.5, app_id="chat")
        m.observe("llm_latency", 0.3, app_id="search")
        hists = m.snapshot()["histograms"]["llm_latency"]
        assert hists["app_id=chat"]["count"] == 1
        assert hists["app_id=search"]["count"] == 1


# ── MetricsCollector - snapshot & reset ──────────────────────────────────


class TestMetricsSnapshotReset:
    def test_snapshot_has_uptime(self):
        m = MetricsCollector()
        snap = m.snapshot()
        assert "uptime_seconds" in snap
        assert snap["uptime_seconds"] >= 0

    def test_snapshot_empty(self):
        m = MetricsCollector()
        snap = m.snapshot()
        assert snap["counters"] == {}
        assert snap["gauges"] == {}
        assert snap["histograms"] == {}

    def test_reset_clears_all(self):
        m = MetricsCollector()
        m.inc("counter1")
        m.set_gauge("gauge1", 10)
        m.observe("hist1", 0.5)
        m.reset()
        snap = m.snapshot()
        assert snap["counters"] == {}
        assert snap["gauges"] == {}
        assert snap["histograms"] == {}


# ── MetricsCollector - thread safety ─────────────────────────────────────


class TestMetricsThreadSafety:
    def test_concurrent_increments(self):
        m = MetricsCollector()
        n_threads = 10
        n_per_thread = 1000

        def worker():
            for _ in range(n_per_thread):
                m.inc("concurrent_counter")

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = m.snapshot()["counters"]["concurrent_counter"]["__global__"]
        assert total == n_threads * n_per_thread

    def test_concurrent_observe(self):
        m = MetricsCollector()

        def worker(val: float):
            for _ in range(100):
                m.observe("latency", val)

        threads = [threading.Thread(target=worker, args=(i * 0.1,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        hist = m.snapshot()["histograms"]["latency"]["__global__"]
        assert hist["count"] == 500


# ── Scope generation ─────────────────────────────────────────────────────


class TestScope:
    def test_global_scope(self):
        assert MetricsCollector._scope() == "__global__"

    def test_single_label(self):
        assert MetricsCollector._scope(app_id="myapp") == "app_id=myapp"

    def test_multiple_labels_sorted(self):
        s1 = MetricsCollector._scope(app_id="x", user_id="y")
        s2 = MetricsCollector._scope(user_id="y", app_id="x")
        assert s1 == s2  # deterministic
        assert s1 == "app_id=x|user_id=y"

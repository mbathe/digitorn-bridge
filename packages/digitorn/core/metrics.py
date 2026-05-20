"""In-process metrics collector for Digitorn daemon.

Collects counters, gauges, and histograms without external dependencies.
Exposes `/api/metrics` as JSON for dashboards and alerting.

Optional Prometheus exposition is handled separately via a thin adapter
if `prometheus_client` is installed.

Usage::

    from digitorn.core.metrics import metrics

    metrics.inc("requests_total", app_id="my-app", user_id="u1")
    metrics.observe("llm_latency_seconds", 1.23, app_id="my-app")
    metrics.set_gauge("active_sessions", 42, app_id="my-app")
    metrics.snapshot()  # → dict
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Histogram:
    count: int = 0
    total: float = 0.0
    buckets: dict[float, int] = field(default_factory=dict)

    _DEFAULT_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        for b in self._DEFAULT_BUCKETS:
            if value <= b:
                self.buckets[b] = self.buckets.get(b, 0) + 1

    def percentile(self, p: float) -> float:
        if self.count == 0:
            return 0.0
        target = self.count * p
        cumulative = 0
        for b in sorted(self.buckets):
            cumulative += self.buckets[b]
            if cumulative >= target:
                return b
        return self._DEFAULT_BUCKETS[-1]

    def to_dict(self) -> dict[str, Any]:
        if self.count == 0:
            return {"count": 0, "avg": 0, "p50": 0, "p95": 0, "p99": 0}
        return {
            "count": self.count,
            "avg": round(self.total / self.count, 4),
            "p50": self.percentile(0.50),
            "p95": self.percentile(0.95),
            "p99": self.percentile(0.99),
        }


_MAX_CARDINALITY = 5000  # Max unique label combos per metric name


class MetricsCollector:
    """Thread-safe in-process metrics store.

    Enforces a per-metric cardinality limit to prevent unbounded memory
    growth in long-running daemons.  When a metric exceeds the limit,
    new label combinations are silently dropped (existing ones keep
    counting).
    """

    def __init__(self, max_cardinality: int = _MAX_CARDINALITY) -> None:
        self._lock = threading.RLock()
        self._max_cardinality = max_cardinality
        self._counters: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._gauges: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._histograms: dict[str, dict[str, _Histogram]] = defaultdict(lambda: defaultdict(_Histogram))
        self._start_time = time.monotonic()
        self._dropped = 0

    @staticmethod
    def _scope(**labels: str | None) -> str:
        parts = [f"{k}={v}" for k, v in sorted(labels.items()) if v]
        return "|".join(parts) if parts else "__global__"

    def _check_cardinality(self, store: dict[str, Any], name: str, scope: str) -> bool:
        """Return True if the scope is allowed (exists or under limit)."""
        scopes = store[name]
        if scope in scopes:
            return True
        if len(scopes) >= self._max_cardinality:
            self._dropped += 1
            return False
        return True

    def inc(self, name: str, delta: int = 1, **labels: str | None) -> None:
        scope = self._scope(**labels)
        with self._lock:
            if self._check_cardinality(self._counters, name, scope):
                self._counters[name][scope] += delta

    def set_gauge(self, name: str, value: float, **labels: str | None) -> None:
        scope = self._scope(**labels)
        with self._lock:
            if self._check_cardinality(self._gauges, name, scope):
                self._gauges[name][scope] = value

    def inc_gauge(self, name: str, delta: float = 1.0, **labels: str | None) -> None:
        scope = self._scope(**labels)
        with self._lock:
            if self._check_cardinality(self._gauges, name, scope):
                self._gauges[name][scope] += delta

    def observe(self, name: str, value: float, **labels: str | None) -> None:
        scope = self._scope(**labels)
        with self._lock:
            if self._check_cardinality(self._histograms, name, scope):
                self._histograms[name][scope].observe(value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "uptime_seconds": round(time.monotonic() - self._start_time, 1),
                "counters": {
                    name: dict(scopes) for name, scopes in self._counters.items()
                },
                "gauges": {
                    name: dict(scopes) for name, scopes in self._gauges.items()
                },
                "histograms": {
                    name: {
                        scope: h.to_dict() for scope, h in scopes.items()
                    }
                    for name, scopes in self._histograms.items()
                },
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()
            self._start_time = time.monotonic()

    @staticmethod
    def _parse_scope(scope: str) -> dict[str, str]:
        """Parse 'k1=v1|k2=v2' scope string back into labels dict."""
        if scope == "__global__":
            return {}
        labels: dict[str, str] = {}
        for part in scope.split("|"):
            if "=" in part:
                k, v = part.split("=", 1)
                labels[k] = v
        return labels

    @staticmethod
    def _labels_str(labels: dict[str, str]) -> str:
        """Format labels as Prometheus label string: {k1="v1",k2="v2"}."""
        if not labels:
            return ""
        inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return "{" + inner + "}"

    def prometheus(self) -> str:
        """Export all metrics in Prometheus text exposition format.

        See: https://prometheus.io/docs/instrumenting/exposition_formats/
        """
        lines: list[str] = []
        with self._lock:
            # Uptime
            uptime = round(time.monotonic() - self._start_time, 1)
            lines.append("# HELP digitorn_uptime_seconds Daemon uptime in seconds.")
            lines.append("# TYPE digitorn_uptime_seconds gauge")
            lines.append(f"digitorn_uptime_seconds {uptime}")

            # Counters
            for name, scopes in self._counters.items():
                pname = f"digitorn_{name}"
                lines.append(f"# TYPE {pname} counter")
                for scope, value in scopes.items():
                    labels = self._labels_str(self._parse_scope(scope))
                    lines.append(f"{pname}{labels} {value}")

            # Gauges
            for name, scopes in self._gauges.items():
                pname = f"digitorn_{name}"
                lines.append(f"# TYPE {pname} gauge")
                for scope, value in scopes.items():
                    labels = self._labels_str(self._parse_scope(scope))
                    lines.append(f"{pname}{labels} {value}")

            # Histograms
            for name, scopes in self._histograms.items():
                pname = f"digitorn_{name}"
                lines.append(f"# TYPE {pname} histogram")
                for scope, h in scopes.items():
                    labels = self._parse_scope(scope)
                    lstr = self._labels_str(labels)
                    # Buckets are already cumulative from observe()
                    for b in sorted(h.buckets):
                        blabels = dict(labels)
                        blabels["le"] = str(b)
                        lines.append(f"{pname}_bucket{self._labels_str(blabels)} {h.buckets[b]}")
                    # +Inf bucket
                    blabels_inf = dict(labels)
                    blabels_inf["le"] = "+Inf"
                    lines.append(f"{pname}_bucket{self._labels_str(blabels_inf)} {h.count}")
                    lines.append(f"{pname}_sum{lstr} {h.total}")
                    lines.append(f"{pname}_count{lstr} {h.count}")

        lines.append("")
        return "\n".join(lines)


metrics = MetricsCollector()

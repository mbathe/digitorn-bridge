"""Tests for JobStore — PersistedWatcher CRUD and notification buffer.

Covers:
- PersistedWatcher serialization (to_dict / from_dict)
- Watcher CRUD (put, get, delete, list, delete_for_app)
- Secondary index maintenance (stale entry cleanup)
- Notification buffer (buffer, drain, count, FIFO eviction, TTL)
- Output channels (LLMNotificationChannel, ChannelRegistry)
"""

from __future__ import annotations

import asyncio
from _test_helpers import run_coro
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from digitorn.core.app.job_store import JobStore, PersistedWatcher


# ---------------------------------------------------------------------------
# In-memory KV backend for testing (no diskcache dependency)
# ---------------------------------------------------------------------------


class MemoryBackend:
    """Minimal in-memory KeyValueBackend for tests."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def set(self, key: str, value: Any, expire: float | None = None) -> None:
        self._store[key] = value

    def delete(self, key: str) -> bool:
        return self._store.pop(key, None) is not None

    def incr(self, key: str, expire: float | None = None) -> int:
        val = self._store.get(key, 0) + 1
        self._store[key] = val
        return val

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend():
    return MemoryBackend()


@pytest.fixture
def store(backend):
    return JobStore(backend=backend, buffer_max=5)


def _make_pw(app_id: str = "app1", watcher_id: str = "w1", **kwargs) -> PersistedWatcher:
    defaults = dict(
        watcher_id=watcher_id,
        app_id=app_id,
        tool_name="http.get",
        params={"url": "https://example.com"},
        interval=30.0,
        label="Test watcher",
        notify_when="on_change",
        notify_config={},
        max_checks=0,
        status="running",
    )
    defaults.update(kwargs)
    return PersistedWatcher(**defaults)


# ---------------------------------------------------------------------------
# PersistedWatcher serialization
# ---------------------------------------------------------------------------


class TestPersistedWatcher:
    def test_to_dict(self):
        pw = _make_pw()
        d = pw.to_dict()
        assert d["watcher_id"] == "w1"
        assert d["app_id"] == "app1"
        assert d["tool_name"] == "http.get"
        assert d["params"] == {"url": "https://example.com"}
        assert d["status"] == "running"
        assert "created_at" in d

    def test_from_dict_roundtrip(self):
        pw = _make_pw(check_count=42, notify_count=3)
        d = pw.to_dict()
        pw2 = PersistedWatcher.from_dict(d)
        assert pw2.watcher_id == pw.watcher_id
        assert pw2.check_count == 42
        assert pw2.notify_count == 3
        assert pw2.tool_name == pw.tool_name

    def test_from_dict_ignores_unknown_fields(self):
        d = _make_pw().to_dict()
        d["unknown_field"] = "should be ignored"
        pw = PersistedWatcher.from_dict(d)
        assert pw.watcher_id == "w1"
        assert not hasattr(pw, "unknown_field")


# ---------------------------------------------------------------------------
# Watcher CRUD
# ---------------------------------------------------------------------------


class TestWatcherCRUD:
    def test_put_and_get(self, store):
        pw = _make_pw()
        store.put_watcher(pw)
        result = store.get_watcher("app1", "w1")
        assert result is not None
        assert result.watcher_id == "w1"
        assert result.tool_name == "http.get"

    def test_get_missing_returns_none(self, store):
        assert store.get_watcher("app1", "nonexistent") is None

    def test_delete(self, store):
        pw = _make_pw()
        store.put_watcher(pw)
        assert store.delete_watcher("app1", "w1") is True
        assert store.get_watcher("app1", "w1") is None

    def test_delete_missing_returns_false(self, store):
        assert store.delete_watcher("app1", "nonexistent") is False

    def test_list_watchers(self, store):
        store.put_watcher(_make_pw(watcher_id="w1"))
        store.put_watcher(_make_pw(watcher_id="w2", label="Second"))
        result = store.list_watchers("app1")
        assert len(result) == 2
        ids = {pw.watcher_id for pw in result}
        assert ids == {"w1", "w2"}

    def test_list_watchers_empty(self, store):
        assert store.list_watchers("app1") == []

    def test_list_watchers_cleans_stale(self, store, backend):
        """If a watcher key is missing but still in the index, list cleans it."""
        store.put_watcher(_make_pw(watcher_id="w1"))
        store.put_watcher(_make_pw(watcher_id="w2"))
        # Manually delete w2's data key (simulate corruption/expiry)
        backend.delete("watcher:app1:w2")
        result = store.list_watchers("app1")
        assert len(result) == 1
        assert result[0].watcher_id == "w1"
        # Index should be cleaned
        idx = backend.get("__watcher_index__app1", set())
        assert "w2" not in idx

    def test_delete_for_app(self, store):
        store.put_watcher(_make_pw(watcher_id="w1"))
        store.put_watcher(_make_pw(watcher_id="w2"))
        store.put_watcher(_make_pw(app_id="app2", watcher_id="w3"))
        count = store.delete_watchers_for_app("app1")
        assert count == 2
        assert store.list_watchers("app1") == []
        # app2 watchers untouched
        assert len(store.list_watchers("app2")) == 1

    def test_update_existing_watcher(self, store):
        pw = _make_pw(check_count=0)
        store.put_watcher(pw)
        pw.check_count = 50
        pw.status = "paused"
        store.put_watcher(pw)
        result = store.get_watcher("app1", "w1")
        assert result.check_count == 50
        assert result.status == "paused"

    def test_multi_app_isolation(self, store):
        store.put_watcher(_make_pw(app_id="a1", watcher_id="w1"))
        store.put_watcher(_make_pw(app_id="a2", watcher_id="w1"))
        r1 = store.get_watcher("a1", "w1")
        r2 = store.get_watcher("a2", "w1")
        assert r1 is not None
        assert r2 is not None
        assert r1.app_id == "a1"
        assert r2.app_id == "a2"


# ---------------------------------------------------------------------------
# Notification buffer
# ---------------------------------------------------------------------------


class TestNotificationBuffer:
    def test_buffer_and_drain(self, store):
        store.buffer_notification("app1", {"type": "watcher", "data": "hello"})
        store.buffer_notification("app1", {"type": "watcher", "data": "world"})
        result = store.drain_buffered("app1")
        assert len(result) == 2
        assert result[0]["data"] == "hello"
        assert result[1]["data"] == "world"
        # Drain again — should be empty
        assert store.drain_buffered("app1") == []

    def test_buffered_count(self, store):
        assert store.buffered_count("app1") == 0
        store.buffer_notification("app1", {"x": 1})
        store.buffer_notification("app1", {"x": 2})
        assert store.buffered_count("app1") == 2

    def test_fifo_eviction(self, store):
        """Buffer max is 5 — oldest entries evicted when full."""
        for i in range(8):
            store.buffer_notification("app1", {"seq": i})
        result = store.drain_buffered("app1")
        assert len(result) == 5
        # Should have the last 5 (seq 3-7)
        seqs = [n["seq"] for n in result]
        assert seqs == [3, 4, 5, 6, 7]

    def test_drain_empty_app(self, store):
        assert store.drain_buffered("nonexistent") == []

    def test_buffered_at_timestamp(self, store):
        store.buffer_notification("app1", {"type": "test"})
        result = store.drain_buffered("app1")
        assert "buffered_at" in result[0]
        assert isinstance(result[0]["buffered_at"], float)

    def test_multi_app_buffers(self, store):
        store.buffer_notification("a1", {"app": "a1"})
        store.buffer_notification("a2", {"app": "a2"})
        r1 = store.drain_buffered("a1")
        r2 = store.drain_buffered("a2")
        assert len(r1) == 1
        assert len(r2) == 1
        assert r1[0]["app"] == "a1"
        assert r2[0]["app"] == "a2"


# ---------------------------------------------------------------------------
# Output channels
# ---------------------------------------------------------------------------


class TestLLMNotificationChannel:
    def test_deliver_to_active_context_builder(self):
        from digitorn.core.app.channels.base import ChannelPayload
        from digitorn.core.app.output_channels import LLMNotificationChannel

        job_store = MagicMock()
        channel = LLMNotificationChannel(job_store=job_store)

        # Fake context_builder with a queue
        cb = MagicMock()
        cb._bg_notifications = asyncio.Queue()
        channel.register_context_builder("app1", cb)

        payload = ChannelPayload(message="test", structured_data={"type": "test"})
        result = run_coro(
            channel.deliver("app1", payload, {})
        )
        assert result.success is True
        cb.push_module_notification.assert_called_once()
        job_store.buffer_notification.assert_not_called()

    def test_deliver_buffers_when_no_cb(self):
        from digitorn.core.app.channels.base import ChannelPayload
        from digitorn.core.app.output_channels import LLMNotificationChannel

        job_store = MagicMock()
        channel = LLMNotificationChannel(job_store=job_store)

        payload = ChannelPayload(message="test", structured_data={"type": "test"})
        result = run_coro(
            channel.deliver("app1", payload, {})
        )
        assert result.success is False
        assert result.buffered is True
        job_store.buffer_notification.assert_called_once()

    def test_unregister_context_builder(self):
        from digitorn.core.app.channels.base import ChannelPayload
        from digitorn.core.app.output_channels import LLMNotificationChannel

        job_store = MagicMock()
        channel = LLMNotificationChannel(job_store=job_store)
        cb = MagicMock()
        cb._bg_notifications = asyncio.Queue()
        channel.register_context_builder("app1", cb)
        channel.unregister_context_builder("app1")

        payload = ChannelPayload(message="test", structured_data={"type": "test"})
        result = run_coro(
            channel.deliver("app1", payload, {})
        )
        assert result.success is False
        assert result.buffered is True
        job_store.buffer_notification.assert_called_once()


class TestChannelRegistry:
    def test_register_and_deliver(self):
        from digitorn.core.app.output_channels import ChannelRegistry, LLMNotificationChannel

        job_store = MagicMock()
        registry = ChannelRegistry()
        channel = LLMNotificationChannel(job_store=job_store)
        registry.register(channel)

        assert registry.list_channels() == ["llm_notification"]
        assert registry.get("llm_notification") is channel

    def test_deliver_unknown_channel_falls_back(self):
        from digitorn.core.app.output_channels import ChannelRegistry, LLMNotificationChannel

        job_store = MagicMock()
        registry = ChannelRegistry()
        registry.register(LLMNotificationChannel(job_store=job_store))

        # "webhook" doesn't exist — should fall back to llm_notification
        result = run_coro(
            registry.deliver("webhook", "app1", {"type": "test"})
        )
        # Buffered because no context_builder registered
        assert result.buffered is True
        job_store.buffer_notification.assert_called_once()

    def test_deliver_no_channels_returns_false(self):
        from digitorn.core.app.output_channels import ChannelRegistry

        registry = ChannelRegistry()
        result = run_coro(
            registry.deliver("llm_notification", "app1", {"type": "test"})
        )
        assert result.success is False


# ---------------------------------------------------------------------------
# ScheduledJob CRUD
# ---------------------------------------------------------------------------

from digitorn.core.app.job_store import ScheduledJob


def _make_job(
    app_id: str = "app1", job_id: str = "j1", **kwargs
) -> ScheduledJob:
    defaults = dict(
        job_id=job_id,
        app_id=app_id,
        schedule_type="once",
        action_type="tool_call",
        tool_name="http.get",
        tool_params={"url": "https://example.com"},
        label="Test job",
        status="active",
        next_run_at="2026-03-13T12:00:00+00:00",
    )
    defaults.update(kwargs)
    return ScheduledJob(**defaults)


class TestScheduledJobSerialization:
    def test_to_dict(self):
        job = _make_job()
        d = job.to_dict()
        assert d["job_id"] == "j1"
        assert d["schedule_type"] == "once"
        assert d["action_type"] == "tool_call"

    def test_from_dict_roundtrip(self):
        job = _make_job(run_count=5, last_error="timeout")
        d = job.to_dict()
        job2 = ScheduledJob.from_dict(d)
        assert job2.job_id == job.job_id
        assert job2.run_count == 5
        assert job2.last_error == "timeout"

    def test_to_summary(self):
        job = _make_job(run_count=3, max_runs=10)
        s = job.to_summary()
        assert s["job_id"] == "j1"
        assert s["run_count"] == 3
        assert s["max_runs"] == 10
        assert "tool_params" not in s  # summary is compact


class TestJobCRUD:
    def test_put_and_get(self, store):
        job = _make_job()
        store.put_job(job)
        result = store.get_job("app1", "j1")
        assert result is not None
        assert result.job_id == "j1"
        assert result.tool_name == "http.get"

    def test_get_missing_returns_none(self, store):
        assert store.get_job("app1", "nonexistent") is None

    def test_delete(self, store):
        store.put_job(_make_job())
        assert store.delete_job("app1", "j1") is True
        assert store.get_job("app1", "j1") is None

    def test_delete_missing(self, store):
        assert store.delete_job("app1", "nonexistent") is False

    def test_list_jobs(self, store):
        store.put_job(_make_job(job_id="j1"))
        store.put_job(_make_job(job_id="j2", label="Second"))
        result = store.list_jobs("app1")
        assert len(result) == 2

    def test_list_jobs_filter_status(self, store):
        store.put_job(_make_job(job_id="j1", status="active"))
        store.put_job(_make_job(job_id="j2", status="completed"))
        active = store.list_jobs("app1", status="active")
        assert len(active) == 1
        assert active[0].job_id == "j1"

    def test_list_all_active_jobs(self, store):
        store.put_job(_make_job(app_id="a1", job_id="j1"))
        store.put_job(_make_job(app_id="a2", job_id="j2"))
        store.put_job(_make_job(app_id="a1", job_id="j3", status="completed"))
        actives = store.list_all_active_jobs()
        assert len(actives) == 2
        ids = {j.job_id for j in actives}
        assert ids == {"j1", "j2"}

    def test_active_index_cleanup(self, store):
        """Completed jobs are removed from the global active index."""
        job = _make_job()
        store.put_job(job)
        assert len(store.list_all_active_jobs()) == 1
        # Mark as completed
        job.status = "completed"
        store.put_job(job)
        assert len(store.list_all_active_jobs()) == 0

    def test_delete_jobs_for_app(self, store):
        store.put_job(_make_job(job_id="j1"))
        store.put_job(_make_job(job_id="j2"))
        store.put_job(_make_job(app_id="app2", job_id="j3"))
        count = store.delete_jobs_for_app("app1")
        assert count == 2
        assert store.list_jobs("app1") == []
        assert len(store.list_jobs("app2")) == 1

    def test_multi_app_isolation(self, store):
        store.put_job(_make_job(app_id="a1", job_id="j1"))
        store.put_job(_make_job(app_id="a2", job_id="j1"))
        r1 = store.get_job("a1", "j1")
        r2 = store.get_job("a2", "j1")
        assert r1.app_id == "a1"
        assert r2.app_id == "a2"


# ---------------------------------------------------------------------------
# Time parser
# ---------------------------------------------------------------------------

from datetime import datetime, timezone
from digitorn.core.app.time_parser import parse_time


class TestTimeParser:
    NOW = datetime(2026, 3, 13, 10, 0, 0, tzinfo=timezone.utc)

    def test_relative_minutes(self):
        r = parse_time("in 5m", now=self.NOW)
        assert r.schedule_type == "once"
        assert "10:05" in r.run_at

    def test_relative_hours(self):
        r = parse_time("in 2h", now=self.NOW)
        assert r.schedule_type == "once"
        assert "12:00" in r.run_at

    def test_relative_french(self):
        r = parse_time("dans 30 minutes", now=self.NOW)
        assert r.schedule_type == "once"
        assert "10:30" in r.run_at

    def test_relative_compound(self):
        r = parse_time("in 1h30m", now=self.NOW)
        assert r.schedule_type == "once"
        assert "11:30" in r.run_at

    def test_absolute_tomorrow(self):
        r = parse_time("tomorrow at 9am", now=self.NOW)
        assert r.schedule_type == "once"
        assert "2026-03-14" in r.run_at
        assert "09:00" in r.run_at

    def test_absolute_french(self):
        r = parse_time("demain à 9h", now=self.NOW)
        assert r.schedule_type == "once"
        assert "2026-03-14" in r.run_at

    def test_iso8601_passthrough(self):
        r = parse_time("2026-03-14T09:00:00Z", now=self.NOW)
        assert r.schedule_type == "once"
        assert "2026-03-14" in r.run_at

    def test_raw_cron(self):
        r = parse_time("0 9 * * *", now=self.NOW)
        assert r.schedule_type == "cron"
        assert r.cron_expr == "0 9 * * *"

    def test_every_day_at(self):
        r = parse_time("every day at 9am", now=self.NOW)
        assert r.schedule_type == "cron"
        assert r.cron_expr == "0 9 * * *"

    def test_every_day_french(self):
        r = parse_time("tous les jours à 9h", now=self.NOW)
        assert r.schedule_type == "cron"
        assert r.cron_expr == "0 9 * * *"

    def test_every_interval(self):
        r = parse_time("every 5 minutes", now=self.NOW)
        assert r.schedule_type == "interval"
        assert r.interval_seconds == 300.0

    def test_every_hour(self):
        r = parse_time("every hour", now=self.NOW)
        assert r.schedule_type == "cron"
        assert r.cron_expr == "0 * * * *"

    def test_every_monday(self):
        r = parse_time("every monday at 10am", now=self.NOW)
        assert r.schedule_type == "cron"
        # Monday = cron day 1
        assert "1" in r.cron_expr

    def test_empty_returns_error(self):
        r = parse_time("", now=self.NOW)
        assert r.error is not None

    def test_unparseable_returns_error(self):
        r = parse_time("blah blah", now=self.NOW)
        assert r.error is not None


# ---------------------------------------------------------------------------
# Cron matching (fallback)
# ---------------------------------------------------------------------------

from digitorn.core.app.scheduler import _cron_matches


class TestCronMatching:
    def test_every_minute(self):
        dt = datetime(2026, 3, 13, 10, 30, tzinfo=timezone.utc)
        assert _cron_matches(["*", "*", "*", "*", "*"], dt)

    def test_specific_minute_hour(self):
        dt = datetime(2026, 3, 13, 9, 0, tzinfo=timezone.utc)
        assert _cron_matches(["0", "9", "*", "*", "*"], dt)
        assert not _cron_matches(["0", "10", "*", "*", "*"], dt)

    def test_range(self):
        dt = datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc)
        assert _cron_matches(["0", "9-12", "*", "*", "*"], dt)
        assert not _cron_matches(["0", "11-12", "*", "*", "*"], dt)

    def test_step(self):
        dt = datetime(2026, 3, 13, 10, 0, tzinfo=timezone.utc)
        assert _cron_matches(["*/5", "*", "*", "*", "*"], dt)  # 0 is divisible by 5
        dt2 = datetime(2026, 3, 13, 10, 3, tzinfo=timezone.utc)
        assert not _cron_matches(["*/5", "*", "*", "*", "*"], dt2)

"""Tests for the source watcher system.

Real integration tests that exercise the full pipeline:
    Watcher backend → SourceWatcherService → EventBus (with routing) → IndexModule

Covers:
    - PollingWatcher with real filesystem operations
    - FilesystemWatcher with real watchfiles/inotify
    - ServiceBusPollingWatcher with real async polling
    - Full pipeline: watcher → LogEventBus → subscriber callback
    - Full pipeline: watcher → EventBus → IndexModule.on_event → index updated
    - Multi-source concurrent watching
    - Persistent watch save/restore cycle
    - Error resilience (network failures, missing files, queue overflow)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from digitorn.core.events.bus import LogEventBus
from digitorn.core.events.models import UniversalEvent
from digitorn.core.watcher.filesystem import FilesystemWatcher
from digitorn.core.watcher.polling import PollingWatcher, _default_file_hash
from digitorn.core.watcher.service import SourceWatcherService
from digitorn.core.watcher.service_bus_poller import ServiceBusPollingWatcher
from digitorn.core.watcher.types import ChangeEvent, ChangeType, WatchConfig, WatchMode
from digitorn.modules.index.module import IndexModule
from digitorn.modules.index.params import RegisterSourceParams, ScanParams
from digitorn.modules.index.types import Source


# ═══════════════════════════════════════════════════════════════════
# Helpers — shared across all test classes
# ═══════════════════════════════════════════════════════════════════


@dataclass
class FakeActionResult:
    """Mimics ActionResult returned by a module via service bus."""

    data: Any


class MockServiceBus:
    """Simulates an inter-module service bus for ServiceBusPollingWatcher tests.

    Exposes ``list_items`` and ``checksum`` actions that return configurable data,
    so we can simulate a real database/mail/S3 module without needing one.
    """

    def __init__(self) -> None:
        self.call_log: list[tuple[str, str, dict[str, Any]]] = []
        self._items: list[dict[str, str]] = []
        self._checksums: dict[str, str | None] = {}

    def set_items(self, items: list[dict[str, str]]) -> None:
        self._items = items

    def set_checksums(self, checksums: dict[str, str | None]) -> None:
        self._checksums = checksums

    async def call(
        self, module_id: str, action: str, params: dict[str, Any],
    ) -> FakeActionResult:
        self.call_log.append((module_id, action, params))
        if action == "list_items":
            return FakeActionResult(data={"items": list(self._items)})
        if action == "checksum":
            item_ids = params.get("items", [])
            return FakeActionResult(
                data={
                    "checksums": [
                        {"id": iid, "hash": self._checksums.get(iid)}
                        for iid in item_ids
                    ],
                },
            )
        return FakeActionResult(data={})


def _make_project(tmp_path: Path) -> Path:
    """Create a small project directory with Python files."""
    (tmp_path / "main.py").write_text("def hello():\n    return 'hello'\n")
    (tmp_path / "utils.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\nbare = false")
    return tmp_path


# ═══════════════════════════════════════════════════════════════════
# 1. PollingWatcher — real filesystem, real hashing, real async
# ═══════════════════════════════════════════════════════════════════


class TestPollingWatcherReal:
    """PollingWatcher with real files and real async polling loops."""

    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        return _make_project(tmp_path)

    def test_file_hash_deterministic(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        h1 = _default_file_hash(str(f))
        h2 = _default_file_hash(str(f))
        assert h1 is not None
        assert h1 == h2
        assert len(h1) == 16

    def test_file_hash_changes_on_write(self, tmp_path: Path) -> None:
        f = tmp_path / "test.txt"
        f.write_text("version 1")
        h1 = _default_file_hash(str(f))
        f.write_text("version 2")
        h2 = _default_file_hash(str(f))
        assert h1 != h2

    def test_file_hash_nonexistent(self) -> None:
        assert _default_file_hash("/nonexistent/path/file.txt") is None

    @pytest.mark.asyncio
    async def test_lifecycle_start_stop(self, project: Path) -> None:
        config = WatchConfig(
            source_id="proj", backend="polling",
            root=str(project), patterns=["*.py"], poll_interval_s=0.1,
        )
        watcher = PollingWatcher(config)

        assert not watcher.running
        assert watcher.source_id == "proj"

        await watcher.start()
        assert watcher.running
        # Initial snapshot: main.py + utils.py (.git ignored by default)
        assert len(watcher._snapshot) == 2
        assert all(".git" not in p for p in watcher._snapshot)

        await watcher.stop()
        assert not watcher.running

    @pytest.mark.asyncio
    async def test_idempotent_start_stop(self, project: Path) -> None:
        config = WatchConfig(
            source_id="proj", backend="polling",
            root=str(project), poll_interval_s=0.1,
        )
        watcher = PollingWatcher(config)
        await watcher.start()
        await watcher.start()  # no-op
        assert watcher.running
        await watcher.stop()
        await watcher.stop()  # no-op
        assert not watcher.running

    @pytest.mark.asyncio
    async def test_detect_file_created(self, project: Path) -> None:
        config = WatchConfig(
            source_id="proj", backend="polling",
            root=str(project), patterns=["*.py"], poll_interval_s=0.1,
        )
        watcher = PollingWatcher(config)
        await watcher.start()

        (project / "new_module.py").write_text("x = 42\n")
        await asyncio.sleep(0.3)

        events = []
        while not watcher._queue.empty():
            events.append(watcher._queue.get_nowait())
        await watcher.stop()

        created = [e for e in events if e.change_type == ChangeType.CREATED]
        assert len(created) == 1
        assert "new_module.py" in created[0].path
        assert created[0].source_id == "proj"

    @pytest.mark.asyncio
    async def test_detect_file_modified(self, project: Path) -> None:
        config = WatchConfig(
            source_id="proj", backend="polling",
            root=str(project), patterns=["*.py"], poll_interval_s=0.1,
        )
        watcher = PollingWatcher(config)
        await watcher.start()
        original_hash = watcher._snapshot[str(project / "main.py")]

        (project / "main.py").write_text("def goodbye(): pass\n")
        await asyncio.sleep(0.3)

        events = []
        while not watcher._queue.empty():
            events.append(watcher._queue.get_nowait())
        await watcher.stop()

        modified = [e for e in events if e.change_type == ChangeType.MODIFIED]
        assert len(modified) == 1
        assert "main.py" in modified[0].path
        assert modified[0].content_hash is not None
        assert modified[0].content_hash != original_hash

    @pytest.mark.asyncio
    async def test_detect_file_deleted(self, project: Path) -> None:
        config = WatchConfig(
            source_id="proj", backend="polling",
            root=str(project), patterns=["*.py"], poll_interval_s=0.1,
        )
        watcher = PollingWatcher(config)
        await watcher.start()

        (project / "utils.py").unlink()
        await asyncio.sleep(0.3)

        events = []
        while not watcher._queue.empty():
            events.append(watcher._queue.get_nowait())
        await watcher.stop()

        deleted = [e for e in events if e.change_type == ChangeType.DELETED]
        assert len(deleted) == 1
        assert "utils.py" in deleted[0].path

    @pytest.mark.asyncio
    async def test_multiple_changes_in_one_cycle(self, project: Path) -> None:
        """Multiple simultaneous changes are all detected in a single poll cycle."""
        config = WatchConfig(
            source_id="proj", backend="polling",
            root=str(project), patterns=["*.py"], poll_interval_s=0.1,
        )
        watcher = PollingWatcher(config)
        await watcher.start()

        # Create, modify, and delete in quick succession
        (project / "new.py").write_text("new = True\n")
        (project / "main.py").write_text("modified = True\n")
        (project / "utils.py").unlink()
        await asyncio.sleep(0.3)

        events = []
        while not watcher._queue.empty():
            events.append(watcher._queue.get_nowait())
        await watcher.stop()

        types = {e.change_type for e in events}
        assert ChangeType.CREATED in types
        assert ChangeType.MODIFIED in types
        assert ChangeType.DELETED in types

    @pytest.mark.asyncio
    async def test_no_events_when_nothing_changes(self, project: Path) -> None:
        config = WatchConfig(
            source_id="proj", backend="polling",
            root=str(project), patterns=["*.py"], poll_interval_s=0.1,
        )
        watcher = PollingWatcher(config)
        await watcher.start()
        await asyncio.sleep(0.3)  # Wait for at least 2 poll cycles

        events = []
        while not watcher._queue.empty():
            events.append(watcher._queue.get_nowait())
        await watcher.stop()

        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_pattern_filtering(self, project: Path) -> None:
        """Only files matching patterns are tracked."""
        config = WatchConfig(
            source_id="proj", backend="polling",
            root=str(project), patterns=["*.py"], poll_interval_s=0.1,
        )
        watcher = PollingWatcher(config)
        await watcher.start()

        # Create files: only .py should be detected
        (project / "readme.md").write_text("# Readme\n")
        (project / "config.yaml").write_text("key: value\n")
        (project / "new.py").write_text("x = 1\n")
        await asyncio.sleep(0.3)

        events = []
        while not watcher._queue.empty():
            events.append(watcher._queue.get_nowait())
        await watcher.stop()

        # Only new.py should be detected
        assert len(events) == 1
        assert "new.py" in events[0].path


# ═══════════════════════════════════════════════════════════════════
# 2. FilesystemWatcher — pattern matching + real inotify
# ═══════════════════════════════════════════════════════════════════


class TestFilesystemWatcherReal:
    """FilesystemWatcher with real watchfiles/inotify events."""

    def test_pattern_matching_include(self, tmp_path: Path) -> None:
        config = WatchConfig(
            source_id="test", root=str(tmp_path),
            patterns=["**/*.py", "**/*.js"], ignore=[".git/**"],
        )
        watcher = FilesystemWatcher(config)

        assert watcher._matches(str(tmp_path / "main.py")) is True
        assert watcher._matches(str(tmp_path / "src" / "app.js")) is True
        assert watcher._matches(str(tmp_path / "readme.md")) is False

    def test_pattern_matching_ignore(self, tmp_path: Path) -> None:
        config = WatchConfig(
            source_id="test", root=str(tmp_path),
            patterns=["**/*"], ignore=[".git/**", "*.pyc", "__pycache__/**"],
        )
        watcher = FilesystemWatcher(config)

        assert watcher._matches(str(tmp_path / ".git" / "config")) is False
        assert watcher._matches(str(tmp_path / "module.pyc")) is False
        assert watcher._matches(str(tmp_path / "__pycache__" / "mod.cpython-312.pyc")) is False
        assert watcher._matches(str(tmp_path / "main.py")) is True

    def test_pattern_matching_outside_root(self, tmp_path: Path) -> None:
        config = WatchConfig(source_id="test", root=str(tmp_path / "subdir"))
        watcher = FilesystemWatcher(config)
        assert watcher._matches("/completely/different/path.py") is False

    @pytest.mark.asyncio
    async def test_inotify_detects_create(self, tmp_path: Path) -> None:
        """Real inotify/fsevents: create a file and verify the event."""
        config = WatchConfig(
            source_id="fs-test", root=str(tmp_path),
            patterns=["**/*.py"], debounce_ms=100,
        )
        watcher = FilesystemWatcher(config)
        await watcher.start()
        assert watcher.running

        # Wait for watchfiles to be ready, then create a file
        await asyncio.sleep(0.2)
        (tmp_path / "hello.py").write_text("print('hello')\n")
        await asyncio.sleep(1.0)  # inotify debounce + propagation

        events = []
        while not watcher._queue.empty():
            events.append(watcher._queue.get_nowait())
        await watcher.stop()

        assert len(events) >= 1
        assert any(
            e.change_type in (ChangeType.CREATED, ChangeType.MODIFIED)
            and "hello.py" in e.path
            for e in events
        )

    @pytest.mark.asyncio
    async def test_inotify_detects_modify(self, tmp_path: Path) -> None:
        """Real inotify: modify an existing file."""
        (tmp_path / "app.py").write_text("v = 1\n")
        await asyncio.sleep(0.1)

        config = WatchConfig(
            source_id="fs-test", root=str(tmp_path),
            patterns=["**/*.py"], debounce_ms=100,
        )
        watcher = FilesystemWatcher(config)
        await watcher.start()
        await asyncio.sleep(0.2)

        (tmp_path / "app.py").write_text("v = 2\n")
        await asyncio.sleep(1.0)

        events = []
        while not watcher._queue.empty():
            events.append(watcher._queue.get_nowait())
        await watcher.stop()

        assert len(events) >= 1
        assert any(e.change_type == ChangeType.MODIFIED and "app.py" in e.path for e in events)

    @pytest.mark.asyncio
    async def test_inotify_detects_delete(self, tmp_path: Path) -> None:
        """Real inotify: delete a file."""
        (tmp_path / "to_delete.py").write_text("delete me\n")
        await asyncio.sleep(0.1)

        config = WatchConfig(
            source_id="fs-test", root=str(tmp_path),
            patterns=["**/*.py"], debounce_ms=100,
        )
        watcher = FilesystemWatcher(config)
        await watcher.start()
        await asyncio.sleep(0.2)

        (tmp_path / "to_delete.py").unlink()
        await asyncio.sleep(1.0)

        events = []
        while not watcher._queue.empty():
            events.append(watcher._queue.get_nowait())
        await watcher.stop()

        assert len(events) >= 1
        assert any(e.change_type == ChangeType.DELETED and "to_delete.py" in e.path for e in events)

    @pytest.mark.asyncio
    async def test_inotify_ignores_non_matching(self, tmp_path: Path) -> None:
        """Real inotify: non-matching files are ignored."""
        config = WatchConfig(
            source_id="fs-test", root=str(tmp_path),
            patterns=["**/*.py"], debounce_ms=100,
        )
        watcher = FilesystemWatcher(config)
        await watcher.start()
        await asyncio.sleep(0.2)

        (tmp_path / "readme.md").write_text("# Doc\n")
        (tmp_path / "data.json").write_text("{}\n")
        await asyncio.sleep(1.0)

        events = []
        while not watcher._queue.empty():
            events.append(watcher._queue.get_nowait())
        await watcher.stop()

        assert len(events) == 0


# ═══════════════════════════════════════════════════════════════════
# 3. ServiceBusPollingWatcher — simulated module with real async
# ═══════════════════════════════════════════════════════════════════


class TestServiceBusPollingWatcherReal:
    """ServiceBusPollingWatcher with a mock service bus but real async polling."""

    @pytest.fixture
    def db_bus(self) -> MockServiceBus:
        bus = MockServiceBus()
        bus.set_items([
            {"id": "users", "path": "public.users"},
            {"id": "orders", "path": "public.orders"},
        ])
        bus.set_checksums({
            "users": "hash_users_v1",
            "orders": "hash_orders_v1",
        })
        return bus

    @pytest.fixture
    def db_config(self) -> WatchConfig:
        return WatchConfig(
            source_id="crm_db",
            backend="service_bus",
            root="postgres://localhost/crm",
            patterns=["public.*"],
            poll_interval_s=0.1,
        )

    @pytest.mark.asyncio
    async def test_lifecycle(self, db_config: WatchConfig, db_bus: MockServiceBus) -> None:
        watcher = ServiceBusPollingWatcher(db_config, db_bus, "database")
        assert not watcher.running

        await watcher.start()
        assert watcher.running
        assert watcher.source_id == "crm_db"
        assert len(watcher._snapshot) == 2
        assert watcher._snapshot["users"] == "hash_users_v1"
        assert watcher._snapshot["orders"] == "hash_orders_v1"

        # Verify list_items and checksum were both called
        actions_called = [action for _, action, _ in db_bus.call_log]
        assert "list_items" in actions_called
        assert "checksum" in actions_called

        await watcher.stop()
        assert not watcher.running

    @pytest.mark.asyncio
    async def test_detect_new_table(self, db_config: WatchConfig, db_bus: MockServiceBus) -> None:
        """Simulates a new DB table appearing."""
        watcher = ServiceBusPollingWatcher(db_config, db_bus, "database")
        await watcher.start()

        db_bus.set_items([
            {"id": "users", "path": "public.users"},
            {"id": "orders", "path": "public.orders"},
            {"id": "products", "path": "public.products"},
        ])
        db_bus.set_checksums({
            "users": "hash_users_v1",
            "orders": "hash_orders_v1",
            "products": "hash_products_v1",
        })
        await asyncio.sleep(0.3)

        events = []
        while not watcher._queue.empty():
            events.append(watcher._queue.get_nowait())
        await watcher.stop()

        created = [e for e in events if e.change_type == ChangeType.CREATED]
        assert len(created) == 1
        assert created[0].path == "products"
        assert created[0].content_hash == "hash_products_v1"
        assert created[0].source_id == "crm_db"

    @pytest.mark.asyncio
    async def test_detect_table_modified(self, db_config: WatchConfig, db_bus: MockServiceBus) -> None:
        """Simulates rows changing in a DB table (checksum changes)."""
        watcher = ServiceBusPollingWatcher(db_config, db_bus, "database")
        await watcher.start()

        db_bus.set_checksums({
            "users": "hash_users_v2",
            "orders": "hash_orders_v1",
        })
        await asyncio.sleep(0.3)

        events = []
        while not watcher._queue.empty():
            events.append(watcher._queue.get_nowait())
        await watcher.stop()

        modified = [e for e in events if e.change_type == ChangeType.MODIFIED]
        assert len(modified) == 1
        assert modified[0].path == "users"
        assert modified[0].content_hash == "hash_users_v2"

    @pytest.mark.asyncio
    async def test_detect_table_dropped(self, db_config: WatchConfig, db_bus: MockServiceBus) -> None:
        """Simulates a DB table being dropped."""
        watcher = ServiceBusPollingWatcher(db_config, db_bus, "database")
        await watcher.start()

        db_bus.set_items([{"id": "users", "path": "public.users"}])
        db_bus.set_checksums({"users": "hash_users_v1"})
        await asyncio.sleep(0.3)

        events = []
        while not watcher._queue.empty():
            events.append(watcher._queue.get_nowait())
        await watcher.stop()

        deleted = [e for e in events if e.change_type == ChangeType.DELETED]
        assert len(deleted) == 1
        assert deleted[0].path == "orders"

    @pytest.mark.asyncio
    async def test_combined_changes(self, db_config: WatchConfig, db_bus: MockServiceBus) -> None:
        """Simulates create + modify + delete in a single poll cycle."""
        watcher = ServiceBusPollingWatcher(db_config, db_bus, "database")
        await watcher.start()

        db_bus.set_items([
            {"id": "users", "path": "public.users"},
            # orders dropped, products added
            {"id": "products", "path": "public.products"},
        ])
        db_bus.set_checksums({
            "users": "hash_users_v2",  # modified
            "products": "hash_products_v1",  # new
        })
        await asyncio.sleep(0.3)

        events = []
        while not watcher._queue.empty():
            events.append(watcher._queue.get_nowait())
        await watcher.stop()

        types = {e.change_type for e in events}
        assert ChangeType.CREATED in types
        assert ChangeType.MODIFIED in types
        assert ChangeType.DELETED in types

    @pytest.mark.asyncio
    async def test_resilience_list_items_failure(
        self, db_config: WatchConfig, db_bus: MockServiceBus,
    ) -> None:
        """When list_items fails (e.g. DB connection lost), snapshot is preserved."""
        watcher = ServiceBusPollingWatcher(db_config, db_bus, "database")
        await watcher.start()
        initial_snapshot = dict(watcher._snapshot)

        # Inject failure
        original_call = db_bus.call

        async def failing_call(
            module_id: str, action: str, params: dict[str, Any],
        ) -> FakeActionResult:
            if action == "list_items":
                raise ConnectionError("connection to postgres lost")
            return await original_call(module_id, action, params)

        db_bus.call = failing_call  # type: ignore[assignment]
        await asyncio.sleep(0.3)

        # No spurious events, snapshot unchanged
        assert watcher._snapshot == initial_snapshot
        events = []
        while not watcher._queue.empty():
            events.append(watcher._queue.get_nowait())
        assert len(events) == 0

        # Restore connection — changes should be detected again
        db_bus.call = original_call  # type: ignore[assignment]
        db_bus.set_checksums({
            "users": "hash_users_v2",
            "orders": "hash_orders_v1",
        })
        await asyncio.sleep(0.3)

        events = []
        while not watcher._queue.empty():
            events.append(watcher._queue.get_nowait())
        await watcher.stop()

        modified = [e for e in events if e.change_type == ChangeType.MODIFIED]
        assert len(modified) >= 1

    @pytest.mark.asyncio
    async def test_async_iterator_interface(
        self, db_config: WatchConfig, db_bus: MockServiceBus,
    ) -> None:
        """Test consuming events via the async for interface."""
        watcher = ServiceBusPollingWatcher(db_config, db_bus, "database")
        await watcher.start()

        db_bus.set_checksums({"users": "hash_users_v2", "orders": "hash_orders_v1"})
        await asyncio.sleep(0.3)

        collected: list[ChangeEvent] = []
        async for event in watcher.changes():
            collected.append(event)
            if len(collected) >= 1:
                break

        await watcher.stop()

        assert len(collected) == 1
        assert collected[0].path == "users"
        assert collected[0].change_type == ChangeType.MODIFIED


# ═══════════════════════════════════════════════════════════════════
# 4. SourceWatcherService + real EventBus with routing
# ═══════════════════════════════════════════════════════════════════


class TestWatcherServiceWithEventBus:
    """Integration: SourceWatcherService publishes to a real LogEventBus
    with subscriber routing (not a mock/collecting bus)."""

    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        return _make_project(tmp_path)

    @pytest.fixture
    def event_log_path(self, tmp_path: Path) -> Path:
        return tmp_path / "test_events.ndjson"

    @pytest.mark.asyncio
    async def test_polling_events_routed_to_subscriber(
        self, project: Path, event_log_path: Path,
    ) -> None:
        """File changes → PollingWatcher → SourceWatcherService → LogEventBus → subscriber."""
        bus = LogEventBus(log_path=event_log_path)
        received: list[UniversalEvent] = []

        async def on_watcher_event(event: UniversalEvent) -> None:
            received.append(event)

        # Subscribe with real MQTT-style pattern matching
        bus.subscribe("digitorn.watcher.*.file_modified", on_watcher_event)
        bus.subscribe("digitorn.watcher.*.file_created", on_watcher_event)
        bus.subscribe("digitorn.watcher.*.file_deleted", on_watcher_event)

        service = SourceWatcherService(bus)
        await service.start()

        config = WatchConfig(
            source_id="my_project", backend="polling",
            root=str(project), patterns=["*.py"], poll_interval_s=0.1,
        )
        await service.watch(config)

        # Modify a file
        (project / "main.py").write_text("def changed(): pass\n")
        await asyncio.sleep(0.5)

        await service.shutdown()

        # Verify events arrived at the subscriber
        assert len(received) >= 1
        evt = received[0]
        assert evt.topic == "digitorn.watcher.my_project.file_modified"
        assert evt.source == "watcher"
        assert evt.event_type == "watcher_change"
        assert evt.data["source_id"] == "my_project"
        assert evt.data["path"].endswith("main.py")
        assert evt.data["change_type"] == "modified"
        assert evt.data["content_hash"] is not None

        # Verify event was also written to the NDJSON log file
        assert event_log_path.exists()
        log_lines = event_log_path.read_text().strip().split("\n")
        assert len(log_lines) >= 1
        assert "file_modified" in log_lines[0]

    @pytest.mark.asyncio
    async def test_create_and_delete_events_routed(
        self, project: Path, event_log_path: Path,
    ) -> None:
        bus = LogEventBus(log_path=event_log_path)
        received: list[UniversalEvent] = []

        async def on_event(event: UniversalEvent) -> None:
            received.append(event)

        bus.subscribe("digitorn.watcher.#", on_event)

        service = SourceWatcherService(bus)
        await service.start()
        await service.watch(WatchConfig(
            source_id="proj", backend="polling",
            root=str(project), patterns=["*.py"], poll_interval_s=0.1,
        ))

        (project / "new.py").write_text("x = 1\n")
        await asyncio.sleep(0.3)
        (project / "utils.py").unlink()
        await asyncio.sleep(0.3)

        await service.shutdown()

        topics = [e.topic for e in received]
        assert any("file_created" in t for t in topics)
        assert any("file_deleted" in t for t in topics)

    @pytest.mark.asyncio
    async def test_service_bus_watcher_events_routed(
        self, event_log_path: Path,
    ) -> None:
        """ServiceBusPollingWatcher events flow through the real event bus."""
        bus = LogEventBus(log_path=event_log_path)
        received: list[UniversalEvent] = []

        async def on_event(event: UniversalEvent) -> None:
            received.append(event)

        bus.subscribe("digitorn.watcher.#", on_event)

        mock_sbus = MockServiceBus()
        mock_sbus.set_items([
            {"id": "users", "path": "public.users"},
            {"id": "orders", "path": "public.orders"},
        ])
        mock_sbus.set_checksums({
            "users": "hash_v1",
            "orders": "hash_v1",
        })

        service = SourceWatcherService(bus)
        await service.start()

        config = WatchConfig(
            source_id="crm_db", backend="service_bus",
            root="postgres://localhost/crm", poll_interval_s=0.1,
        )
        await service.watch(config, service_bus=mock_sbus, module_id="database")

        status = service.get_status()
        assert status["watchers"]["crm_db"]["backend"] == "ServiceBusPollingWatcher"

        # Modify a table
        mock_sbus.set_checksums({"users": "hash_v2", "orders": "hash_v1"})
        await asyncio.sleep(0.5)

        await service.shutdown()

        assert len(received) >= 1
        assert any(
            e.topic == "digitorn.watcher.crm_db.file_modified"
            and e.data["path"] == "users"
            for e in received
        )

    @pytest.mark.asyncio
    async def test_service_bus_backend_requires_bus(self) -> None:
        """service_bus backend without bus/module_id raises ValueError."""
        bus = LogEventBus()
        service = SourceWatcherService(bus)
        await service.start()

        with pytest.raises(ValueError, match="service_bus"):
            await service.watch(WatchConfig(
                source_id="test", backend="service_bus", root="test://",
            ))

        await service.shutdown()

    @pytest.mark.asyncio
    async def test_watch_unwatch_lifecycle(
        self, project: Path, event_log_path: Path,
    ) -> None:
        bus = LogEventBus(log_path=event_log_path)
        service = SourceWatcherService(bus)
        await service.start()

        config = WatchConfig(
            source_id="proj", backend="polling",
            root=str(project), poll_interval_s=0.1,
        )
        await service.watch(config)
        assert "proj" in service.watched_sources
        assert service.get_status()["count"] == 1

        removed = await service.unwatch("proj")
        assert removed is True
        assert "proj" not in service.watched_sources

        removed = await service.unwatch("nonexistent")
        assert removed is False

        await service.shutdown()

    @pytest.mark.asyncio
    async def test_replace_existing_watcher(
        self, project: Path, event_log_path: Path,
    ) -> None:
        bus = LogEventBus(log_path=event_log_path)
        service = SourceWatcherService(bus)
        await service.start()

        config1 = WatchConfig(
            source_id="proj", backend="polling",
            root=str(project), poll_interval_s=0.1,
        )
        await service.watch(config1)
        config2 = WatchConfig(
            source_id="proj", backend="polling",
            root=str(project), poll_interval_s=0.2,
        )
        await service.watch(config2)

        assert len(service.watched_sources) == 1
        await service.shutdown()

    @pytest.mark.asyncio
    async def test_multi_source_concurrent(
        self, tmp_path: Path, event_log_path: Path,
    ) -> None:
        """Watch multiple sources simultaneously, events are correctly attributed."""
        proj_a = tmp_path / "project_a"
        proj_b = tmp_path / "project_b"
        proj_a.mkdir()
        proj_b.mkdir()
        (proj_a / "a.py").write_text("a = 1\n")
        (proj_b / "b.py").write_text("b = 2\n")

        bus = LogEventBus(log_path=event_log_path)
        received: list[UniversalEvent] = []

        async def on_event(event: UniversalEvent) -> None:
            received.append(event)

        bus.subscribe("digitorn.watcher.#", on_event)

        service = SourceWatcherService(bus)
        await service.start()

        await service.watch(WatchConfig(
            source_id="proj_a", backend="polling",
            root=str(proj_a), patterns=["*.py"], poll_interval_s=0.1,
        ))
        await service.watch(WatchConfig(
            source_id="proj_b", backend="polling",
            root=str(proj_b), patterns=["*.py"], poll_interval_s=0.1,
        ))

        assert len(service.watched_sources) == 2

        (proj_a / "a.py").write_text("a = 10\n")
        (proj_b / "b.py").write_text("b = 20\n")
        await asyncio.sleep(0.5)

        await service.shutdown()

        # Events from both sources should be present
        sources = {e.data["source_id"] for e in received}
        assert "proj_a" in sources
        assert "proj_b" in sources

        # Each event has the correct source_id
        for e in received:
            if "a.py" in e.data.get("path", ""):
                assert e.data["source_id"] == "proj_a"
            elif "b.py" in e.data.get("path", ""):
                assert e.data["source_id"] == "proj_b"


# ═══════════════════════════════════════════════════════════════════
# 5. Full pipeline: Watcher → EventBus → IndexModule auto-reindex
# ═══════════════════════════════════════════════════════════════════


class TestFullPipelineWatcherToIndex:
    """End-to-end: file change → watcher → event bus → index module
    on_event → index store updated. This is the real flow in the daemon."""

    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        return _make_project(tmp_path)

    @pytest.mark.asyncio
    async def test_file_create_auto_indexes(self, project: Path, tmp_path: Path) -> None:
        """New file detected by watcher → index module auto-indexes it."""
        bus = LogEventBus(log_path=tmp_path / "events.ndjson")
        index_mod = IndexModule()

        # Register a source in the index
        index_mod.store.add_source(Source(
            source_id="proj",
            module_id="filesystem",
            root=str(project),
            extractor="auto",
        ))

        # Wire: event bus routes watcher events to index module's on_event
        async def route_to_index(event: UniversalEvent) -> None:
            await index_mod.on_event(event.topic, event.model_dump())

        bus.subscribe("digitorn.watcher.proj.file_created", route_to_index)
        bus.subscribe("digitorn.watcher.proj.file_modified", route_to_index)
        bus.subscribe("digitorn.watcher.proj.file_deleted", route_to_index)

        # Start watcher service
        service = SourceWatcherService(bus)
        await service.start()
        await service.watch(WatchConfig(
            source_id="proj", backend="polling",
            root=str(project), patterns=["*.py"], poll_interval_s=0.1,
        ))

        # Create a new Python file with a function
        (project / "models.py").write_text(
            "class User:\n"
            "    def __init__(self, name: str):\n"
            "        self.name = name\n"
        )
        await asyncio.sleep(0.8)

        await service.shutdown()

        # Index should have auto-indexed the new file
        entries = index_mod.store.get_by_path(str(project / "models.py"))
        assert len(entries) > 0
        names = [e.name for e in entries]
        assert "User" in names

    @pytest.mark.asyncio
    async def test_file_modify_updates_index(self, project: Path, tmp_path: Path) -> None:
        """Modified file detected by watcher → index invalidates + re-indexes."""
        bus = LogEventBus(log_path=tmp_path / "events.ndjson")
        index_mod = IndexModule()

        index_mod.store.add_source(Source(
            source_id="proj",
            module_id="filesystem",
            root=str(project),
            extractor="auto",
        ))

        # Manually index existing file first
        file_path = str(project / "main.py")
        content = (project / "main.py").read_text()
        ext = index_mod.extractors.resolve("auto", file_path, {})
        entries, rels = ext.extract("proj", file_path, content, {})
        for e in entries:
            index_mod.store.upsert(e)

        assert len(index_mod.store.get_by_name("hello")) > 0

        # Wire event bus to index module
        async def route_to_index(event: UniversalEvent) -> None:
            await index_mod.on_event(event.topic, event.model_dump())

        bus.subscribe("digitorn.watcher.#", route_to_index)

        service = SourceWatcherService(bus)
        await service.start()
        await service.watch(WatchConfig(
            source_id="proj", backend="polling",
            root=str(project), patterns=["*.py"], poll_interval_s=0.1,
        ))

        # Modify main.py: replace hello with greet
        (project / "main.py").write_text("def greet(name: str):\n    return f'Hi {name}'\n")
        await asyncio.sleep(0.8)

        await service.shutdown()

        # Old function removed, new function indexed
        assert len(index_mod.store.get_by_name("hello")) == 0
        assert len(index_mod.store.get_by_name("greet")) > 0

    @pytest.mark.asyncio
    async def test_file_delete_invalidates_index(self, project: Path, tmp_path: Path) -> None:
        """Deleted file detected by watcher → index entries removed."""
        bus = LogEventBus(log_path=tmp_path / "events.ndjson")
        index_mod = IndexModule()

        index_mod.store.add_source(Source(
            source_id="proj",
            module_id="filesystem",
            root=str(project),
            extractor="auto",
        ))

        # Index existing file
        file_path = str(project / "utils.py")
        content = (project / "utils.py").read_text()
        ext = index_mod.extractors.resolve("auto", file_path, {})
        entries, rels = ext.extract("proj", file_path, content, {})
        for e in entries:
            index_mod.store.upsert(e)

        assert len(index_mod.store.get_by_path(file_path)) > 0

        # Wire event bus
        async def route_to_index(event: UniversalEvent) -> None:
            await index_mod.on_event(event.topic, event.model_dump())

        bus.subscribe("digitorn.watcher.#", route_to_index)

        service = SourceWatcherService(bus)
        await service.start()
        await service.watch(WatchConfig(
            source_id="proj", backend="polling",
            root=str(project), patterns=["*.py"], poll_interval_s=0.1,
        ))

        (project / "utils.py").unlink()
        await asyncio.sleep(0.8)

        await service.shutdown()

        assert len(index_mod.store.get_by_path(file_path)) == 0


# ═══════════════════════════════════════════════════════════════════
# 6. Index register_source with watch — real watcher lifecycle
# ═══════════════════════════════════════════════════════════════════


class TestRegisterSourceWatchIntegration:
    """Test register_source(watch=True) with a real SourceWatcherService."""

    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        return _make_project(tmp_path)

    @pytest.mark.asyncio
    async def test_register_filesystem_starts_watcher(self, project: Path) -> None:
        bus = LogEventBus()
        watcher_service = SourceWatcherService(bus)
        await watcher_service.start()

        mod = IndexModule()
        mod._watcher_service = watcher_service

        result = await mod.register_source(RegisterSourceParams(
            source_id="proj",
            module_id="filesystem",
            root=str(project),
            watch=True,
            watch_mode="persistent",
        ))

        assert result.success
        assert result.data["watch"] is True
        assert result.data["watch_mode"] == "persistent"
        assert result.data["watch_status"] == "active"
        assert "proj" in watcher_service.watched_sources

        status = watcher_service.get_status()
        assert status["watchers"]["proj"]["running"] is True
        assert status["watchers"]["proj"]["mode"] == "persistent"

        await watcher_service.shutdown()

    @pytest.mark.asyncio
    async def test_register_without_watch(self, project: Path) -> None:
        bus = LogEventBus()
        watcher_service = SourceWatcherService(bus)
        await watcher_service.start()

        mod = IndexModule()
        mod._watcher_service = watcher_service

        result = await mod.register_source(RegisterSourceParams(
            source_id="proj",
            module_id="filesystem",
            root=str(project),
            watch=False,
        ))

        assert result.success
        assert result.data["watch_status"] == "disabled"
        assert "proj" not in watcher_service.watched_sources

        await watcher_service.shutdown()

    @pytest.mark.asyncio
    async def test_register_without_watcher_service(self, project: Path) -> None:
        mod = IndexModule()  # No watcher service at all

        result = await mod.register_source(RegisterSourceParams(
            source_id="proj",
            module_id="filesystem",
            root=str(project),
            watch=True,
        ))

        assert result.success
        assert result.data["watch_status"] == "no_watcher_service"

    @pytest.mark.asyncio
    async def test_register_db_source_needs_service_bus(self, project: Path) -> None:
        """Non-filesystem module with watch=True but no service bus context → error."""
        bus = LogEventBus()
        watcher_service = SourceWatcherService(bus)
        await watcher_service.start()

        mod = IndexModule()
        mod._watcher_service = watcher_service

        result = await mod.register_source(RegisterSourceParams(
            source_id="crm_db",
            module_id="database",
            root="postgres://localhost/crm",
            watch=True,
            watch_mode="persistent",
        ))

        assert result.success
        # Without service_bus in context, service_bus backend can't start
        assert result.data["watch_status"] == "error"

        await watcher_service.shutdown()

    @pytest.mark.asyncio
    async def test_state_snapshot_saves_persistent_watches(self, project: Path) -> None:
        bus = LogEventBus()
        watcher_service = SourceWatcherService(bus)
        await watcher_service.start()

        mod = IndexModule()
        mod._watcher_service = watcher_service

        await mod.register_source(RegisterSourceParams(
            source_id="persistent-proj",
            module_id="filesystem",
            root=str(project),
            watch=True,
            watch_mode="persistent",
        ))
        await mod.register_source(RegisterSourceParams(
            source_id="ephemeral-proj",
            module_id="filesystem",
            root=str(project),
            watch=True,
            watch_mode="ephemeral",
        ))

        snapshot = mod.state_snapshot()

        assert "persistent_watches" in snapshot
        watch_ids = [w["source_id"] for w in snapshot["persistent_watches"]]
        assert "persistent-proj" in watch_ids
        assert "ephemeral-proj" not in watch_ids

        await watcher_service.shutdown()

    @pytest.mark.asyncio
    async def test_restore_state_restarts_persistent_watches(self, project: Path) -> None:
        """Persistent watches survive a daemon restart via state save/restore."""
        bus = LogEventBus()
        watcher_service = SourceWatcherService(bus)
        await watcher_service.start()

        # Phase 1: Register persistent watch and save state
        mod1 = IndexModule()
        mod1._watcher_service = watcher_service

        await mod1.register_source(RegisterSourceParams(
            source_id="proj",
            module_id="filesystem",
            root=str(project),
            watch=True,
            watch_mode="persistent",
        ))

        snapshot = mod1.state_snapshot()
        await watcher_service.shutdown()

        # Phase 2: Simulate daemon restart — new service, new module, restore state
        bus2 = LogEventBus()
        watcher_service2 = SourceWatcherService(bus2)
        await watcher_service2.start()

        mod2 = IndexModule()
        mod2._watcher_service = watcher_service2

        await mod2.restore_state(snapshot)

        # Persistent watch should have been restarted
        assert "proj" in watcher_service2.watched_sources
        assert watcher_service2.get_status()["watchers"]["proj"]["running"] is True

        await watcher_service2.shutdown()

    @pytest.mark.asyncio
    async def test_end_to_end_register_scan_watch_detect(self, project: Path, tmp_path: Path) -> None:
        """Full flow: register → scan → watch → external edit → auto-reindex."""
        bus = LogEventBus(log_path=tmp_path / "events.ndjson")
        watcher_service = SourceWatcherService(bus)
        await watcher_service.start()

        mod = IndexModule()
        mod._watcher_service = watcher_service

        # Register source
        await mod.register_source(RegisterSourceParams(
            source_id="proj",
            module_id="filesystem",
            root=str(project),
            scan_pattern="**/*.py",
            watch=True,
            watch_mode="ephemeral",
        ))

        # Scan to populate index
        await mod.scan(ScanParams(source_id="proj"))
        initial_entries = mod.store.stats()["entries"]
        assert initial_entries > 0
        assert len(mod.store.get_by_name("hello")) > 0

        # Wire event bus to auto-update the index
        async def route_to_index(event: UniversalEvent) -> None:
            await mod.on_event(event.topic, event.model_dump())

        bus.subscribe("digitorn.watcher.proj.file_created", route_to_index)
        bus.subscribe("digitorn.watcher.proj.file_modified", route_to_index)

        # External edit: add a new file
        (project / "service.py").write_text(
            "class UserService:\n"
            "    def get_user(self, user_id: int):\n"
            "        pass\n"
        )
        await asyncio.sleep(0.8)

        # Verify auto-indexing happened
        new_entries = mod.store.get_by_path(str(project / "service.py"))
        assert len(new_entries) > 0
        assert any(e.name == "UserService" for e in new_entries)

        await watcher_service.shutdown()


# ═══════════════════════════════════════════════════════════════════
# 7. WatchConfig serialization
# ═══════════════════════════════════════════════════════════════════


class TestWatchConfigSerialization:
    def test_to_dict(self) -> None:
        config = WatchConfig(
            source_id="proj", backend="filesystem", root="/app",
            patterns=["**/*.py"], mode=WatchMode.PERSISTENT, app_id="my-app",
        )
        d = config.to_dict()
        assert d["source_id"] == "proj"
        assert d["mode"] == "persistent"
        assert d["app_id"] == "my-app"
        assert d["backend"] == "filesystem"

    def test_from_dict(self) -> None:
        d = {
            "source_id": "proj", "backend": "polling", "root": "/db",
            "patterns": ["*"], "mode": "persistent", "app_id": "crm",
            "poll_interval_s": 2.0,
        }
        config = WatchConfig.from_dict(d)
        assert config.source_id == "proj"
        assert config.backend == "polling"
        assert config.mode == WatchMode.PERSISTENT
        assert config.app_id == "crm"
        assert config.poll_interval_s == 2.0

    def test_roundtrip(self) -> None:
        original = WatchConfig(
            source_id="test", backend="filesystem", root="/project",
            patterns=["**/*.py", "**/*.js"], ignore=[".git/**"],
            debounce_ms=500, mode=WatchMode.PERSISTENT, app_id="ide",
        )
        restored = WatchConfig.from_dict(original.to_dict())
        assert restored.source_id == original.source_id
        assert restored.backend == original.backend
        assert restored.root == original.root
        assert restored.patterns == original.patterns
        assert restored.ignore == original.ignore
        assert restored.debounce_ms == original.debounce_ms
        assert restored.mode == original.mode
        assert restored.app_id == original.app_id


# ═══════════════════════════════════════════════════════════════════
# 8. Scénario réel : LLM utilise l'index, fichiers changent, index
#    se met à jour automatiquement, LLM voit les changements
# ═══════════════════════════════════════════════════════════════════


class TestLLMScenarioRealWorkspace:
    """Simule un scénario réel d'utilisation par un agent LLM :

    1. Un workspace Python est créé avec plusieurs fichiers interdépendants
    2. L'index module enregistre le workspace comme source avec watch=true
    3. Le scan indexe tout le code (fonctions, classes, imports, relations)
    4. Le LLM fait une recherche (query) et demande le contexte (context)
    5. Un développeur modifie des fichiers (hors daemon — IDE, git pull, etc.)
    6. Le watcher détecte les changements et met l'index à jour automatiquement
    7. Le LLM refait la même recherche et voit les changements

    Ce test vérifie le pipeline complet :
        fichiers réels → PollingWatcher → SourceWatcherService → LogEventBus
        → routing MQTT → IndexModule.on_event → store invalidate + re-index
        → query/context retournent les nouvelles données
    """

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        """Crée un vrai projet Python avec des fichiers interdépendants."""
        ws = tmp_path / "ecommerce"
        ws.mkdir()

        # models.py — modèles de données
        (ws / "models.py").write_text(
            "from dataclasses import dataclass\n"
            "\n"
            "\n"
            "@dataclass\n"
            "class Product:\n"
            "    product_id: int\n"
            "    name: str\n"
            "    price: float\n"
            "    stock: int = 0\n"
            "\n"
            "\n"
            "@dataclass\n"
            "class Order:\n"
            "    order_id: int\n"
            "    product_id: int\n"
            "    quantity: int\n"
            "    total: float\n"
        )

        # pricing.py — logique de prix
        (ws / "pricing.py").write_text(
            "from models import Product\n"
            "\n"
            "\n"
            "def calculate_price(product: Product, quantity: int) -> float:\n"
            "    \"\"\"Calculate total price for a product order.\"\"\"\n"
            "    return product.price * quantity\n"
            "\n"
            "\n"
            "def apply_discount(total: float, discount_pct: float) -> float:\n"
            "    \"\"\"Apply percentage discount to total.\"\"\"\n"
            "    return total * (1 - discount_pct / 100)\n"
        )

        # inventory.py — gestion de stock
        (ws / "inventory.py").write_text(
            "from models import Product\n"
            "\n"
            "\n"
            "def check_stock(product: Product, quantity: int) -> bool:\n"
            "    \"\"\"Check if enough stock is available.\"\"\"\n"
            "    return product.stock >= quantity\n"
            "\n"
            "\n"
            "def update_stock(product: Product, sold: int) -> int:\n"
            "    \"\"\"Decrease stock after a sale. Returns new stock level.\"\"\"\n"
            "    product.stock -= sold\n"
            "    return product.stock\n"
        )

        # api.py — endpoints
        (ws / "api.py").write_text(
            "from models import Product, Order\n"
            "from pricing import calculate_price, apply_discount\n"
            "from inventory import check_stock, update_stock\n"
            "\n"
            "\n"
            "def create_order(product: Product, quantity: int, discount: float = 0) -> Order:\n"
            "    \"\"\"Create a new order with optional discount.\"\"\"\n"
            "    if not check_stock(product, quantity):\n"
            "        raise ValueError('Not enough stock')\n"
            "    total = calculate_price(product, quantity)\n"
            "    if discount > 0:\n"
            "        total = apply_discount(total, discount)\n"
            "    update_stock(product, quantity)\n"
            "    return Order(\n"
            "        order_id=0,\n"
            "        product_id=product.product_id,\n"
            "        quantity=quantity,\n"
            "        total=total,\n"
            "    )\n"
        )

        return ws

    @pytest.mark.asyncio
    async def test_llm_query_then_edit_then_query_again(
        self, workspace: Path, tmp_path: Path,
    ) -> None:
        """
        Scénario complet :
        1. Le LLM recherche "calculate_price" → trouve la fonction
        2. Le LLM demande le contexte de "calculate_price" → obtient le code
        3. Un développeur modifie pricing.py (ajoute une fonction, renomme l'existante)
        4. Le watcher détecte et met à jour l'index automatiquement
        5. Le LLM refait la recherche → voit les changements
        """
        # ── Setup : daemon-like wiring ────────────────────────────
        bus = LogEventBus(log_path=tmp_path / "events.ndjson")
        watcher_service = SourceWatcherService(bus)
        await watcher_service.start()

        index = IndexModule()
        index._watcher_service = watcher_service

        # Wire watcher events → index module (comme le daemon le fait)
        async def route_to_index(event: UniversalEvent) -> None:
            await index.on_event(event.topic, event.model_dump())

        bus.subscribe("digitorn.watcher.*.file_created", route_to_index)
        bus.subscribe("digitorn.watcher.*.file_modified", route_to_index)
        bus.subscribe("digitorn.watcher.*.file_deleted", route_to_index)

        # ── Étape 1 : Enregistrer le workspace avec watch ─────────
        reg_result = await index.register_source(RegisterSourceParams(
            source_id="ecommerce",
            module_id="filesystem",
            root=str(workspace),
            scan_pattern="**/*.py",
            watch=True,
            watch_mode="ephemeral",
        ))
        assert reg_result.success
        assert reg_result.data["watch_status"] == "active"
        assert "ecommerce" in watcher_service.watched_sources

        # ── Étape 2 : Scanner pour indexer ────────────────────────
        scan_result = await index.scan(ScanParams(source_id="ecommerce"))
        assert scan_result.success
        files_scanned = scan_result.data["files_scanned"]
        assert files_scanned == 4  # models, pricing, inventory, api
        total_entries = scan_result.data["total_entries"]
        assert total_entries > 0

        # ── Étape 3 : Le LLM recherche "calculate_price" ──────────
        from digitorn.modules.index.params import QueryParams, ContextParams

        query_result = await index.query(QueryParams(q="calculate_price"))
        assert query_result.success
        assert query_result.data["count"] >= 1

        # Vérifier qu'on trouve la bonne fonction
        results = query_result.data["results"]
        calc_entry = next(
            (r for r in results if r["name"] == "calculate_price"),
            None,
        )
        assert calc_entry is not None
        assert calc_entry["kind"] == "function"
        assert "pricing.py" in calc_entry["path"]
        assert "product" in calc_entry["signature"].lower()

        # ── Étape 4 : Le LLM demande le contexte complet ─────────
        ctx_result = await index.context(ContextParams(
            target="calculate_price",
            token_budget=4000,
            include_relations=True,
        ))
        assert ctx_result.success
        assert ctx_result.data["target"]["name"] == "calculate_price"

        # Le contenu réel du fichier est récupéré
        content = ctx_result.data["content"]
        assert content is not None
        assert "calculate_price" in content

        # ── Étape 5 : Le LLM recherche toutes les classes ────────
        class_result = await index.query(QueryParams(q="Product", kind="class"))
        assert class_result.success
        class_names = [r["name"] for r in class_result.data["results"]]
        assert "Product" in class_names

        # ── Étape 6 : Vérifier l'état initial de l'index ─────────
        # On sait exactement ce qui est indexé
        assert len(index.store.get_by_name("calculate_price")) > 0
        assert len(index.store.get_by_name("apply_discount")) > 0
        assert len(index.store.get_by_name("check_stock")) > 0
        assert len(index.store.get_by_name("create_order")) > 0
        assert len(index.store.get_by_name("Product")) > 0
        assert len(index.store.get_by_name("Order")) > 0

        # ══════════════════════════════════════════════════════════
        # Le développeur modifie le workspace (IDE, git pull, etc.)
        # ══════════════════════════════════════════════════════════

        # Modification 1 : Renommer calculate_price → compute_total
        #                   et ajouter une fonction calculate_tax
        (workspace / "pricing.py").write_text(
            "from models import Product\n"
            "\n"
            "TAX_RATE = 0.20\n"
            "\n"
            "\n"
            "def compute_total(product: Product, quantity: int) -> float:\n"
            "    \"\"\"Compute total price for a product order.\"\"\"\n"
            "    return product.price * quantity\n"
            "\n"
            "\n"
            "def calculate_tax(subtotal: float) -> float:\n"
            "    \"\"\"Calculate tax on a subtotal.\"\"\"\n"
            "    return subtotal * TAX_RATE\n"
            "\n"
            "\n"
            "def apply_discount(total: float, discount_pct: float) -> float:\n"
            "    \"\"\"Apply percentage discount to total.\"\"\"\n"
            "    return total * (1 - discount_pct / 100)\n"
        )

        # Modification 2 : Ajouter un nouveau fichier shipping.py
        (workspace / "shipping.py").write_text(
            "from models import Order\n"
            "\n"
            "\n"
            "def calculate_shipping(order: Order, zone: str = 'domestic') -> float:\n"
            "    \"\"\"Calculate shipping cost based on order total and zone.\"\"\"\n"
            "    base = 5.0 if zone == 'domestic' else 15.0\n"
            "    if order.total > 100:\n"
            "        return 0.0  # Free shipping\n"
            "    return base\n"
            "\n"
            "\n"
            "class ShippingTracker:\n"
            "    \"\"\"Track shipment status.\"\"\"\n"
            "    def __init__(self, order: Order):\n"
            "        self.order = order\n"
            "        self.status = 'pending'\n"
            "\n"
            "    def ship(self) -> None:\n"
            "        self.status = 'shipped'\n"
            "\n"
            "    def deliver(self) -> None:\n"
            "        self.status = 'delivered'\n"
        )

        # Modification 3 : Supprimer inventory.py
        (workspace / "inventory.py").unlink()

        # ── Attendre que le watcher détecte et que l'index se mette à jour
        await asyncio.sleep(1.0)

        # ══════════════════════════════════════════════════════════
        # Le LLM refait ses recherches — il doit voir les changements
        # ══════════════════════════════════════════════════════════

        # ── Vérification 1 : "calculate_price" n'existe plus ─────
        old_query = await index.query(QueryParams(q="calculate_price", kind="function"))
        old_names = [r["name"] for r in old_query.data["results"]]
        assert "calculate_price" not in old_names

        # ── Vérification 2 : "compute_total" existe maintenant ───
        new_query = await index.query(QueryParams(q="compute_total"))
        assert new_query.data["count"] >= 1
        new_names = [r["name"] for r in new_query.data["results"]]
        assert "compute_total" in new_names

        # ── Vérification 3 : "calculate_tax" (nouvelle fonction) ──
        tax_query = await index.query(QueryParams(q="calculate_tax"))
        assert tax_query.data["count"] >= 1
        tax_entry = next(
            r for r in tax_query.data["results"] if r["name"] == "calculate_tax"
        )
        assert "subtotal" in tax_entry["signature"]

        # ── Vérification 4 : shipping.py est indexé (nouveau fichier)
        ship_query = await index.query(QueryParams(q="calculate_shipping"))
        assert ship_query.data["count"] >= 1

        tracker_query = await index.query(QueryParams(q="ShippingTracker", kind="class"))
        assert tracker_query.data["count"] >= 1

        # ── Vérification 5 : inventory.py n'est plus dans l'index
        stock_query = await index.query(QueryParams(q="check_stock", kind="function"))
        stock_names = [r["name"] for r in stock_query.data["results"]]
        assert "check_stock" not in stock_names

        update_query = await index.query(QueryParams(q="update_stock", kind="function"))
        update_names = [r["name"] for r in update_query.data["results"]]
        assert "update_stock" not in update_names

        # ── Vérification 6 : Le contexte de "compute_total" fonctionne
        ctx_new = await index.context(ContextParams(
            target="compute_total",
            token_budget=4000,
        ))
        assert ctx_new.success
        assert ctx_new.data["target"]["name"] == "compute_total"
        new_content = ctx_new.data["content"]
        assert new_content is not None
        assert "compute_total" in new_content
        assert "product.price" in new_content

        # ── Vérification 7 : Le contexte de "ShippingTracker" fonctionne
        ctx_ship = await index.context(ContextParams(
            target="ShippingTracker",
            token_budget=4000,
        ))
        assert ctx_ship.success
        assert ctx_ship.data["target"]["name"] == "ShippingTracker"

        # ── Vérification 8 : Les stats de l'index reflètent les changements
        stats = index.store.stats()
        sources = index.store.list_sources()
        ecom_source = next(s for s in sources if s.source_id == "ecommerce")
        assert ecom_source.watch is True

        # ── Vérification 9 : Le fichier NDJSON contient les events
        log_path = tmp_path / "events.ndjson"
        assert log_path.exists()
        log_content = log_path.read_text()
        assert "file_modified" in log_content
        assert "file_created" in log_content
        assert "file_deleted" in log_content

        # ── Cleanup ───────────────────────────────────────────────
        await watcher_service.shutdown()

    @pytest.mark.asyncio
    async def test_rapid_consecutive_edits(
        self, workspace: Path, tmp_path: Path,
    ) -> None:
        """
        Scénario : un développeur fait plusieurs éditions rapides (ctrl+s répétés).
        Le watcher ne doit pas perdre d'événements et l'index doit converger
        vers l'état final correct.
        """
        bus = LogEventBus(log_path=tmp_path / "events.ndjson")
        watcher_service = SourceWatcherService(bus)
        await watcher_service.start()

        index = IndexModule()
        index._watcher_service = watcher_service

        async def route_to_index(event: UniversalEvent) -> None:
            await index.on_event(event.topic, event.model_dump())

        bus.subscribe("digitorn.watcher.*.file_modified", route_to_index)
        bus.subscribe("digitorn.watcher.*.file_created", route_to_index)

        await index.register_source(RegisterSourceParams(
            source_id="ecommerce",
            module_id="filesystem",
            root=str(workspace),
            scan_pattern="**/*.py",
            watch=True,
        ))
        await index.scan(ScanParams(source_id="ecommerce"))

        # Le développeur fait 5 éditions rapides sur le même fichier
        for i in range(5):
            (workspace / "pricing.py").write_text(
                f"VERSION = {i}\n"
                f"\n"
                f"def pricing_v{i}(x: float) -> float:\n"
                f"    \"\"\"Version {i} of pricing.\"\"\"\n"
                f"    return x * {1 + i * 0.1}\n"
            )
            await asyncio.sleep(0.05)  # ~50ms entre chaque save

        # Attendre que le watcher converge
        await asyncio.sleep(1.0)

        # L'index doit refléter la DERNIÈRE version
        from digitorn.modules.index.params import QueryParams

        final_query = await index.query(QueryParams(q="pricing_v4"))
        assert final_query.data["count"] >= 1

        # Les anciennes versions ne doivent plus exister
        old_query = await index.query(QueryParams(q="calculate_price", kind="function"))
        old_names = [r["name"] for r in old_query.data["results"]]
        assert "calculate_price" not in old_names

        await watcher_service.shutdown()

    @pytest.mark.asyncio
    async def test_git_branch_switch_scenario(
        self, workspace: Path, tmp_path: Path,
    ) -> None:
        """
        Scénario : simuler un git checkout qui modifie plusieurs fichiers d'un coup.
        Tous les fichiers changent en même temps, le watcher doit tout capter.
        """
        bus = LogEventBus(log_path=tmp_path / "events.ndjson")
        watcher_service = SourceWatcherService(bus)
        await watcher_service.start()

        index = IndexModule()
        index._watcher_service = watcher_service

        async def route_to_index(event: UniversalEvent) -> None:
            await index.on_event(event.topic, event.model_dump())

        bus.subscribe("digitorn.watcher.*.file_modified", route_to_index)
        bus.subscribe("digitorn.watcher.*.file_created", route_to_index)
        bus.subscribe("digitorn.watcher.*.file_deleted", route_to_index)

        await index.register_source(RegisterSourceParams(
            source_id="ecommerce",
            module_id="filesystem",
            root=str(workspace),
            scan_pattern="**/*.py",
            watch=True,
        ))
        await index.scan(ScanParams(source_id="ecommerce"))

        # Vérifier l'état initial
        from digitorn.modules.index.params import QueryParams
        initial = await index.query(QueryParams(q="Product", kind="class"))
        assert initial.data["count"] >= 1

        # Simuler un "git checkout feature-branch" :
        # Tous les fichiers changent d'un coup
        (workspace / "models.py").write_text(
            "from dataclasses import dataclass\n"
            "\n"
            "@dataclass\n"
            "class Product:\n"
            "    product_id: int\n"
            "    name: str\n"
            "    price: float\n"
            "    stock: int = 0\n"
            "    category: str = 'general'\n"  # nouveau champ
            "\n"
            "@dataclass\n"
            "class Customer:\n"  # nouveau modèle
            "    customer_id: int\n"
            "    email: str\n"
            "    name: str\n"
        )
        (workspace / "pricing.py").write_text(
            "from models import Product\n"
            "\n"
            "def calculate_price(product: Product, qty: int, member: bool = False) -> float:\n"
            "    \"\"\"V2: with member discount.\"\"\"\n"
            "    base = product.price * qty\n"
            "    return base * 0.9 if member else base\n"
        )
        (workspace / "api.py").write_text(
            "from models import Product, Customer\n"
            "\n"
            "def register_customer(email: str, name: str) -> Customer:\n"
            "    return Customer(customer_id=0, email=email, name=name)\n"
        )

        await asyncio.sleep(1.0)

        # Vérifier que l'index reflète la nouvelle branche
        customer_q = await index.query(QueryParams(q="Customer", kind="class"))
        assert customer_q.data["count"] >= 1

        register_q = await index.query(QueryParams(q="register_customer"))
        assert register_q.data["count"] >= 1

        # calculate_price a une nouvelle signature (avec member)
        price_q = await index.query(QueryParams(q="calculate_price"))
        assert price_q.data["count"] >= 1
        price_entry = next(
            r for r in price_q.data["results"] if r["name"] == "calculate_price"
        )
        assert "member" in price_entry["signature"]

        # create_order n'existe plus (api.py a été réécrit)
        order_q = await index.query(QueryParams(q="create_order", kind="function"))
        order_names = [r["name"] for r in order_q.data["results"]]
        assert "create_order" not in order_names

        await watcher_service.shutdown()


# ═══════════════════════════════════════════════════════════════════
# 9. End-to-end: Lifecycle manager → event subscription → watcher → reindex
# ═══════════════════════════════════════════════════════════════════


class TestLifecycleWatcherIntegration:
    """Test the full daemon-style wiring:

    ModuleLifecycleManager starts IndexModule
        → auto-subscribes to watcher events via EventBus
        → watcher detects file changes
        → EventBus dispatches to IndexModule.on_event()
        → index auto-updates

    This is the critical test that proves all the pieces work together.
    """

    @pytest.fixture()
    def workspace(self, tmp_path: Path) -> Path:
        """Create a workspace with Python files."""
        ws = tmp_path / "project"
        ws.mkdir()
        (ws / "app.py").write_text(
            "def hello():\n"
            "    return 'Hello, World!'\n"
            "\n"
            "def goodbye():\n"
            "    return 'Goodbye!'\n"
        )
        (ws / "utils.py").write_text(
            "def add(a: int, b: int) -> int:\n"
            "    return a + b\n"
        )
        return ws

    @pytest.mark.asyncio()
    async def test_lifecycle_wires_events_correctly(
        self, workspace: Path, tmp_path: Path,
    ) -> None:
        """Lifecycle manager starts module → subscribes to watcher events
        → watcher fires → index updates automatically."""
        from digitorn.core.state_store import InMemoryStateStore
        from digitorn.modules.lifecycle import ModuleLifecycleManager
        from digitorn.modules.registry import ModuleRegistry

        # ── Setup: real EventBus + watcher + registry + lifecycle ──
        bus = LogEventBus(log_path=tmp_path / "events.ndjson")
        watcher_service = SourceWatcherService(bus)
        await watcher_service.start()

        index = IndexModule()
        index._watcher_service = watcher_service  # inject watcher

        registry = ModuleRegistry()
        registry.register_instance(index)

        state_store = InMemoryStateStore()
        lifecycle = ModuleLifecycleManager(
            registry=registry,
            event_bus=bus,
            state_store=state_store,
        )

        # ── Start module via lifecycle (triggers auto-subscribe) ──
        await lifecycle.start_module("index")

        # ── Register source + scan ──
        result = await index.register_source(RegisterSourceParams(
            source_id="myproject",
            module_id="filesystem",
            root=str(workspace),
            watch=True,
            watch_mode="persistent",
        ))
        assert result.data["watch_status"] == "active"

        await index.scan(ScanParams(source_id="myproject"))

        # Verify initial index
        from digitorn.modules.index.params import QueryParams
        q = await index.query(QueryParams(q="hello"))
        assert q.data["count"] >= 1

        # ── External change: add a new file ──
        (workspace / "new_feature.py").write_text(
            "def exciting_feature(x: int) -> str:\n"
            "    return f'Feature {x}'\n"
        )

        # Wait for watcher → EventBus → on_event → reindex
        await asyncio.sleep(1.5)

        # ── Verify: new function is in the index ──
        q2 = await index.query(QueryParams(q="exciting_feature"))
        assert q2.data["count"] >= 1, "New file should be auto-indexed"

        # ── External change: modify existing file ──
        (workspace / "app.py").write_text(
            "def hello():\n"
            "    return 'Hello, Universe!'\n"
            "\n"
            "def goodbye():\n"
            "    return 'Goodbye!'\n"
            "\n"
            "def new_function():\n"
            "    return 42\n"
        )

        await asyncio.sleep(1.5)

        q3 = await index.query(QueryParams(q="new_function"))
        assert q3.data["count"] >= 1, "Modified file should be re-indexed"

        # ── External change: delete a file ──
        (workspace / "utils.py").unlink()
        await asyncio.sleep(1.5)

        q4 = await index.query(QueryParams(q="add", kind="function"))
        add_names = [r["name"] for r in q4.data["results"]]
        assert "add" not in add_names, "Deleted file entries should be removed"

        # ── State save/restore cycle ──
        await lifecycle.stop_module("index")

        # Verify state was saved
        saved = await state_store.load("index")
        assert saved is not None
        assert "persistent_watches" in saved
        assert len(saved["persistent_watches"]) == 1
        assert saved["persistent_watches"][0]["source_id"] == "myproject"

        await watcher_service.shutdown()

    @pytest.mark.asyncio()
    async def test_lifecycle_state_restore_restarts_watches(
        self, workspace: Path, tmp_path: Path,
    ) -> None:
        """After a daemon restart, persistent watches should be restored
        and continue working."""
        from digitorn.core.state_store import InMemoryStateStore
        from digitorn.modules.lifecycle import ModuleLifecycleManager
        from digitorn.modules.registry import ModuleRegistry

        # ── Phase 1: Initial startup, register source, save state ──
        bus1 = LogEventBus(log_path=tmp_path / "events1.ndjson")
        watcher1 = SourceWatcherService(bus1)
        await watcher1.start()

        index1 = IndexModule()
        index1._watcher_service = watcher1

        registry1 = ModuleRegistry()
        registry1.register_instance(index1)

        state_store = InMemoryStateStore()
        lifecycle1 = ModuleLifecycleManager(
            registry=registry1, event_bus=bus1, state_store=state_store,
        )
        await lifecycle1.start_module("index")

        await index1.register_source(RegisterSourceParams(
            source_id="myproject",
            module_id="filesystem",
            root=str(workspace),
            watch=True,
            watch_mode="persistent",
        ))
        await index1.scan(ScanParams(source_id="myproject"))

        # Save state and shut down
        await lifecycle1.stop_module("index")
        await watcher1.shutdown()

        # ── Phase 2: Simulate daemon restart ──
        bus2 = LogEventBus(log_path=tmp_path / "events2.ndjson")
        watcher2 = SourceWatcherService(bus2)
        await watcher2.start()

        index2 = IndexModule()
        index2._watcher_service = watcher2

        registry2 = ModuleRegistry()
        registry2.register_instance(index2)

        lifecycle2 = ModuleLifecycleManager(
            registry=registry2, event_bus=bus2, state_store=state_store,
        )
        await lifecycle2.start_module("index")

        # Verify state was restored
        from digitorn.modules.index.params import QueryParams
        q = await index2.query(QueryParams(q="hello"))
        assert q.data["count"] >= 1, "Index should be restored from state"

        # Verify watcher was restarted — add a new file
        (workspace / "restored.py").write_text(
            "def i_was_restored() -> bool:\n"
            "    return True\n"
        )

        await asyncio.sleep(1.5)

        q2 = await index2.query(QueryParams(q="i_was_restored"))
        assert q2.data["count"] >= 1, (
            "Persistent watch should be restarted after restore"
        )

        await lifecycle2.stop_module("index")
        await watcher2.shutdown()

    @pytest.mark.asyncio()
    async def test_state_store_json_roundtrip(self, tmp_path: Path) -> None:
        """JsonStateStore can save and load module state."""
        from digitorn.core.state_store import JsonStateStore

        store = JsonStateStore(tmp_path / "state")

        # Save
        state = {"sources": {"s1": {"root": "/tmp"}}, "entries": {}, "relations": []}
        await store.save("index", state)

        # Load
        loaded = await store.load("index")
        assert loaded == state

        # List
        modules = await store.list_modules()
        assert "index" in modules

        # Delete
        await store.delete("index")
        assert await store.load("index") is None

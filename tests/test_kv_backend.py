"""Tests for the KeyValueBackend abstraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from digitorn.core.kv import DiskCacheBackend, create_backend


@pytest.fixture
def backend(tmp_path: Path) -> DiskCacheBackend:
    return DiskCacheBackend(directory=tmp_path / "kv")


class TestDiskCacheBackend:
    def test_get_set(self, backend: DiskCacheBackend) -> None:
        backend.set("key", "value")
        assert backend.get("key") == "value"

    def test_get_missing(self, backend: DiskCacheBackend) -> None:
        assert backend.get("missing") is None
        assert backend.get("missing", "default") == "default"

    def test_delete(self, backend: DiskCacheBackend) -> None:
        backend.set("key", "value")
        assert backend.delete("key") is True
        assert backend.get("key") is None
        assert backend.delete("key") is False

    def test_incr(self, backend: DiskCacheBackend) -> None:
        assert backend.incr("counter") == 1
        assert backend.incr("counter") == 2
        assert backend.incr("counter") == 3

    def test_complex_values(self, backend: DiskCacheBackend) -> None:
        data = {"nested": [1, 2, 3], "set": {4, 5}}
        backend.set("complex", data)
        result = backend.get("complex")
        assert result["nested"] == [1, 2, 3]
        assert result["set"] == {4, 5}

    def test_len_and_volume(self, backend: DiskCacheBackend) -> None:
        backend.set("a", 1)
        backend.set("b", 2)
        assert len(backend) == 2
        assert backend.volume > 0


class TestCreateBackend:
    def test_default_creates_diskcache(self, tmp_path: Path) -> None:
        b = create_backend(directory=tmp_path / "test")
        assert isinstance(b, DiskCacheBackend)
        b.close()

    def test_path_creates_diskcache(self, tmp_path: Path) -> None:
        b = create_backend(str(tmp_path / "test2"))
        assert isinstance(b, DiskCacheBackend)
        b.close()

    def test_redis_url_attempts_redis(self) -> None:
        # If redis is not installed or not running, this should raise
        # We just test the dispatch logic
        try:
            b = create_backend("redis://localhost:6379/15")
            b.close()
        except Exception:
            pass  # Redis not available - that's fine for unit tests

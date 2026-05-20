"""Content-addressed blob storage for multimedia attachments."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from digitorn.core.runtime.session_store.types import BlobRef, utc_iso

logger = logging.getLogger(__name__)


class BlobStore:
    """Content-addressed blob persistence on local disk."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, blob_hash: str) -> Path:
        return self._root / blob_hash[:2] / blob_hash

    async def put(self, data: bytes, mime: str) -> BlobRef:
        """Store `data` under its sha256, return a BlobRef. If the"""
        h = hashlib.sha256(data).hexdigest()
        size = len(data)
        return await asyncio.to_thread(self._put_sync, h, data, mime, size)

    def _put_sync(self, h: str, data: bytes, mime: str, size: int) -> BlobRef:
        bdir = self._path_for(h)
        bdir.mkdir(parents=True, exist_ok=True)
        data_path = bdir / "data"
        meta_path = bdir / "meta.json"
        if not data_path.exists():
            fd, tmp = tempfile.mkstemp(prefix=".data_", suffix=".tmp", dir=str(bdir))
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, data_path)
            meta = {
                "mime": mime,
                "size": size,
                "ref_count": 1,
                "first_seen_at": utc_iso(),
            }
        else:
            meta = self._read_meta(meta_path) or {
                "mime": mime, "size": size, "ref_count": 0,
                "first_seen_at": utc_iso(),
            }
            meta["ref_count"] = int(meta.get("ref_count", 0)) + 1
        self._write_meta(meta_path, meta)
        return BlobRef(hash=h, mime=mime, size=size)

    async def get(self, blob_hash: str) -> bytes | None:
        return await asyncio.to_thread(self._get_sync, blob_hash)

    def _get_sync(self, blob_hash: str) -> bytes | None:
        path = self._path_for(blob_hash) / "data"
        if not path.exists():
            return None
        return path.read_bytes()

    async def exists(self, blob_hash: str) -> bool:
        return await asyncio.to_thread(
            lambda: (self._path_for(blob_hash) / "data").exists(),
        )

    async def incref(self, blob_hash: str) -> int:
        return await asyncio.to_thread(self._refcount_delta, blob_hash, +1)

    async def decref(self, blob_hash: str) -> int:
        return await asyncio.to_thread(self._refcount_delta, blob_hash, -1)

    def _refcount_delta(self, blob_hash: str, delta: int) -> int:
        meta_path = self._path_for(blob_hash) / "meta.json"
        meta = self._read_meta(meta_path)
        if meta is None:
            return 0
        meta["ref_count"] = max(0, int(meta.get("ref_count", 0)) + delta)
        self._write_meta(meta_path, meta)
        return int(meta["ref_count"])

    async def gc(self) -> int:
        """Delete blobs with ref_count == 0. Returns count removed."""
        return await asyncio.to_thread(self._gc_sync)

    def _gc_sync(self) -> int:
        removed = 0
        if not self._root.exists():
            return 0
        for bucket in self._root.iterdir():
            if not bucket.is_dir():
                continue
            for blob_dir in bucket.iterdir():
                meta = self._read_meta(blob_dir / "meta.json")
                if meta is None:
                    continue
                if int(meta.get("ref_count", 0)) <= 0:
                    try:
                        for f in blob_dir.iterdir():
                            f.unlink()
                        blob_dir.rmdir()
                        removed += 1
                    except OSError as exc:
                        logger.warning(
                            "blob_gc_failed dir=%s err=%s", blob_dir, exc,
                        )
        return removed

    @staticmethod
    def _read_meta(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _write_meta(path: Path, meta: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".meta_", suffix=".tmp", dir=str(path.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(meta, f, default=str, ensure_ascii=False)
        os.replace(tmp, path)

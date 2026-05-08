"""BlobStore: content-addressing, dedup, ref counting, GC."""
from __future__ import annotations

from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.blob_store import BlobStore


@pytest.mark.asyncio
async def test_put_and_get_roundtrip(tmp_root: Path):
    bs = BlobStore(tmp_root / "blobs")
    ref = await bs.put(b"hello world", "text/plain")
    assert ref.mime == "text/plain"
    assert ref.size == 11
    assert ref.hash == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    data = await bs.get(ref.hash)
    assert data == b"hello world"


@pytest.mark.asyncio
async def test_dedup_same_content(tmp_root: Path):
    bs = BlobStore(tmp_root / "blobs")
    a = await bs.put(b"same", "image/png")
    b = await bs.put(b"same", "image/png")
    assert a.hash == b.hash
    bucket_dir = (tmp_root / "blobs" / a.hash[:2]).iterdir()
    blob_dirs = [d for d in bucket_dir if d.is_dir()]
    assert len(blob_dirs) == 1


@pytest.mark.asyncio
async def test_ref_count_increments_on_put(tmp_root: Path):
    bs = BlobStore(tmp_root / "blobs")
    ref = await bs.put(b"x", "application/octet-stream")
    await bs.put(b"x", "application/octet-stream")
    await bs.put(b"x", "application/octet-stream")
    n = await bs.incref(ref.hash)
    assert n >= 3


@pytest.mark.asyncio
async def test_decref_to_zero_then_gc(tmp_root: Path):
    bs = BlobStore(tmp_root / "blobs")
    ref = await bs.put(b"orphan", "text/plain")
    n = await bs.decref(ref.hash)
    assert n == 0
    removed = await bs.gc()
    assert removed == 1
    assert await bs.get(ref.hash) is None


@pytest.mark.asyncio
async def test_gc_keeps_referenced(tmp_root: Path):
    bs = BlobStore(tmp_root / "blobs")
    keep = await bs.put(b"keep", "text/plain")
    drop = await bs.put(b"drop", "text/plain")
    await bs.decref(drop.hash)
    removed = await bs.gc()
    assert removed == 1
    assert await bs.get(keep.hash) == b"keep"
    assert await bs.get(drop.hash) is None


@pytest.mark.asyncio
async def test_get_unknown_returns_none(tmp_root: Path):
    bs = BlobStore(tmp_root / "blobs")
    assert await bs.get("0" * 64) is None


@pytest.mark.asyncio
async def test_exists(tmp_root: Path):
    bs = BlobStore(tmp_root / "blobs")
    ref = await bs.put(b"abc", "text/plain")
    assert await bs.exists(ref.hash) is True
    assert await bs.exists("0" * 64) is False


@pytest.mark.asyncio
async def test_large_blob(tmp_root: Path):
    bs = BlobStore(tmp_root / "blobs")
    big = b"\x42" * 1_500_000
    ref = await bs.put(big, "application/octet-stream")
    assert ref.size == 1_500_000
    assert await bs.get(ref.hash) == big

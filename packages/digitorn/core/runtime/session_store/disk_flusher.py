"""Background write-behind worker, sharded for 100K-session scale."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Callable

from digitorn.core.runtime.session_store.meta_io import MetaIO
from digitorn.core.runtime.session_store.types import Event, utc_iso

logger = logging.getLogger(__name__)


SessionDirResolver = Callable[[str], Path]
ChatMetaResolver = Callable[[str], dict]


_DEFAULT_NUM_SHARDS = 32

_DURABILITY_MODES = frozenset({"strict", "relaxed"})


def _shard_for_sid(sid: str, num_shards: int) -> int:
    """Hash-based shard assignment. `sha256` over the sid bytes"""
    h = hashlib.sha256(sid.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") % num_shards


class _FlusherShard:
    """One independent write-behind worker. Identical to the legacy"""

    def __init__(
        self,
        *,
        shard_id: int,
        session_dir_resolver: SessionDirResolver,
        chat_meta_resolver: "ChatMetaResolver | None" = None,
        flush_interval_ms: int = 25,
        batch_max: int = 500,
        queue_max: int = 100_000,
        durability_mode: str = "relaxed",
    ) -> None:
        if durability_mode not in _DURABILITY_MODES:
            raise ValueError(
                f"durability_mode must be one of {sorted(_DURABILITY_MODES)}, "
                f"got {durability_mode!r}"
            )
        self._shard_id = shard_id
        self._dir_resolver = session_dir_resolver
        self._chat_meta_resolver = chat_meta_resolver
        self._flush_s = flush_interval_ms / 1000.0
        self._batch_max = batch_max
        self._durability_mode = durability_mode
        self._queue_max = queue_max
        self._queue: asyncio.Queue[tuple[str, Event]] = asyncio.Queue(queue_max)
        self._sid_pending: dict[str, int] = defaultdict(int)
        self._per_sid_quota = max(1, int(queue_max * 0.125))
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._drained = asyncio.Event()
        self._drained.set()

        self.dropped: int = 0
        self.dropped_by_sid: dict[str, int] = defaultdict(int)
        self.written: int = 0
        self.batch_count: int = 0
        self.last_batch_size: int = 0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(),
            name=f"session-disk-flusher-{self._shard_id:02d}",
        )

    async def stop(self, *, drain_timeout: float = 20.0) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._drained.wait(), timeout=drain_timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "disk_flusher_drain_timeout shard=%d queue_size=%d",
                self._shard_id, self._queue.qsize(),
            )
        try:
            await asyncio.wait_for(self._task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None

    def enqueue(self, sid: str, event: Event) -> None:
        sid_pending = self._sid_pending.get(sid, 0)
        qsize = self._queue.qsize()
        if qsize > (self._queue_max // 2) and sid_pending >= self._per_sid_quota:
            self.dropped += 1
            self.dropped_by_sid[sid] += 1
            # Log loudly the first time (and every 100 drops) so ops
            # can spot the runaway session id without log flooding.
            if self.dropped_by_sid[sid] in (1, 100) or self.dropped_by_sid[sid] % 1000 == 0:
                logger.error(
                    "disk_flusher_sid_quota_exceeded shard=%d sid=%s "
                    "pending=%d quota=%d queue=%d/%d total_dropped_for_sid=%d "
                    "DROPPING event seq=%d type=%s -- runaway session is "
                    "starving the shard; investigate / abort the session",
                    self._shard_id, sid, sid_pending, self._per_sid_quota,
                    qsize, self._queue_max, self.dropped_by_sid[sid],
                    event.seq, event.type,
                )
            return
        try:
            self._queue.put_nowait((sid, event))
            self._sid_pending[sid] = sid_pending + 1
            self._drained.clear()
        except asyncio.QueueFull:
            self.dropped += 1
            self.dropped_by_sid[sid] += 1
            logger.error(
                "disk_flusher_queue_full shard=%d sid=%s seq=%d type=%s "
                "DROPPING event -- check disk IO health (shard-wide saturation)",
                self._shard_id, sid, event.seq, event.type,
            )

    async def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                batch = await self._gather_batch()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "disk_flusher_gather_failed shard=%d err=%s",
                    self._shard_id, exc, exc_info=True,
                )
                self._drained.set()
                continue
            if not batch:
                if self._queue.empty():
                    self._drained.set()
                continue
            try:
                await self._write_batch(batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "disk_flusher_write_batch_failed shard=%d "
                    "batch_size=%d err=%s",
                    self._shard_id,
                    sum(len(v) for v in batch.values()),
                    exc, exc_info=True,
                )
                self._drained.set()
                continue
            self.batch_count += 1
            self.last_batch_size = sum(len(v) for v in batch.values())
            if self._queue.empty():
                self._drained.set()
        self._drained.set()

    async def _gather_batch(self) -> dict[str, list[Event]]:
        batch: dict[str, list[Event]] = defaultdict(list)
        try:
            sid, ev = await asyncio.wait_for(
                self._queue.get(), timeout=self._flush_s,
            )
            batch[sid].append(ev)
            self._dec_sid_pending(sid)
            self._queue.task_done()
        except asyncio.TimeoutError:
            return {}
        total = 1
        while total < self._batch_max and not self._queue.empty():
            try:
                sid, ev = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            batch[sid].append(ev)
            self._dec_sid_pending(sid)
            self._queue.task_done()
            total += 1
        return batch

    def _dec_sid_pending(self, sid: str) -> None:
        """Drop the per-sid pending counter as events leave the queue."""
        cur = self._sid_pending.get(sid, 0)
        if cur <= 1:
            self._sid_pending.pop(sid, None)
        else:
            self._sid_pending[sid] = cur - 1

    async def _write_batch(self, batch: dict[str, list[Event]]) -> None:
        sids = list(batch.keys())
        results = await asyncio.gather(
            *(asyncio.to_thread(self._write_session, sid, batch[sid])
              for sid in sids),
            return_exceptions=True,
        )
        for sid, res in zip(sids, results):
            if isinstance(res, BaseException):
                logger.error(
                    "disk_flusher_session_write_failed shard=%d sid=%s "
                    "events=%d err=%s",
                    self._shard_id, sid, len(batch[sid]), res,
                    exc_info=res,
                )

    def _write_session(self, sid: str, events: list[Event]) -> None:
        events.sort(key=lambda e: e.seq)
        session_dir = self._dir_resolver(sid)
        session_dir.mkdir(parents=True, exist_ok=True)
        meta = MetaIO.read(session_dir) or {}
        last_seq = int(meta.get("last_seq", 0))
        kept: list[Event] = []
        for ev in events:
            if ev.seq <= last_seq:
                logger.error(
                    "seq_regression sid=%s on_disk=%d incoming=%d "
                    "type=%s -- DROPPING duplicate",
                    sid, last_seq, ev.seq, ev.type,
                )
                continue
            kept.append(ev)
            last_seq = ev.seq
        if not kept:
            return

        path = session_dir / "events.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for ev in kept:
                f.write(json.dumps(ev.to_dict(), default=str, ensure_ascii=False))
                f.write("\n")
            f.flush()
            if self._durability_mode == "strict":
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass

        meta.update({
            "session_id": sid,
            "last_seq": last_seq,
            "first_seq": int(meta.get("first_seq") or kept[0].seq),
            "event_count": int(meta.get("event_count", 0)) + len(kept),
            "last_flushed_at": utc_iso(),
        })
        if "started_at" not in meta:
            meta["started_at"] = kept[0].ts
        if self._chat_meta_resolver is not None:
            try:
                chat_meta = self._chat_meta_resolver(sid) or {}
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "chat_meta_resolver_failed sid=%s err=%s -- skipping",
                    sid, exc,
                )
                chat_meta = {}
            for k, v in chat_meta.items():
                if v is None and k in meta:
                    continue
                meta[k] = v
        MetaIO.write(session_dir, meta)
        self.written += len(kept)

    async def flush(self) -> None:
        if self._queue.empty():
            return
        await self._drained.wait()


class DiskFlusher:
    """Pool of `_FlusherShard` workers, sharded by `hash(sid)`."""

    def __init__(
        self,
        *,
        session_dir_resolver: SessionDirResolver,
        chat_meta_resolver: "ChatMetaResolver | None" = None,
        flush_interval_ms: int = 25,
        batch_max: int = 500,
        queue_max: int = 100_000,
        num_shards: int = _DEFAULT_NUM_SHARDS,
        durability_mode: str = "relaxed",
    ) -> None:
        if num_shards < 1:
            raise ValueError("num_shards must be >= 1")
        self._num_shards = num_shards
        self._durability_mode = durability_mode
        per_shard_queue = max(1024, (queue_max + num_shards - 1) // num_shards)
        self._shards: list[_FlusherShard] = [
            _FlusherShard(
                shard_id=i,
                session_dir_resolver=session_dir_resolver,
                chat_meta_resolver=chat_meta_resolver,
                flush_interval_ms=flush_interval_ms,
                batch_max=batch_max,
                queue_max=per_shard_queue,
                durability_mode=durability_mode,
            )
            for i in range(num_shards)
        ]

    @property
    def num_shards(self) -> int:
        return self._num_shards

    @property
    def durability_mode(self) -> str:
        return self._durability_mode

    async def start(self) -> None:
        await asyncio.gather(*(s.start() for s in self._shards))

    async def stop(self, *, drain_timeout: float = 20.0) -> None:
        await asyncio.gather(*(
            s.stop(drain_timeout=drain_timeout) for s in self._shards
        ))

    def enqueue(self, sid: str, event: Event) -> None:
        """Hot-path enqueue. Routes to `shards[hash(sid) % N]`."""
        idx = _shard_for_sid(sid, self._num_shards)
        self._shards[idx].enqueue(sid, event)

    async def flush(self) -> None:
        """Wait until every shard's queue is empty + drained. Used on"""
        await asyncio.gather(*(s.flush() for s in self._shards))


    @property
    def dropped(self) -> int:
        return sum(s.dropped for s in self._shards)

    @property
    def written(self) -> int:
        return sum(s.written for s in self._shards)

    @property
    def batch_count(self) -> int:
        return sum(s.batch_count for s in self._shards)

    @property
    def last_batch_size(self) -> int:
        # Snapshot of the last batch size across shards (max is the
        # most useful single number for "how busy was the flusher").
        return max((s.last_batch_size for s in self._shards), default=0)

    def shard_stats(self) -> list[dict]:
        """Per-shard breakdown -- useful for debugging hot shards"""
        return [
            {
                "shard_id": i,
                "written": s.written,
                "dropped": s.dropped,
                "batches": s.batch_count,
                "queue_size": s._queue.qsize(),
                "last_batch_size": s.last_batch_size,
            }
            for i, s in enumerate(self._shards)
        ]

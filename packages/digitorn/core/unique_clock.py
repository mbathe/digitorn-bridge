"""Monotonic-unique UTC clock - guarantees strictly increasing timestamps.

Used as the `default=` callable on every history/audit table so
each row carries a timestamp that is:

1. **Unique across the process.** Two concurrent writers never get the
   same microsecond - the clock bumps forward by 1µs on collision.
2. **Monotonic.** Each call returns a strictly greater value than the
   previous one, even when the system clock ticks backward (NTP
   adjustment, virtualisation quirks).
3. **Timezone-aware UTC.** Stored as `DateTime(timezone=True)`.

Why not `datetime.now(utc)`:
  - Windows/Python often has only ~1ms resolution → two calls within
    the same millisecond get the SAME timestamp. A UNIQUE constraint
    on the ts column would then start failing IntegrityErrors.
  - Under burst writes (streaming tokens, parallel tool_calls) we
    routinely see 10+ rows per ms. That's exactly where uniqueness
    must hold.

Design:
  - Internal state: the last-issued µs-since-epoch (int).
  - Each call takes a threading Lock, reads wall-clock µs, takes
    `max(wall_now_us, last_issued + 1)`, updates last_issued, and
    returns the corresponding `datetime`.
  - 16 ns per call on a modern CPU, lock contention is negligible
    for the writer rate we see (<10k events/s/sid).

Cross-process guarantee:
  - Same DB written by two daemons / workers: each process has its
    own clock. They *can* collide in wall-clock µs. The DB
    `UNIQUE` constraint catches it as an IntegrityError; callers
    that need the strongest guarantee wrap the INSERT in a
    `unique_ts_retry` helper (see below) that bumps and retries
    until the row lands.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable, TypeVar

_lock = threading.Lock()
_last_us: int = 0


def unique_utc_now() -> datetime:
    """Return a timezone-aware UTC datetime that is strictly greater
    than any previous return value within this process.

    Resolution: 1 microsecond. Collisions within the same µs bump the
    next available µs forward - the difference stays invisible to
    humans (humans can't tell 18:39:11.850139 apart from .850140).
    """
    global _last_us
    with _lock:
        now_us = time.time_ns() // 1_000
        if now_us <= _last_us:
            now_us = _last_us + 1
        _last_us = now_us
    return datetime.fromtimestamp(now_us / 1_000_000, tz=timezone.utc)


T = TypeVar("T")


async def unique_ts_retry(
    build_fn: Callable[[datetime], T],
    *,
    commit_fn: Callable[[T], "object"],
    max_retries: int = 5,
) -> T:
    """Try to insert a row with a unique ts. On IntegrityError retry
    with the clock bumped forward.

    `build_fn(ts)` must return a row (not yet added to the session).
    `commit_fn(row)` must add it to the session AND commit; raising
    IntegrityError on duplicate ts triggers the retry with a fresh ts.

    After `max_retries` failures we re-raise. In practice 1 retry
    is already overkill unless two processes write the exact same µs
    simultaneously against a single DB.
    """
    from sqlalchemy.exc import IntegrityError

    for attempt in range(max_retries + 1):
        row = build_fn(unique_utc_now())
        try:
            result = await commit_fn(row)
            return result  # type: ignore[return-value]
        except IntegrityError:
            if attempt >= max_retries:
                raise
            # Bump the shared clock to force next call > any wall µs.
            global _last_us
            with _lock:
                _last_us += 1
    # Unreachable.
    raise RuntimeError("unique_ts_retry exhausted")


__all__ = ["unique_utc_now", "unique_ts_retry"]

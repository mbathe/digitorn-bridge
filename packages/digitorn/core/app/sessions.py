"""SessionStore - backend-agnostic persistent session storage.

Sessions expire after idle timeout (30 min) or absolute timeout (24 h).
Both are configurable and can be disabled.

Supports DiskCache (default, zero-config) and Redis (multi-host production)
via the ``KeyValueBackend`` abstraction.

Usage::

    store = SessionStore()

    store = SessionStore(backend_url="redis://localhost:6379/0")

    store.put(session)
    session = store.get("app", "session")
    store.delete("app", "session")
    store.list_for_app("app")
    store.delete_for_app("app")
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from digitorn.core.kv import KeyValueBackend, create_backend

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path.home() / ".digitorn" / "sessions"

_DEFAULT_USER = "local"


@dataclass
class ConversationSession:
    """A stateful conversation session for a deployed app."""

    session_id: str
    app_id: str
    user_id: str = _DEFAULT_USER
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    title: str = ""
    memory_snapshot: dict[str, Any] = field(default_factory=dict)
    turn_count: int = 0
    workspace: str = ""  # Persisted workspace path - set on first chat, reused on subsequent turns
    # Interruption tracking - enables smart resume
    interrupted: bool = False  # True if session didn't end cleanly
    interrupted_at: float = 0.0

    def add_system(self, content: str) -> None:
        if not self.messages:
            self.messages.append({"role": "system", "content": content})

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})
        self.last_active = time.time()
        if not self.title and len(content) > 0:
            self.title = content[:80]

    def add_assistant(self, content: str) -> None:
        self.messages.append({"role": "assistant", "content": content})
        # Keep ``last_active`` fresh so the session drawer's sort
        # (most-recent first) reflects "the assistant just replied"
        # and not merely "the user last typed". Without this, a long
        # turn that fires many ``add_assistant`` appends would leave
        # the drawer order stale for the duration of the turn.
        self.last_active = time.time()

    def summary(self) -> dict[str, Any]:
        """Rich summary suitable for list rendering.

        Includes a best-effort ``last_message_preview`` (last
        assistant/user content, trimmed to 200 chars) so the client
        can render chat cards without a second fetch. Token /cost
        fields are 0 here and joined on top in ``AppManager.list_sessions``
        via the UsageStore when available.
        """
        preview = ""
        last_role = ""
        if self.messages:
            for msg in reversed(self.messages):
                role = msg.get("role", "")
                if role in ("assistant", "user"):
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        # Multimodal - find the first text part
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                content = part.get("text", "")
                                break
                        else:
                            content = ""
                    if isinstance(content, str) and content:
                        preview = content[:200]
                        last_role = role
                        break
        return {
            "session_id": self.session_id,
            "app_id": self.app_id,
            "user_id": self.user_id,
            "title": self.title,
            "message_count": len(self.messages),
            "turn_count": self.turn_count,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "workspace": self.workspace,
            "interrupted": self.interrupted,
            "last_message_preview": preview,
            "last_message_role": last_role,
            # Filled by AppManager.list_sessions on top of this dict
            # when a deployed app + usage store are available
            "app_name": None,
            "app_icon": None,
            "app_color": None,
            "tokens": 0,
            "cost_usd": 0.0,
            "last_error": None,
        }


_DEFAULT_IDLE_TTL = 1800     # 30 min idle timeout
_DEFAULT_ABSOLUTE_TTL = 86400  # 24 h absolute timeout


def _configured_idle_ttl() -> float:
    """Return idle TTL from config, falling back to hardcoded default."""
    try:
        from digitorn.core.config import get_settings
        return get_settings().session.idle_ttl
    except Exception:
        return _DEFAULT_IDLE_TTL


def _configured_absolute_ttl() -> float:
    """Return absolute TTL from config, falling back to hardcoded default."""
    try:
        from digitorn.core.config import get_settings
        return get_settings().session.absolute_ttl
    except Exception:
        return _DEFAULT_ABSOLUTE_TTL


class _DistributedRedisLock:
    """Redis-based distributed lock compatible with asyncio.Lock interface.

    Uses Redis SET NX EX for cross-process mutual exclusion. Compatible
    with the manager.py pattern::

        lock = store.session_lock(app_id, session_id)
        await asyncio.wait_for(lock.acquire(), timeout=60)
        try:
            ...
        finally:
            lock.release()  # sync call, OK

    The lock is automatically released after ``timeout`` seconds (Redis
    key TTL) even if the holder crashes, preventing deadlocks.
    """

    def __init__(self, redis_client: Any, key: str, timeout: int = 90) -> None:
        self._redis = redis_client  # sync redis.Redis client
        self._lock_key = f"dlock:{key}"
        self._timeout = timeout
        self._token: str | None = None

    async def acquire(self) -> bool:
        """Acquire the distributed lock (polls Redis until granted).

        Uses ``SET NX EX`` (via ``_try_acquire``) so the first caller
        wins and the key auto-expires after ``self._timeout`` seconds if
        the holder crashes. An earlier version of this loop did an
        unconditional ``SET`` first "to pre-populate" which clobbered
        the key and made the NX check fail forever - the loop then
        spun until the caller's ``wait_for`` timeout fired and the
        turn never started.
        """
        token = _uuid.uuid4().hex
        while True:
            acquired = await asyncio.to_thread(
                self._try_acquire, token,
            )
            if acquired:
                self._token = token
                return True
            await asyncio.sleep(0.05)

    def _try_acquire(self, token: str) -> bool:
        """Sync helper: SET NX EX in one call."""
        return bool(self._redis.set(
            self._lock_key, token, nx=True, ex=self._timeout,
        ))

    def release(self) -> None:
        """Release the lock (sync, safe to call from finally block).

        Uses a Lua script for atomic check-and-delete so only the
        lock holder can release it (no accidental release of
        someone else's lock after TTL expiry + re-acquire).
        """
        if self._token is None:
            return
        try:
            _RELEASE_SCRIPT = (
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) "
                "else return 0 end"
            )
            self._redis.eval(_RELEASE_SCRIPT, 1, self._lock_key, self._token)
        except Exception:
            pass
        self._token = None

    def locked(self) -> bool:
        """Check if the lock is currently held (by anyone)."""
        try:
            return bool(self._redis.exists(self._lock_key))
        except Exception:
            return False


class SessionStore:
    """Backend-agnostic persistent session store.

    Sessions expire after ``idle_ttl`` seconds of inactivity or
    ``absolute_ttl`` seconds after creation (whichever comes first).

    Args:
        directory: Path for DiskCache storage (ignored if backend_url is Redis).
        backend_url: Backend URL - ``redis://...`` for Redis, filesystem path
                     or None for DiskCache.
        backend: Pre-constructed backend instance (overrides url/directory).
        idle_ttl: Idle timeout in seconds (default 30 min). None = no idle timeout.
        absolute_ttl: Absolute timeout in seconds (default 24 h). None = no absolute timeout.
    """

    def __init__(
        self,
        directory: str | Path | None = None,
        backend_url: str | None = None,
        backend: KeyValueBackend | None = None,
        ttl: float | None = None,
        idle_ttl: float | None = None,
        absolute_ttl: float | None = None,
    ) -> None:
        self._idle_ttl = idle_ttl if idle_ttl is not None else _configured_idle_ttl()
        self._absolute_ttl = absolute_ttl if absolute_ttl is not None else _configured_absolute_ttl()
        if backend is not None:
            self._backend = backend
        elif backend_url:
            self._backend = create_backend(backend_url)
        else:
            self._backend = create_backend(
                directory=directory or _DEFAULT_DIR,
                size_limit=2**30,
            )
        self._index_key = "__app_index__"
        # Sharded index locks: one RLock per app_id to reduce contention.
        # Sessions from different apps never block each other.
        self._index_locks: dict[str, threading.RLock] = {}
        self._index_locks_guard = threading.Lock()  # Only protects lock creation
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._locks_lock = threading.RLock()

        # Detect Redis backend for distributed locks
        self._redis_client = self._detect_redis_client(self._backend)
        if self._redis_client:
            logger.info("session_store_distributed_locks_enabled")

    @staticmethod
    def _detect_redis_client(backend: Any = None) -> Any:
        """Extract the raw sync Redis client from the backend, if Redis."""
        # ResilientRedisBackend wraps a RedisBackend
        inner = getattr(backend, '_redis', None)
        if inner is not None:
            # ResilientRedisBackend._redis is a RedisBackend instance
            raw = getattr(inner, '_redis', None)
            if raw is not None:
                return raw
        # Direct RedisBackend
        raw = getattr(backend, '_redis', None)
        if raw is not None:
            return raw
        return None

    def session_lock(self, app_id: str, session_id: str, user_id: str = _DEFAULT_USER) -> "asyncio.Lock | _DistributedRedisLock":
        """Return a lock scoped to a specific session.

        If the backend is Redis, returns a ``_DistributedRedisLock`` that
        works across processes (multi-worker safe). Otherwise returns an
        ``asyncio.Lock`` (single-process only).

        Thread-safe AND coroutine-safe via `_locks_lock` (threading.Lock).
        """
        key = self._key(app_id, session_id, user_id)

        # Distributed lock - always create fresh (state lives in Redis)
        if self._redis_client is not None:
            return _DistributedRedisLock(self._redis_client, key)

        # In-process lock - cached per key
        with self._locks_lock:
            existing = self._session_locks.get(key)
            if existing is not None:
                return existing
            new_lock = asyncio.Lock()
            self._session_locks[key] = new_lock
            return new_lock

    def _cleanup_orphan_locks(self, max_locks: int = 10000) -> int:
        """Remove session locks for sessions that no longer exist.

        Called periodically to prevent unbounded growth of _session_locks
        across the daemon's lifetime. Returns the number of locks removed.
        """
        if len(self._session_locks) <= max_locks:
            return 0
        with self._locks_lock:
            # Snapshot keys, then check each against backend
            keys = list(self._session_locks.keys())
            removed = 0
            for key in keys:
                try:
                    parts = key.split(":", 2)
                    if len(parts) != 3:
                        # Invalid key format - drop it
                        self._session_locks.pop(key, None)
                        removed += 1
                        continue
                    app_id, user_id, session_id = parts
                    # If session doesn't exist in backend, drop the lock
                    if not self._backend.exists(self._key(app_id, session_id, user_id)):
                        # Don't drop if currently locked
                        lock = self._session_locks.get(key)
                        if lock is not None and not lock.locked():
                            self._session_locks.pop(key, None)
                            removed += 1
                except Exception:
                    pass
            return removed

    @staticmethod
    def _validate_id(value: str, name: str = "id") -> str:
        import re
        if not value or len(value) > 256:
            raise ValueError(f"Invalid {name}: must be 1-256 chars")
        if not re.match(r'^[a-zA-Z0-9_\-\.]+$', value):
            raise ValueError(f"Invalid {name}: only alphanumeric, dash, underscore, dot allowed")
        return value

    @staticmethod
    def _key(app_id: str, session_id: str, user_id: str = _DEFAULT_USER) -> str:
        return f"{app_id}:{user_id}:{session_id}"

    def put(self, session: ConversationSession) -> None:
        """Store or update a session (permanent, no expiration)."""
        key = self._key(session.app_id, session.session_id, session.user_id)
        self._backend.set(key, session)
        self._index_add(session.app_id, key)
        self._index_add(f"user:{session.user_id}:{session.app_id}", key)

    def _is_expired(self, session: ConversationSession) -> bool:
        """Check if a session has expired (idle or absolute timeout)."""
        now = time.time()
        if self._idle_ttl and (now - session.last_active > self._idle_ttl):
            return True
        if self._absolute_ttl and (now - session.created_at > self._absolute_ttl):
            return True
        return False

    def get_any_owner(self, app_id: str, session_id: str) -> str | None:
        """Return the user_id that owns this (app_id, session_id) if any.

        Used by the authorization helper in the API layer to detect
        "this sid already belongs to a different user" WITHOUT leaking
        the session payload itself. Scans the per-app index (cheap)
        and matches against the key format ``{app_id}:{user_id}:{sid}``.
        """
        try:
            keys = self._index_get(app_id) or set()
        except Exception:
            return None
        suffix = f":{session_id}"
        for key in keys:
            if not isinstance(key, str) or not key.endswith(suffix):
                continue
            # key = "{app_id}:{user_id}:{session_id}" - middle piece is owner
            parts = key.split(":", 2)
            if len(parts) == 3:
                return parts[1]
        return None

    def get(self, app_id: str, session_id: str, user_id: str = _DEFAULT_USER) -> ConversationSession | None:
        """Get a session from the hot-path cache. Returns None on miss.

        This cache is a **performance accelerator only** - the durable
        source of truth for sessions + messages is the DB (``user_sessions``
        + ``session_messages`` tables). Callers must ALWAYS be prepared
        for a None here and fall back to DB rehydration via
        ``AppManager.get_session_or_rebuild``.

        On expiry the cache entry is evicted so the next call will rebuild
        from DB through the manager's fallback path.
        """
        key = self._key(app_id, session_id, user_id)
        session = self._backend.get(key)
        if session is not None and self._is_expired(session):
            logger.info("session_cache_evicted app=%s session=%s reason=%s",
                        app_id, session_id,
                        "idle" if self._idle_ttl and (time.time() - session.last_active > self._idle_ttl) else "absolute")
            self._backend.delete(key)
            self._index_remove(app_id, key)
            self._index_remove(f"user:{user_id}:{app_id}", key)
            with self._locks_lock:
                self._session_locks.pop(key, None)
            return None
        return session

    def touch(self, app_id: str, session_id: str, user_id: str = _DEFAULT_USER) -> None:
        """Update last_active timestamp for a session."""
        key = self._key(app_id, session_id, user_id)
        session = self._backend.get(key)
        if session is not None:
            session.last_active = time.time()
            self._backend.set(key, session)

    def delete(self, app_id: str, session_id: str, user_id: str = _DEFAULT_USER) -> bool:
        """Delete a session. Returns True if it existed."""
        key = self._key(app_id, session_id, user_id)
        existed = self._backend.delete(key)
        if existed:
            self._index_remove(app_id, key)
            self._index_remove(f"user:{user_id}:{app_id}", key)
        # Also clean up the :messages, :events keys and session lock
        self._backend.delete(f"{key}:messages")
        self._backend.delete(f"{key}:events")
        with self._locks_lock:
            self._session_locks.pop(key, None)
        return existed

    def list_for_user(
        self,
        app_id: str,
        user_id: str = _DEFAULT_USER,
        limit: int = 0,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List sessions for a specific user on an app.

        Results are sorted by last_active descending (most recent first).
        Use ``limit`` / ``offset`` for pagination (0 = no limit).
        """
        keys = self._index_get(f"user:{user_id}:{app_id}")
        result = []
        stale = []
        for key in keys:
            session = self._backend.get(key)
            if session is None:
                stale.append(key)
            else:
                result.append(session.summary())
        for key in stale:
            self._index_remove(f"user:{user_id}:{app_id}", key)
        result.sort(key=lambda s: s["last_active"], reverse=True)
        if offset:
            result = result[offset:]
        if limit:
            result = result[:limit]
        return result

    def count_for_user(self, app_id: str, user_id: str = _DEFAULT_USER) -> int:
        """Count total sessions for a user on an app (for pagination)."""
        keys = self._index_get(f"user:{user_id}:{app_id}")
        return len(keys)

    def delete_for_app(self, app_id: str) -> int:
        """Delete all sessions for an app. Returns count deleted.

        Holds _index_lock for the entire delete loop so concurrent get() calls
        don't read sessions that are about to be deleted (stale data).
        """
        count = 0
        with self._get_index_lock(app_id):
            keys = self._index_pop(app_id)
            for key in keys:
                try:
                    if self._backend.delete(key):
                        count += 1
                    # Also clean :messages and :events
                    self._backend.delete(f"{key}:messages")
                    self._backend.delete(f"{key}:events")
                except Exception:
                    pass
        # Clean up session locks for this app (separate lock)
        with self._locks_lock:
            stale_locks = [k for k in self._session_locks if k.startswith(f"{app_id}:")]
            for k in stale_locks:
                del self._session_locks[k]
        return count

    def list_for_app(
        self,
        app_id: str,
        limit: int = 0,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List session summaries for an app (O(k))."""
        keys = self._index_get(app_id)
        result = []
        stale = []
        for key in keys:
            session = self._backend.get(key)
            if session is not None:
                result.append(session.summary())
            else:
                stale.append(key)
        if stale:
            self._index_remove_many(app_id, stale)
        result.sort(key=lambda s: s["last_active"], reverse=True)
        if offset:
            result = result[offset:]
        if limit:
            result = result[:limit]
        return result

    def save_messages(
        self, app_id: str, session_id: str, messages: list[dict],
        user_id: str = _DEFAULT_USER,
    ) -> None:
        """Persist conversation messages for session resume."""
        import json
        key = f"{self._key(app_id, session_id, user_id)}:messages"
        self._backend.set(key, json.dumps(messages, default=str))

    def load_messages(
        self, app_id: str, session_id: str, user_id: str = _DEFAULT_USER,
    ) -> list[dict] | None:
        """Load persisted messages for session resume. Returns None if not found."""
        import json
        key = f"{self._key(app_id, session_id, user_id)}:messages"
        data = self._backend.get(key)
        if data is None:
            return None
        return json.loads(data)

    # ── Event log persistence ─────────────────────────────────────────────

    def save_events(
        self, app_id: str, session_id: str, events: list[dict],
        user_id: str = _DEFAULT_USER,
    ) -> None:
        """Persist the full event log for a session (legacy, single blob).

        NOTE: This overwrites the entire log. Prefer ``save_turn_events`` for
        per-turn persistence that preserves history across turns.
        """
        import json
        key = f"{self._key(app_id, session_id, user_id)}:events"
        self._backend.set(key, json.dumps(events, default=str))

    def save_turn_events(
        self, app_id: str, session_id: str, turn_index: int,
        events: list[dict], user_id: str = _DEFAULT_USER,
    ) -> None:
        """Persist events for a specific turn (preserves previous turns).

        Uses a turn-scoped key so each turn has its own log. The full
        history is reconstructed by ``load_events()`` which aggregates all
        turn logs in order. This pattern enables:
        - Saving events in real-time during a turn without losing history
        - Replaying a specific turn by index
        - Crash safety: partial turn logs survive daemon restarts
        """
        import json
        key = f"{self._key(app_id, session_id, user_id)}:events:turn:{turn_index}"
        self._backend.set(key, json.dumps(events, default=str))
        # Track the list of turn indices in an index key
        idx_key = f"{self._key(app_id, session_id, user_id)}:events:turns"
        existing = self._backend.get(idx_key)
        try:
            turns = json.loads(existing) if existing else []
        except Exception:
            turns = []
        if turn_index not in turns:
            turns.append(turn_index)
            turns.sort()
            self._backend.set(idx_key, json.dumps(turns))

    def load_events(
        self, app_id: str, session_id: str, user_id: str = _DEFAULT_USER,
    ) -> list[dict] | None:
        """Load the full event log - aggregates all turn event logs in order.

        Returns events from all turns concatenated chronologically. Falls
        back to the legacy single-blob format if no turn-scoped logs exist.
        """
        import json
        base = self._key(app_id, session_id, user_id)

        # Try turn-scoped format first (new)
        idx_data = self._backend.get(f"{base}:events:turns")
        if idx_data:
            try:
                turns = json.loads(idx_data)
                aggregated: list[dict] = []
                for turn_idx in sorted(turns):
                    turn_data = self._backend.get(f"{base}:events:turn:{turn_idx}")
                    if turn_data:
                        try:
                            aggregated.extend(json.loads(turn_data))
                        except Exception:
                            continue
                if aggregated:
                    return aggregated
            except Exception:
                pass

        # Fall back to legacy single-blob format
        data = self._backend.get(f"{base}:events")
        if data is None:
            return None
        try:
            return json.loads(data)
        except Exception:
            return None

    def append_events(
        self, app_id: str, session_id: str, new_events: list[dict],
        user_id: str = _DEFAULT_USER,
    ) -> None:
        """Append events to the existing log (load + extend + save)."""
        if not new_events:
            return
        existing = self.load_events(app_id, session_id, user_id) or []
        existing.extend(new_events)
        self.save_events(app_id, session_id, existing, user_id)

    def fork(self, app_id: str, source_id: str, new_id: str) -> bool:
        """Fork a session: copy messages + session state to a new session ID."""
        source = self.get(app_id, source_id)
        if source is None:
            return False

        from digitorn.core.app.sessions import ConversationSession
        new_session = ConversationSession(
            session_id=new_id,
            app_id=app_id,
            messages=list(source.messages),
        )
        self.put(new_session)

        messages = self.load_messages(app_id, source_id)
        if messages:
            self.save_messages(app_id, new_id, messages)

        events = self.load_events(app_id, source_id)
        if events:
            self.save_events(app_id, new_id, events)

        return True

    def recover_orphans(self) -> int:
        """Recover sessions from orphaned message stores.

        When a session object was lost (e.g. old TTL expiration) but its
        :messages key persists, this rebuilds the session object.
        Returns the count of recovered sessions.
        """
        import json

        recovered = 0
        # Access underlying diskcache.Cache for key iteration
        cache = getattr(self._backend, "_cache", None)
        if cache is None:
            return 0
        try:
            all_keys = list(cache.iterkeys())
        except Exception:
            return 0

        for key in all_keys:
            k = str(key)
            if not k.endswith(":messages"):
                continue
            session_key = k[: -len(":messages")]
            # Skip if session object already exists
            if self._backend.get(session_key) is not None:
                continue
            # Parse key: app_id:user_id:session_id
            parts = session_key.split(":", 2)
            if len(parts) != 3:
                continue
            app_id, user_id, session_id = parts
            # Load messages
            raw = self._backend.get(key)
            if raw is None:
                continue
            try:
                messages = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue
            if not messages:
                continue
            # Rebuild session
            title = ""
            for m in messages:
                if m.get("role") == "user" and m.get("content"):
                    title = str(m["content"])[:80]
                    break
            session = ConversationSession(
                session_id=session_id,
                app_id=app_id,
                user_id=user_id,
                messages=messages,
                title=title,
            )
            self.put(session)
            recovered += 1
            logger.info("recovered_orphan_session app=%s sid=%s user=%s msgs=%d",
                        app_id, session_id, user_id, len(messages))

        return recovered

    def close(self) -> None:
        """Close the underlying backend."""
        self._backend.close()

    def evict_expired(self) -> int:
        """Proactively remove expired sessions. Returns count of evicted sessions.

        Called periodically (e.g., every 60s) to prevent memory leak from
        sessions that expire but are never accessed again.
        """
        evicted = 0
        try:
            all_keys = list(self._backend.iterkeys()) if hasattr(self._backend, "iterkeys") else []
            for key in all_keys:
                if key.startswith("__") or key.startswith("user:"):
                    continue  # Skip index keys
                try:
                    session = self._backend.get(key)
                    if session is not None and isinstance(session, ConversationSession) and self._is_expired(session):
                        self.delete(session.app_id, session.session_id, session.user_id)
                        evicted += 1
                except Exception:
                    pass
        except Exception:
            pass
        if evicted > 0:
            logger.info("evict_expired_sessions count=%d", evicted)
        return evicted

    @property
    def stats(self) -> dict[str, Any]:
        """Return store statistics."""
        info: dict[str, Any] = {
            "idle_ttl": self._idle_ttl,
            "absolute_ttl": self._absolute_ttl,
        }
        if hasattr(self._backend, "volume"):
            info["disk_size_bytes"] = self._backend.volume
        if hasattr(self._backend, "__len__"):
            info["total_keys"] = len(self._backend)
        return info


    def _get_index_lock(self, app_id: str) -> threading.RLock:
        """Return the index lock for a specific app_id (lazy-created)."""
        lock = self._index_locks.get(app_id)
        if lock is not None:
            return lock
        with self._index_locks_guard:
            # Double-check after acquiring guard
            if app_id not in self._index_locks:
                self._index_locks[app_id] = threading.RLock()
            return self._index_locks[app_id]

    def _index_key_for(self, app_id: str) -> str:
        return f"{self._index_key}{app_id}"

    def _index_get(self, app_id: str) -> set[str]:
        try:
            return self._backend.get(self._index_key_for(app_id), set())
        except Exception:
            logger.warning("Failed to read session index for app %s", app_id, exc_info=True)
            return set()

    def _index_add(self, app_id: str, key: str) -> None:
        idx_key = self._index_key_for(app_id)
        try:
            with self._get_index_lock(app_id):
                keys = self._backend.get(idx_key, set())
                keys.add(key)
                self._backend.set(idx_key, keys)
        except Exception:
            logger.warning("Failed to add session index entry for app %s", app_id, exc_info=True)

    def _index_remove(self, app_id: str, key: str) -> None:
        idx_key = self._index_key_for(app_id)
        try:
            with self._get_index_lock(app_id):
                keys = self._backend.get(idx_key, set())
                keys.discard(key)
                if keys:
                    self._backend.set(idx_key, keys)
                else:
                    self._backend.delete(idx_key)
        except Exception:
            logger.warning("Failed to remove session index entry for app %s", app_id, exc_info=True)

    def _index_remove_many(self, app_id: str, stale_keys: list[str]) -> None:
        idx_key = self._index_key_for(app_id)
        with self._get_index_lock(app_id):
            keys = self._backend.get(idx_key, set())
            keys -= set(stale_keys)
            if keys:
                self._backend.set(idx_key, keys)
            else:
                self._backend.delete(idx_key)

    def _index_pop(self, app_id: str) -> set[str]:
        idx_key = self._index_key_for(app_id)
        with self._get_index_lock(app_id):
            keys = self._backend.get(idx_key, set())
            self._backend.delete(idx_key)
            return keys

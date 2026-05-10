"""LegacySessionStoreAdapter: sync facade over ``InMemorySessionStore``
that mimics the legacy ``digitorn.core.app.sessions.SessionStore`` API.

Why this exists: ``manager_v2`` and the ``apps_v2`` API layer call
~65 different methods on the legacy ``SessionStore`` (``get``, ``put``,
``session_lock(app, sid, uid)``, ``save_messages``, ``load_messages``,
``list_for_app``, ``recover_orphans``, ``_backend``, ``_index_get``,
...). Rewriting every callsite at once is risky. This adapter
preserves the OLD API surface while routing reads/writes through the
new ``InMemorySessionStore``, so the daemon can run on the new
filesystem-first storage WITHOUT touching any callsite.

Phase 3 plan:
  1. Drop in this adapter (you are here). All legacy callsites work
     unchanged. Single source of truth = InMemorySessionStore.
  2. File by file, replace ``self._session_store.foo(...)`` with the
     native ``InMemorySessionStore`` API (no adapter call).
  3. Once no consumer remains, this adapter file is deleted.

Sync-vs-async bridge: legacy callers use
``await asyncio.to_thread(self._session_store.fn, ...)``. The adapter
methods are SYNC. Methods that wrap async store ops use
``run_coroutine_threadsafe`` against the loop captured at construction
time. That keeps the legacy call shape (sync from a worker thread)
working without changing the call sites.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, TYPE_CHECKING

from digitorn.core.runtime.session_store.store import InMemorySessionStore

if TYPE_CHECKING:
    # ConversationSession lives in the legacy module; we import it
    # lazily so this adapter file stays usable even after the legacy
    # module gets deleted in Phase 4 (the only remaining caller of
    # ``get`` / ``put`` will have moved to the native API by then).
    from digitorn.core.app.sessions import ConversationSession

logger = logging.getLogger(__name__)

_DEFAULT_USER = "local"
_BRIDGE_CALL_TIMEOUT = 30.0

# Names Python looks up internally that should NOT be shimmed by the
# adapter's catch-all ``__getattr__`` (would mess with copy, pickling,
# repr, etc.).
_ADAPTER_KNOWN_PRIVATE: set[str] = {
    "__class__", "__dict__", "__weakref__", "__init__", "__del__",
    "__repr__", "__str__", "__hash__", "__eq__", "__ne__",
    "__getstate__", "__setstate__", "__reduce__", "__reduce_ex__",
    "__copy__", "__deepcopy__", "__sizeof__", "__dir__", "__format__",
}
# Method names already noisy-logged by the catch-all so we don't
# re-warn 1000 times per missing method.
_ADAPTER_LOGGED_MISSING: set[str] = set()


def _parse_iso_to_epoch(s: str | None) -> float:
    """Parse an ISO timestamp to a unix epoch float. Returns the
    current time on parse failure -- legacy ConversationSession needs
    a numeric ``last_active`` no matter what."""
    if not s:
        return time.time()
    try:
        # tolerate trailing Z
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return time.time()


class _NoOpKVBackend:
    """Stub for callers that still poke ``store._backend`` (the legacy
    KV pointer used by the old JobStore + QuotaStore). All ops are
    no-ops; reads return None / empty / False so the call site
    gracefully degrades. Phase 3 swaps the JobStore consumer to
    ``FileJobStore``; this stub catches any straggler import."""

    def get(self, key: str, default: Any = None) -> Any:
        return default

    def set(self, key: str, value: Any) -> None:  # pragma: no cover
        pass

    def delete(self, key: str) -> bool:
        return False

    def exists(self, key: str) -> bool:
        return False

    def keys(self, pattern: str = "*") -> list[str]:
        return []


class LegacySessionStoreAdapter:
    """Sync facade over ``InMemorySessionStore`` exposing the legacy
    ``SessionStore`` surface."""

    def __init__(self, store: InMemorySessionStore) -> None:
        self._store = store
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        # Stub kept so legacy callers like
        # ``QuotaStore(session_store._backend)`` don't crash. JobStore
        # has already migrated to FileJobStore; QuotaStore lives in
        # ``app/olds/`` and gets deleted in Phase 4.
        self._backend = _NoOpKVBackend()
        # Compatibility: some legacy code reads
        # ``session_store._idle_ttl`` or ``._absolute_ttl``. The new
        # design has no expiry -- sessions are permanent. Expose 0 so
        # those readers see "no expiry".
        self._idle_ttl = 0.0
        self._absolute_ttl = 0.0

    # ── Sync↔async bridge ────────────────────────────────────────────

    def _run(self, coro, timeout: float = _BRIDGE_CALL_TIMEOUT):
        """Drive an async coroutine to completion from a sync caller.

        Three modes:
          * If we captured a running loop and the caller is on a worker
            thread (the typical ``asyncio.to_thread`` case), schedule
            the coro on the loop via ``run_coroutine_threadsafe``.
          * If we're on the loop's own thread, raise (caller should
            ``await`` natively, not wrap in to_thread).
          * If no loop is captured (e.g. during ``__init__`` before
            FastAPI lifespan started), spin up a one-shot loop with
            ``asyncio.run``.
        """
        if self._loop is None or not self._loop.is_running():
            return asyncio.run(coro)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            # Caller is on the loop -- there's no thread to run from.
            # The legacy callers all use ``asyncio.to_thread``, so this
            # is an integration error. Surface it loudly.
            raise RuntimeError(
                "LegacySessionStoreAdapter sync method called directly "
                "from the event loop. Wrap with asyncio.to_thread, or "
                "migrate the call site to the native InMemorySessionStore "
                "async API."
            )
        return asyncio.run_coroutine_threadsafe(
            coro, self._loop,
        ).result(timeout=timeout)

    # ── Identity / locking ───────────────────────────────────────────

    def session_lock(
        self, app_id: str, session_id: str, user_id: str = _DEFAULT_USER,
    ) -> asyncio.Lock:
        """Per-session asyncio.Lock. The new store keys lock by ``sid``
        only (the new sid space is uuid-based, app+user namespacing is
        no longer needed). app_id and user_id are accepted for source
        compatibility but ignored."""
        return self._store.session_lock(session_id)

    # ── Read paths ───────────────────────────────────────────────────

    def get(
        self, app_id: str, session_id: str, user_id: str = _DEFAULT_USER,
    ) -> "ConversationSession | None":
        """Return a ConversationSession view of the session in the new
        store, or None if the session does not exist or belongs to a
        different app.

        Loads from disk on cache miss. Never raises -- returning None
        on any error matches the legacy contract."""
        state = self._store.state(session_id)
        if state is None:
            try:
                state = self._run(self._store.open(
                    session_id,
                    app_id=app_id or "", user_id=user_id or _DEFAULT_USER,
                    create_if_missing=False, pin=False,
                ))
            except (KeyError, RuntimeError):
                return None
            except Exception as exc:
                logger.debug(
                    "legacy_adapter_get_open_failed sid=%s err=%s",
                    session_id, exc,
                )
                return None
        if state is None:
            return None
        if app_id and state.app_id and state.app_id != app_id:
            # Cross-app read: legacy returned None.
            return None
        return self._state_to_conv_session(state)

    def get_any_owner(self, app_id: str, session_id: str) -> str | None:
        """Cross-user lookup. Forwards to the native async method."""
        try:
            return self._run(self._store.get_any_owner(app_id, session_id))
        except Exception as exc:
            logger.debug(
                "legacy_adapter_get_any_owner_failed sid=%s err=%s",
                session_id, exc,
            )
            return None

    def load_messages(
        self, app_id: str, session_id: str, user_id: str = _DEFAULT_USER,
    ) -> list[dict[str, Any]]:
        """LLM-shaped messages list ([{"role": ..., "content": ...},
        ...]) derived from ``state.messages``. The legacy callers feed
        this directly to LiteLLM."""
        state = self._store.state(session_id)
        if state is None:
            try:
                state = self._run(self._store.open(
                    session_id,
                    app_id=app_id or "", user_id=user_id or _DEFAULT_USER,
                    create_if_missing=False, pin=False,
                ))
            except (KeyError, RuntimeError):
                return []
            except Exception:
                return []
        if state is None:
            return []
        out: list[dict[str, Any]] = []
        for m in state.messages:
            row: dict[str, Any] = {"role": m.role, "content": m.content}
            if getattr(m, "tool_calls", None):
                row["tool_calls"] = m.tool_calls
            out.append(row)
        return out

    # ── Write paths ──────────────────────────────────────────────────

    def put(self, session: "ConversationSession") -> None:
        """Apply the chat-level fields from a ConversationSession onto
        the corresponding SessionState. Auto-opens the session if not
        loaded.

        Why mutate state directly: ``workspace`` / ``workdir`` are
        not derived from events -- they're paths the manager attaches
        on session create. The new design has a ``session_workspace``
        event for this, but legacy callers don't emit it. Direct
        mutation here is the transition shim. Phase 5 wires the
        manager to emit ``session_workspace`` and this branch goes
        away."""
        sid = session.session_id
        state = self._store.state(sid)
        if state is None:
            try:
                state = self._run(self._store.open(
                    sid,
                    app_id=session.app_id,
                    user_id=session.user_id or _DEFAULT_USER,
                    create_if_missing=True, pin=True,
                ))
            except Exception as exc:
                logger.warning(
                    "legacy_adapter_put_open_failed sid=%s err=%s",
                    sid, exc,
                )
                return
        if state is None:
            return
        # Sync the path-shaped fields (not event-derived).
        state.workspace = str(getattr(session, "workspace", "") or "")
        state.workdir = str(getattr(session, "workdir", "") or "")
        # Title / turn_count have projections from events; only
        # propagate the legacy value if it's strictly more recent
        # (i.e. legacy bumped it without an event). The projections
        # already keep them in sync in the bridge case.
        legacy_title = str(getattr(session, "title", "") or "")
        if legacy_title and not state.title:
            state.title = legacy_title[:200]
        legacy_turns = int(getattr(session, "turn_count", 0) or 0)
        if legacy_turns > state.turn_count:
            state.turn_count = legacy_turns
        if getattr(session, "interrupted", False):
            state.interrupted = True
            if not state.interrupted_at:
                state.interrupted_at = datetime.utcfromtimestamp(
                    float(getattr(session, "interrupted_at", time.time())),
                ).isoformat() + "Z"

    def save_messages(
        self, app_id: str, session_id: str,
        messages: list[dict[str, Any]], user_id: str = _DEFAULT_USER,
    ) -> None:
        """No-op: messages are derived from events in the new world.
        Each user_message / assistant_message event already lands in
        ``state.messages`` via ``apply_projection``. Legacy callers
        that build a fresh messages list and call save_messages are
        duplicating work -- the right action here is to drop the
        call entirely (Phase 3.4 of file-by-file migration). For the
        adapter we just no-op so the daemon doesn't crash."""
        return None

    def save_turn_events(
        self, app_id: str, session_id: str, turn_index: int,
        events: list[Any], user_id: str = _DEFAULT_USER,
    ) -> None:
        """No-op: the turn concept was removed (everything is one
        seq-ordered event stream). Each event already has its own
        seq stamped by the SeqAllocator -- there is no per-turn
        grouping to persist."""
        return None

    def save_turn_messages(
        self, app_id: str, session_id: str, turn_index: int,
        messages: list[dict[str, Any]], user_id: str = _DEFAULT_USER,
    ) -> None:
        """No-op: per-turn delta-save was an optimisation for the
        legacy KV backend that stored cumulative blobs. In the new
        event-sourced world each message lands as a user_message /
        assistant_message event with its own seq -- no need for a
        separate "turn slice" persistence."""
        return None

    def __getattr__(self, name: str):
        """Defensive catch-all: any legacy method we forgot to shim
        gets a noisy no-op so the daemon doesn't crash. Logs once
        per name so the gap is visible without flooding the logs.

        ``__getattr__`` is only invoked when normal attribute lookup
        fails, so this never shadows real methods on this class."""
        if name.startswith("_") or name in _ADAPTER_KNOWN_PRIVATE:
            raise AttributeError(name)
        if name not in _ADAPTER_LOGGED_MISSING:
            _ADAPTER_LOGGED_MISSING.add(name)
            logger.warning(
                "legacy_adapter_missing_method name=%s -- using no-op stub. "
                "Add an explicit shim to LegacySessionStoreAdapter or "
                "migrate the call site to the native API.", name,
            )
        def _noop(*args, **kwargs):
            return None
        return _noop


    def touch(
        self, app_id: str, session_id: str, user_id: str = _DEFAULT_USER,
    ) -> None:
        """Mark the session active (LRU bump)."""
        state = self._store.state(session_id)
        if state is not None:
            state.touch()

    # ── Destructive ─────────────────────────────────────────────────

    def delete(
        self, app_id: str, session_id: str, user_id: str = _DEFAULT_USER,
    ) -> bool:
        """Delete a session entirely. Returns True if it existed."""
        try:
            return bool(self._run(
                self._store.delete(session_id, force=True),
            ))
        except Exception as exc:
            logger.warning(
                "legacy_adapter_delete_failed sid=%s err=%s",
                session_id, exc,
            )
            return False

    def delete_for_app(self, app_id: str) -> int:
        try:
            return int(self._run(self._store.delete_for_app(app_id)))
        except Exception as exc:
            logger.warning(
                "legacy_adapter_delete_for_app_failed app=%s err=%s",
                app_id, exc,
            )
            return 0

    # ── Listing ─────────────────────────────────────────────────────

    def list_for_app(self, app_id: str) -> list["ConversationSession"]:
        try:
            summaries = self._run(self._store.list_for_app(app_id))
        except Exception as exc:
            logger.warning(
                "legacy_adapter_list_for_app_failed app=%s err=%s",
                app_id, exc,
            )
            return []
        return [self._summary_to_conv_session(s) for s in summaries]

    def list_for_user(
        self, app_id: str | None = None, user_id: str = _DEFAULT_USER,
    ) -> list["ConversationSession"]:
        """Sessions owned by ``user_id``, optionally filtered by
        ``app_id``. Built on top of ``list_for_app`` + the per-state
        user_id check."""
        if app_id:
            apps = [app_id]
        else:
            apps = self._discover_app_ids()
        out: list["ConversationSession"] = []
        for a in apps:
            for cs in self.list_for_app(a):
                if cs.user_id == user_id:
                    out.append(cs)
        return out

    def count_for_user(self, app_id: str, user_id: str) -> int:
        return len(self.list_for_user(app_id=app_id, user_id=user_id))

    def _index_get(self, app_id: str) -> set[str]:
        """Internal hook some legacy callers use to count sessions for
        an app cheaply. Returns the set of session_ids."""
        try:
            sids = self._run(
                self._store.list_session_ids_for_app(app_id),
            )
        except Exception:
            return set()
        return set(sids)

    def _discover_app_ids(self) -> list[str]:
        """Walk meta.json files to enumerate apps. Used by
        ``list_for_user`` when no app filter is provided. O(n) but
        only triggered on the rare unfiltered listing path."""
        out: set[str] = set()
        if not self._store.root.exists():
            return []
        for meta_path in self._store.root.rglob("meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            aid = meta.get("app_id")
            if aid:
                out.add(str(aid))
        return sorted(out)

    # ── Recovery ────────────────────────────────────────────────────

    def recover_orphans(self) -> int:
        """Sync-shape wrapper. Legacy call site is in
        ``_BaseMixin.__init__`` which runs ON the FastAPI lifespan
        loop, so we cannot ``run_coroutine_threadsafe`` (would
        deadlock the same loop we're running on). Schedule a
        fire-and-forget task instead and return 0 -- the recovery
        completes shortly after the daemon's HTTP comes up."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running is self._loop:
            try:
                asyncio.create_task(self._store.recover_orphans())
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "legacy_adapter_recover_orphans_schedule_failed err=%s",
                    exc,
                )
            return 0
        try:
            return int(self._run(self._store.recover_orphans()))
        except Exception as exc:
            logger.warning(
                "legacy_adapter_recover_orphans_failed err=%s", exc,
            )
            return 0

    # ── State <-> ConversationSession converters ────────────────────

    def _state_to_conv_session(self, state) -> "ConversationSession":
        from digitorn.core.app.sessions import ConversationSession
        cs = ConversationSession(
            session_id=state.session_id,
            app_id=state.app_id,
            user_id=state.user_id or _DEFAULT_USER,
            messages=[
                {"role": m.role, "content": m.content,
                 **({"tool_calls": m.tool_calls} if getattr(m, "tool_calls", None) else {})}
                for m in state.messages
            ],
            created_at=_parse_iso_to_epoch(state.started_at),
            last_active=time.time(),
            title=state.title or "",
            memory_snapshot=dict(state.memory_facts),
            turn_count=int(state.turn_count or 0),
            workspace=state.workspace or "",
            workdir=state.workdir or "",
            interrupted=bool(state.interrupted),
            interrupted_at=_parse_iso_to_epoch(state.interrupted_at) if state.interrupted else 0.0,
        )
        return cs

    def _summary_to_conv_session(self, summary) -> "ConversationSession":
        """Build a ConversationSession from a SessionSummary. Messages
        list is empty (the summary doesn't carry them); callers that
        need messages should ``get(app, sid, uid)`` instead."""
        from digitorn.core.app.sessions import ConversationSession
        return ConversationSession(
            session_id=summary.session_id,
            app_id=summary.app_id,
            user_id=summary.user_id or _DEFAULT_USER,
            messages=[],
            created_at=_parse_iso_to_epoch(summary.started_at),
            last_active=time.time(),
            title=str(summary.title or ""),
            memory_snapshot={},
            turn_count=0,
            workspace="",
            workdir="",
            interrupted=False,
            interrupted_at=0.0,
        )

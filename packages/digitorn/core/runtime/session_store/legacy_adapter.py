"""LegacySessionStoreAdapter: sync facade over `InMemorySessionStore`"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, TYPE_CHECKING

from digitorn.core.runtime.session_store.store import InMemorySessionStore

if TYPE_CHECKING:
    from digitorn.core.app.sessions import ConversationSession

logger = logging.getLogger(__name__)

_DEFAULT_USER = "local"
_BRIDGE_CALL_TIMEOUT = 30.0

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
    """Parse an ISO timestamp to a unix epoch float"""
    if not s:
        return time.time()
    try:
        # tolerate trailing Z
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return time.time()


class _NoOpKVBackend:
    """Stub for callers that still poke `store._backend` (the legacy"""

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
    """Sync facade over `InMemorySessionStore` exposing the legacy"""

    def __init__(self, store: InMemorySessionStore) -> None:
        self._store = store
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._backend = _NoOpKVBackend()
        self._idle_ttl = 0.0
        self._absolute_ttl = 0.0


    def _run(self, coro, timeout: float = _BRIDGE_CALL_TIMEOUT):
        """Drive an async coroutine to completion from a sync caller."""
        if self._loop is None or not self._loop.is_running():
            return asyncio.run(coro)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            raise RuntimeError(
                "LegacySessionStoreAdapter sync method called directly "
                "from the event loop. Wrap with asyncio.to_thread, or "
                "migrate the call site to the native InMemorySessionStore "
                "async API."
            )
        return asyncio.run_coroutine_threadsafe(
            coro, self._loop,
        ).result(timeout=timeout)


    def session_lock(
        self, app_id: str, session_id: str, user_id: str = _DEFAULT_USER,
    ) -> asyncio.Lock:
        """Per-session asyncio.Lock. The new store keys lock by `sid`"""
        return self._store.session_lock(session_id)


    def get(
        self, app_id: str, session_id: str, user_id: str = _DEFAULT_USER,
    ) -> "ConversationSession | None":
        """Return a ConversationSession view of the session in the new"""
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
        if (
            user_id and user_id != _DEFAULT_USER
            and state.user_id and state.user_id != user_id
        ):
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
        """LLM-shaped messages list ([{"role": ..., "content": ...},"""
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
        if app_id and state.app_id and state.app_id != app_id:
            return []
        if (
            user_id and user_id != _DEFAULT_USER
            and state.user_id and state.user_id != user_id
        ):
            return []
        return self._messages_with_tool_results(state)


    def put(self, session: "ConversationSession") -> None:
        """Apply the chat-level fields from a ConversationSession onto"""
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
        """No-op: messages are derived from events in the new world."""
        return None

    def save_turn_events(
        self, app_id: str, session_id: str, turn_index: int,
        events: list[Any], user_id: str = _DEFAULT_USER,
    ) -> None:
        """No-op: the turn concept was removed (everything is one"""
        return None

    def save_turn_messages(
        self, app_id: str, session_id: str, turn_index: int,
        messages: list[dict[str, Any]], user_id: str = _DEFAULT_USER,
    ) -> None:
        """No-op: per-turn delta-save was an optimisation for the"""
        return None

    def __getattr__(self, name: str):
        """Defensive catch-all: any legacy method we forgot to shim"""
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


    def delete(
        self, app_id: str, session_id: str, user_id: str = _DEFAULT_USER,
    ) -> bool:
        """Delete a session entirely"""
        try:
            state = self._store.state(session_id)
            if state is None:
                # Load from disk to verify ownership before destructive op.
                try:
                    state = self._run(self._store.open(
                        session_id,
                        app_id=app_id or "",
                        user_id=user_id or _DEFAULT_USER,
                        create_if_missing=False, pin=False,
                    ))
                except (KeyError, RuntimeError):
                    return False
            if state is None:
                return False
            if app_id and state.app_id and state.app_id != app_id:
                return False
            if (
                user_id and user_id != _DEFAULT_USER
                and state.user_id and state.user_id != user_id
            ):
                logger.info(
                    "legacy_adapter_delete_refused_cross_user "
                    "sid=%s session_owner=%s caller=%s",
                    session_id, state.user_id, user_id,
                )
                return False
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


    def list_for_app(
        self, app_id: str,
        *, limit: int = 0, offset: int = 0,
    ) -> list["ConversationSession"]:
        """Legacy contract: `limit=0` means "no limit". `offset`"""
        try:
            summaries = self._run(self._store.list_for_app(app_id))
        except Exception as exc:
            logger.warning(
                "legacy_adapter_list_for_app_failed app=%s err=%s",
                app_id, exc,
            )
            return []
        if offset:
            summaries = summaries[int(offset):]
        if limit:
            summaries = summaries[: int(limit)]
        return [self._summary_to_conv_session(s) for s in summaries]

    def list_for_user(
        self, app_id: str | None = None, user_id: str = _DEFAULT_USER,
        *, limit: int = 0, offset: int = 0,
    ) -> list["ConversationSession"]:
        """Sessions owned by `user_id`, optionally filtered by"""
        if app_id:
            apps = [app_id]
        else:
            apps = self._discover_app_ids()
        out: list["ConversationSession"] = []
        for a in apps:
            for cs in self.list_for_app(a):
                if cs.user_id == user_id:
                    out.append(cs)
        if offset:
            out = out[int(offset):]
        if limit:
            out = out[: int(limit)]
        return out

    def count_for_user(self, app_id: str, user_id: str) -> int:
        return len(self.list_for_user(app_id=app_id, user_id=user_id))

    def _index_get(self, app_id: str) -> set[str]:
        """Internal hook some legacy callers use to count sessions for"""
        try:
            sids = self._run(
                self._store.list_session_ids_for_app(app_id),
            )
        except Exception:
            return set()
        return set(sids)

    def _discover_app_ids(self) -> list[str]:
        """Walk meta.json files to enumerate apps. Used by"""
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


    def recover_orphans(self) -> int:
        """Sync-shape wrapper. Legacy call site is in"""
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


    @staticmethod
    def _messages_with_tool_results(state) -> list[dict[str, Any]]:
        """Return the LLM-shaped messages list for `state`, with"""
        def _tool_result_content(r: Any) -> str:
            # output, fall back to the JSON envelope shape so resume
            # parity with live payloads is exact.
            if r is None:
                return ""
            output = getattr(r, "output", None)
            if output is not None:
                if isinstance(output, str):
                    return output
                try:
                    import json as _json
                    return _json.dumps(output, ensure_ascii=False, default=str)
                except Exception:
                    return str(output)
            success = bool(getattr(r, "success", True))
            error = getattr(r, "error", None) or ""
            if not success and error:
                return f'{{"success": false, "error": {error!r}}}'
            return '{"success": true}' if success else '{"success": false}'

        out: list[dict[str, Any]] = []
        for m in state.messages:
            row: dict[str, Any] = {"role": m.role, "content": m.content}
            tcs = getattr(m, "tool_calls", None) or []
            if tcs:
                fixed_tcs = []
                for tc in tcs:
                    if isinstance(tc, dict):
                        tc_copy = dict(tc)
                        if not tc_copy.get("type"):
                            tc_copy["type"] = "function"
                        fixed_tcs.append(tc_copy)
                    else:
                        fixed_tcs.append(tc)
                row["tool_calls"] = fixed_tcs
            out.append(row)
            if m.role == "assistant" and tcs:
                for tc in tcs:
                    tc_id = (
                        tc.get("id")
                        if isinstance(tc, dict) else getattr(tc, "id", "")
                    ) or ""
                    if not tc_id:
                        continue
                    tr = state.tool_results.get(tc_id)
                    if tr is None:
                        out.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": (
                                '{"success": false, "interrupted": true, '
                                '"error": "Session interrupted before this '
                                'tool completed."}'
                            ),
                        })
                        continue
                    out.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": _tool_result_content(tr),
                    })
        return out

    @staticmethod
    def _build_memory_snapshot(state) -> dict[str, Any]:
        """Reconstruct the nested memory snapshot that"""
        todos = []
        for t in state.todos:
            try:
                td = t.to_dict()
                if "text" in td and "content" not in td:
                    td = {**td, "content": td["text"]}
                todos.append(td)
            except Exception:
                # rather than break the whole resume.
                pass
        return {
            "working": {
                "goal": state.goal or "",
                "todos": todos,
            },
            "semantic": {
                "facts": list(state.semantic_facts),
            },
            "episodes": [],
        }

    def _state_to_conv_session(self, state) -> "ConversationSession":
        from digitorn.core.app.sessions import ConversationSession
        cs = ConversationSession(
            session_id=state.session_id,
            app_id=state.app_id,
            user_id=state.user_id or _DEFAULT_USER,
            messages=self._messages_with_tool_results(state),
            created_at=_parse_iso_to_epoch(state.started_at),
            last_active=time.time(),
            title=state.title or "",
            memory_snapshot=self._build_memory_snapshot(state),
            turn_count=int(state.turn_count or 0),
            workspace=state.workspace or "",
            workdir=state.workdir or "",
            interrupted=bool(state.interrupted),
            interrupted_at=_parse_iso_to_epoch(state.interrupted_at) if state.interrupted else 0.0,
        )
        return cs

    def _summary_to_conv_session(self, summary) -> "ConversationSession":
        """Build a ConversationSession from a SessionSummary. Messages"""
        from digitorn.core.app.sessions import ConversationSession
        live = self._store.state(summary.session_id)
        turn_count = int(live.turn_count) if live else 0
        interrupted = bool(live.interrupted) if live else False
        workspace = (live.workspace if live else "") or ""
        workdir = (live.workdir if live else "") or ""
        interrupted_at = (
            _parse_iso_to_epoch(live.interrupted_at)
            if live and live.interrupted_at else 0.0
        )
        return ConversationSession(
            session_id=summary.session_id,
            app_id=summary.app_id,
            user_id=summary.user_id or _DEFAULT_USER,
            messages=[],
            created_at=_parse_iso_to_epoch(summary.started_at),
            last_active=time.time(),
            title=str(summary.title or ""),
            memory_snapshot={},
            turn_count=turn_count,
            workspace=workspace,
            workdir=workdir,
            interrupted=interrupted,
            interrupted_at=interrupted_at,
        )

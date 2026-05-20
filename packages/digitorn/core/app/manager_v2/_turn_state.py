"""_TurnStateMixin - TurnState store + state envelope + watchdog."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ._models import TurnState

logger = logging.getLogger(__name__)


class _TurnStateMixin:
    """Per-session in-flight turn state + watchdog + state envelope."""

    _turn_state: dict[str, TurnState]
    _turn_heartbeat_tasks: dict[str, asyncio.Task]

    async def start_stale_turn_watchdog(
        self,
        interval: float = 30.0,
        staleness_threshold: float = 300.0,  # 5 minutes
    ) -> None:
        """Scan the TurnState store every `interval` seconds and mark"""
        if getattr(self, "_stale_turn_watchdog_task", None) is not None:
            logger.warning("stale_turn_watchdog already running")
            return

        async def _loop() -> None:
            logger.info(
                "stale_turn_watchdog_started interval=%ss threshold=%ss",
                interval, staleness_threshold,
            )
            while True:
                try:
                    await asyncio.sleep(interval)
                    now = time.time()
                    stale: list[tuple[str, str, TurnState]] = []
                    for key, state in list(self._turn_state.items()):
                        if state.interrupted:
                            continue
                        if now - state.last_activity_at > staleness_threshold:
                            parts = key.split(":", 1)
                            if len(parts) == 2:
                                stale.append((parts[0], parts[1], state))

                    for app_id, session_id, state in stale:
                        logger.warning(
                            "stale_turn_detected app=%s session=%s "
                            "corr=%s idle=%.1fs",
                            app_id, session_id,
                            state.correlation_id,
                            now - state.last_activity_at,
                        )
                        final = self.turn_state_end(
                            app_id, session_id, interrupted=True,
                        )
                        try:
                            from digitorn.core.events.envelope import (
                                SessionEvent, OpType, OpState,
                            )
                            await self.event_bus.emit(SessionEvent.build(
                                type="error",
                                app_id=app_id,
                                session_id=session_id,
                                user_id=(state.correlation_id and "local") or "local",
                                op_id=state.correlation_id,
                                op_type=OpType.TURN,
                                op_state=OpState.FAILED,
                                correlation_id=state.correlation_id,
                                payload={
                                    "error": "Turn timed out - no activity for >5 min",
                                    "code": "turn_stale",
                                    "correlation_id": state.correlation_id,
                                    "turn": final.to_dict() if final else None,
                                },
                            ))
                        except Exception as exc:
                            logger.debug("stale_turn emit failed: %s", exc)
                except asyncio.CancelledError:
                    logger.info("stale_turn_watchdog_stopped")
                    return
                except Exception as exc:
                    logger.warning("stale_turn_watchdog_tick_error: %s", exc)

        self._stale_turn_watchdog_task = asyncio.create_task(_loop())

    async def stop_stale_turn_watchdog(self) -> None:
        task = getattr(self, "_stale_turn_watchdog_task", None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        self._stale_turn_watchdog_task = None


    def _turn_key(self, app_id: str, session_id: str) -> str:
        return f"{app_id}:{session_id}"

    def turn_state_begin(
        self, app_id: str, session_id: str, correlation_id: str,
    ) -> TurnState:
        """Create the TurnState for a new turn"""
        now = time.time()
        state = TurnState(
            correlation_id=correlation_id,
            started_at=now,
            last_activity_at=now,
            phase="requesting",
        )
        self._turn_state[self._turn_key(app_id, session_id)] = state
        return state

    def turn_state_update(
        self,
        app_id: str,
        session_id: str,
        *,
        phase: str | None = None,
        tool_calls_delta: int = 0,
        tokens_out_delta: int = 0,
        tokens_in_delta: int = 0,
    ) -> TurnState | None:
        """Mutate the live TurnState. Silently no-ops when no turn is"""
        state = self._turn_state.get(self._turn_key(app_id, session_id))
        if state is None:
            return None
        state.last_activity_at = time.time()
        if phase is not None:
            state.phase = phase
        if tool_calls_delta:
            state.tool_calls_count += tool_calls_delta
        if tokens_out_delta:
            state.tokens_out += tokens_out_delta
        if tokens_in_delta:
            state.tokens_in += tokens_in_delta
        return state

    def turn_state_end(
        self, app_id: str, session_id: str, *, interrupted: bool = False,
    ) -> TurnState | None:
        """Remove the TurnState on terminal event."""
        key = self._turn_key(app_id, session_id)
        state = self._turn_state.pop(key, None)
        if state is None:
            return None
        if interrupted:
            state.interrupted = True
        # Cancel the heartbeat pulser if one is registered.
        hb = self._turn_heartbeat_tasks.pop(key, None)
        if hb is not None and not hb.done():
            hb.cancel()
        return state

    def turn_state_get(
        self, app_id: str, session_id: str,
    ) -> TurnState | None:
        """Return a live reference (NOT a copy) to the TurnState."""
        return self._turn_state.get(self._turn_key(app_id, session_id))

    def turn_state_snapshot(
        self, app_id: str, session_id: str,
    ) -> dict[str, Any] | None:
        state = self.turn_state_get(app_id, session_id)
        return state.to_dict() if state else None

    def _start_turn_heartbeat(
        self, app_id: str, session_id: str, user_id: str,
        correlation_id: str,
    ) -> None:
        """Spawn a background task emitting `turn:heartbeat` every 3s"""
        key = self._turn_key(app_id, session_id)
        old = self._turn_heartbeat_tasks.pop(key, None)
        if old is not None and not old.done():
            old.cancel()

        async def _pulse() -> None:
            from digitorn.core.events.envelope import (
                SessionEvent, OpType, OpState,
            )
            try:
                while True:
                    await asyncio.sleep(3.0)
                    state = self.turn_state_get(app_id, session_id)
                    if state is None:
                        return  # turn ended; nothing to report
                    try:
                        await self.event_bus.emit(SessionEvent.build(
                            type="turn:heartbeat",
                            app_id=app_id,
                            session_id=session_id,
                            user_id=user_id,
                            op_id=correlation_id,
                            op_type=OpType.TURN,
                            op_state=OpState.RUNNING,
                            correlation_id=correlation_id,
                            payload={"turn": state.to_dict()},
                        ))
                    except Exception as exc:
                        logger.debug(
                            "turn_heartbeat_emit_failed session=%s: %s",
                            session_id, exc,
                        )
            except asyncio.CancelledError:
                return

        task = asyncio.create_task(_pulse(), name=f"turn-heartbeat:{key}")
        self._turn_heartbeat_tasks[key] = task

    async def build_state_envelope(
        self, app_id: str, session_id: str, user_id: str = "local",
    ) -> dict[str, Any]:
        """Assemble the authoritative state envelope for a session."""
        current_seq = 0
        try:
            buffer = getattr(self.event_bus, "_buffer", None)
            if buffer is not None and hasattr(buffer, "_seq"):
                scope_key = f"session::{session_id}"
                current_seq = int(buffer._seq.get(scope_key, 0) or 0)
        except Exception:
            current_seq = 0

        # Queue snapshot - same payload shape as the SSE queue:snapshot
        # event, for client-side reuse of the existing reducer.
        queue_payload: dict[str, Any] = {
            "entries": [], "depth": 0,
            "is_active": False, "running_correlation_id": None,
        }
        try:
            from digitorn.core.app import message_queue as _mq
            entries = await _mq.list_for_session(session_id)
            running = next(
                (e for e in entries if e.status == "running"), None,
            )
            queue_payload = {
                "entries": [e.to_dict() for e in entries],
                "depth": len(entries),
                "is_active": running is not None,
                "running_correlation_id": (
                    running.correlation_id if running else None
                ),
            }
        except Exception as exc:
            logger.debug("state_envelope queue failed: %s", exc)

        compaction_info: dict[str, Any] = {
            "had_compaction": False, "last_at_seq": None,
        }
        try:
            from digitorn.core.runtime.session_store.bridge import (
                get_default_bridge,
            )
            bridge = get_default_bridge()
            if bridge is not None:
                try:
                    state = await bridge.store.open(
                        session_id, app_id=app_id, user_id=user_id,
                        create_if_missing=False, pin=False,
                    )
                    # Walk events backward to find the latest compaction.
                    for ev in reversed(state.events):
                        if ev.type == "compaction":
                            payload = ev.payload or {}
                            kept_range = payload.get("kept_range") or {}
                            compaction_info = {
                                "had_compaction": True,
                                "last_at_seq": int(ev.seq),
                                "kept_from_seq": int(
                                    kept_range.get("from_seq", 0)
                                    if isinstance(kept_range, dict) else 0
                                ),
                            }
                            break
                except KeyError:
                    pass
        except Exception as exc:
            logger.debug("state_envelope compaction failed: %s", exc)

        # Turn - live TurnState or None
        turn_payload = self.turn_state_snapshot(app_id, session_id)

        from datetime import datetime, timezone as _tz
        return {
            "schema_version": 1,
            "app_id": app_id,
            "session_id": session_id,
            "user_id": user_id,
            "seq": current_seq,
            "turn": turn_payload,
            "queue": queue_payload,
            "compaction": compaction_info,
            "server_time": datetime.now(_tz.utc).isoformat(),
        }

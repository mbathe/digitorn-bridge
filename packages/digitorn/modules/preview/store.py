"""Per-session state for the preview module - in-memory cache.

The disk file ``{workspace}/.digitorn/sessions/{sid}/state.json`` is the
SINGLE source of truth (see ``fs_backend``). This module keeps a hot
in-memory copy of that state for the active sessions so mutations
don't pay a disk roundtrip on the hot path - the debounced flush in
``preview.module`` writes the JSON every ~500 ms while events keep
streaming live to the client.

Three concrete types live here:

  * :class:`PreviewSessionState` - the per-session ``state`` dict +
    ``resources`` channel map. That's it. No event ring buffer, no
    seq counter (envelope.seq from SessionBus is the only ordering
    key clients need).
  * :class:`PreviewSessionStore` - process-wide cache of the active
    states keyed by session id. Synchronous - all callers run on the
    asyncio event loop, no thread pool.
  * Soft watermark warnings on resource size so a runaway agent
    doesn't silently bloat memory. We never evict (would lose user
    files) - we only log when a session crosses the threshold.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PreviewSessionState:
    """All preview data for a single session.

    The data model is generic: a key/value ``state`` map plus arbitrary
    ``resources`` partitioned into named channels. App shells decide
    what each channel holds - canvas nodes, source files, slides,
    spreadsheet cells, document blocks. The module never inspects
    payloads; it only stores, fans out, and replays them.
    """

    session_id: str
    user_id: str | None = None
    state: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    # Soft watermark. Crossing the warning level emits a one-shot log
    # line per session+channel so the operator notices a session
    # accumulating thousands of resources (typically a runaway code-
    # gen loop). We do NOT evict - dropping resources would silently
    # lose user-visible files / nodes / slides. The proper fix when
    # this fires is to enable ``workspace.sync_to_disk`` which moves
    # bulk content out of process memory.
    _RESOURCE_WARN_THRESHOLD: int = 2000

    def channel(self, name: str) -> dict[str, dict[str, Any]]:
        ch = self.resources.get(name)
        if ch is None:
            ch = {}
            self.resources[name] = ch
        return ch

    def _maybe_warn_resource_size(self, channel_name: str) -> None:
        ch = self.resources.get(channel_name) or {}
        if len(ch) < self._RESOURCE_WARN_THRESHOLD:
            return
        warn_key = f"{channel_name}:warned"
        if self.state.get(warn_key):
            return
        self.state[warn_key] = True
        logger.warning(
            "preview_resource_high_water session=%s channel=%s size=%d "
            "threshold=%d - consider workspace.sync_to_disk=true to "
            "keep memory bounded",
            self.session_id, channel_name, len(ch),
            self._RESOURCE_WARN_THRESHOLD,
        )

    def snapshot(self) -> dict[str, Any]:
        """Serialise the live state to the wire / disk format.

        The same shape is read back by ``restore_from_dict`` so a round-
        trip through ``state.json`` preserves every field. ``nodes`` and
        ``edges`` are convenience copies of the matching channels for
        clients that pre-date the generic ``resources`` API; new clients
        should read ``resources``.
        """
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "state": dict(self.state),
            "resources": {
                name: {rid: dict(payload) for rid, payload in items.items()}
                for name, items in self.resources.items()
            },
            "nodes": [
                dict(p) for p in self.resources.get("nodes", {}).values()
            ],
            "edges": [
                dict(p) for p in self.resources.get("edges", {}).values()
            ],
        }

    def clear(self) -> None:
        """Wipe state and resources for this session.

        Emits no event by itself - callers (preview.clear action) emit
        a ``preview:cleared`` event after this so clients see the wipe
        on the wire.
        """
        self.state.clear()
        self.resources.clear()

    def restore_from_dict(self, data: dict[str, Any]) -> None:
        """Hydrate from a ``state.json`` payload (matches ``snapshot``)."""
        self.state = dict(data.get("state") or {})
        self.resources = {
            ch: {rid: dict(payload) for rid, payload in items.items()}
            for ch, items in (data.get("resources") or {}).items()
        }
        if data.get("user_id"):
            self.user_id = data["user_id"]


class PreviewSessionStore:
    """Process-wide in-memory cache: ``session_id`` -> ``PreviewSessionState``.

    All access is synchronous. Hydration from disk is the caller's job
    (``preview.module`` reads ``state.json`` via ``fs_backend`` and
    seeds the cache via ``restore_from_dict``).
    """

    def __init__(self) -> None:
        self._sessions: dict[str, PreviewSessionState] = {}

    def get_or_create(self, session_id: str) -> PreviewSessionState:
        if session_id not in self._sessions:
            self._sessions[session_id] = PreviewSessionState(session_id=session_id)
        return self._sessions[session_id]

    def get(self, session_id: str) -> PreviewSessionState | None:
        return self._sessions.get(session_id)

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def session_ids(self) -> list[str]:
        return list(self._sessions.keys())

"""Per-session state for the preview module - in-memory cache."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

@dataclass
class PreviewSessionState:
    """All preview data for a single session."""

    session_id: str
    user_id: str | None = None
    state: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

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
        """Serialise the live state to the wire / disk format."""
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
        """Wipe state and resources for this session."""
        self.state.clear()
        self.resources.clear()

    def restore_from_dict(self, data: dict[str, Any]) -> None:
        """Hydrate from a `state.json` payload (matches `snapshot`)."""
        self.state = dict(data.get("state") or {})
        self.resources = {
            ch: {rid: dict(payload) for rid, payload in items.items()}
            for ch, items in (data.get("resources") or {}).items()
        }
        if data.get("user_id"):
            self.user_id = data["user_id"]

class PreviewSessionStore:
    """Process-wide in-memory cache: `session_id` -> `PreviewSessionState`."""

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

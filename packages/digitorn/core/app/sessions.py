"""ConversationSession dataclass."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

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
    workspace: str = ""
    workdir: str = ""
    # Interruption tracking - enables smart resume
    interrupted: bool = False  # True if session didn't end cleanly
    interrupted_at: float = 0.0
    # Classified error of the most recent turn. None after a clean turn.
    # SSE clients get the `error` event live; poll-based clients (dev CLI,
    # plain REST) read it from summary().
    last_error: dict[str, Any] | None = None
    forked_from: str = ""

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
        self.last_active = time.time()

    def summary(self) -> dict[str, Any]:
        """Rich summary suitable for list rendering."""
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
            "workspace": self.workdir or self.workspace,
            "workdir": self.workdir or self.workspace,
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
            "last_error": self.last_error,
            "forked_from": self.forked_from or None,
        }

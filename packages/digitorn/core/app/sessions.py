"""ConversationSession dataclass.

Data container for an active chat session. Used as the inter-module
data type across the daemon (manager_v2, apps_v2 routes, sub-agent
spawn, etc.). The persistent storage is owned by the new
``InMemorySessionStore`` (filesystem-first); this dataclass is the
in-memory facade callers receive.

The legacy KV-backed ``SessionStore`` was removed in the SessionStore
unification refactor. Nothing constructs it anymore. The bridge +
``LegacySessionStoreAdapter`` translate between ``SessionState`` (the
new internal type) and ``ConversationSession`` (this type) for legacy
callers.
"""

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
    # ``workspace`` is the daemon-private per-session dir under
    # ``~/.digitorn/workspaces/{app}/{sid}/``. ALWAYS auto-created.
    # Holds state.json, baselines, hidden ``__sdk__/``, etc. The agent
    # never points its tools here directly - it operates on ``workdir``.
    workspace: str = ""
    # ``workdir`` is the agent's working directory. When the user passes
    # a ``workdir`` at session create (``runtime.workdir_mode: required``
    # apps), the agent's tools (Read/Write/Edit/Bash, WsRead/WsWrite, ...)
    # operate inside it. When omitted, ``workdir`` defaults to
    # ``workspace`` so the agent and the daemon share one tree (legacy
    # behaviour, retained for backward compat with existing apps).
    workdir: str = ""
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
            # ``workspace`` here is the agent-facing path the frontend
            # renders (file tree, status bar, etc.). The daemon-private
            # ``self.workspace`` is intentionally NOT exposed - it
            # holds internal state (baselines, ``__sdk__/``) and would
            # confuse the UI if surfaced. Both keys carry the workdir
            # value so legacy clients reading ``workspace`` still work.
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
            "last_error": None,
        }

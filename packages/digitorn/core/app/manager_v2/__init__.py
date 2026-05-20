"""manager_v2 - mixin-based composition of :class:`AppManager`."""

from __future__ import annotations

from ._base import _BaseMixin
from ._chat import _ChatMixin
from ._deploy import _DeployMixin
from ._hydration import _HydrationMixin
from ._mcp import _McpMixin
from ._models import (
    DeployedApp,
    TurnState,
    _normalize_scope,
    _recover_interrupted_session,
    _resolve_tool_display,
    _scoped_slug,
)
from ._persistence import _PersistenceMixin
from ._queue import _QueueMixin
from ._session import _SessionMixin
from ._turn_state import _TurnStateMixin


class AppManager(
    _DeployMixin,
    _SessionMixin,
    _ChatMixin,
    _TurnStateMixin,
    _QueueMixin,
    _PersistenceMixin,
    _McpMixin,
    _HydrationMixin,
    _BaseMixin,
):
    """Central manager for the full app lifecycle in the daemon."""

    pass


__all__ = [
    "AppManager",
    "DeployedApp",
    "TurnState",
    "_normalize_scope",
    "_recover_interrupted_session",
    "_resolve_tool_display",
    "_scoped_slug",
]

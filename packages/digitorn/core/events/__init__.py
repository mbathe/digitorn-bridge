"""Digitorn — Event system.

Provides an async EventBus with topic-based routing, wildcard matching,
and multiple backends (log, Socket.IO, fanout).
"""

from digitorn.core.events.bus import EventBus, FanoutEventBus, LogEventBus, NullEventBus
from digitorn.core.events.models import UniversalEvent
from digitorn.core.events.router import EventRouter

__all__ = [
    "EventBus",
    "EventRouter",
    "FanoutEventBus",
    "LogEventBus",
    "NullEventBus",
    "UniversalEvent",
]

"""Digitorn - MQTT-style topic router."""

from __future__ import annotations

import re
from functools import lru_cache

@lru_cache(maxsize=1024)
def _compile_pattern(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    for segment in pattern.split("."):
        if segment == "#":
            parts.append(r"(?:\w+(?:\.\w+)*)?")
        elif segment == "*":
            parts.append(r"\w+")
        else:
            parts.append(re.escape(segment))
    return re.compile(r"^" + r"\.".join(parts) + r"$")

def topic_matches(pattern: str, topic: str) -> bool:
    """Check if *topic* matches the given wildcard *pattern*."""
    if pattern == "#":
        return True
    return _compile_pattern(pattern).match(topic) is not None

class EventRouter:
    """Route events to listeners based on MQTT-style topic patterns."""

    def __init__(self) -> None:
        self._routes: dict[str, list] = {}

    def add(self, pattern: str, handler: object) -> None:
        """Register a handler for a topic pattern."""
        self._routes.setdefault(pattern, []).append(handler)

    def remove(self, pattern: str, handler: object) -> None:
        """Remove a handler from a pattern."""
        handlers = self._routes.get(pattern, [])
        if handler in handlers:
            handlers.remove(handler)
            if not handlers:
                del self._routes[pattern]

    def match(self, topic: str) -> list:
        """Return all handlers matching the given topic."""
        matched = []
        for pattern, handlers in self._routes.items():
            if topic_matches(pattern, topic):
                matched.extend(handlers)
        return matched

    def clear(self) -> None:
        """Remove all routes."""
        self._routes.clear()

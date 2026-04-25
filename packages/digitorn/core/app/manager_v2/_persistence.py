"""_PersistenceMixin — placeholder for future history_log / persistence helpers.

The original ``manager.py`` has no top-level ``_persist_turn`` /
``_persist_turn_bg`` methods today (persistence is inlined into
``_chat_locked``). This mixin is kept empty so the composed class
matches the planned layout, and so future persistence helpers can
land here without rewiring ``__init__.py``.
"""

from __future__ import annotations


class _PersistenceMixin:
    """No-op placeholder — see module docstring."""

    pass

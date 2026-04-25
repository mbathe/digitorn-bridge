"""_HydrationMixin — placeholder for join_session hydration helpers.

The original ``manager.py`` does not currently host any
``_compute_*_snapshot`` / ``compute_active_ops`` methods directly;
those live in :mod:`digitorn.core.events.hydration` and are imported
where needed. This mixin is reserved for future helpers colocated
with the manager.
"""

from __future__ import annotations


class _HydrationMixin:
    """No-op placeholder — see module docstring."""

    pass

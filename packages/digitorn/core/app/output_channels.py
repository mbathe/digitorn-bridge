"""Backward-compatibility shim - re-exports from the new channels package.

The universal channel system now lives in ``digitorn.core.app.channels``.
This module re-exports the key classes so existing imports continue to work.

.. deprecated::
    Import from ``digitorn.core.app.channels`` instead.
"""

from digitorn.core.app.channels.llm import LLMNotificationChannel
from digitorn.core.app.channels.registry import ChannelRegistry

__all__ = ["ChannelRegistry", "LLMNotificationChannel"]

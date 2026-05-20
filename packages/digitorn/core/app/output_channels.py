"""Backward-compatibility shim - re-exports from the new channels package."""

from digitorn.core.app.channels.llm import LLMNotificationChannel
from digitorn.core.app.channels.registry import ChannelRegistry

__all__ = ["ChannelRegistry", "LLMNotificationChannel"]

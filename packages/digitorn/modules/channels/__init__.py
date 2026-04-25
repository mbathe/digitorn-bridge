"""Unified bidirectional channels module.

Receives events from any source (webhook, cron, email, file watch, RSS, queue)
and responds through the same or a different channel. Replaces the separate
trigger system (background.py) and output channels with a single YAML-configured
module.
"""

def __getattr__(name: str):
    if name == "ChannelsModule":
        from digitorn.modules.channels.module import ChannelsModule
        return ChannelsModule
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["ChannelsModule"]

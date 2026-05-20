"""Unified bidirectional channels module."""

def __getattr__(name: str):
    if name == "ChannelsModule":
        from digitorn.modules.channels.module import ChannelsModule
        return ChannelsModule
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["ChannelsModule"]

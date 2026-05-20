"""Universal Output Channel System - extensible notification delivery."""

from digitorn.core.app.channels.base import (
    BaseOutputChannel,
    ChannelCapabilities,
    ChannelHealth,
    ChannelMeta,
    ChannelPayload,
    DeliveryContext,
    DeliveryResult,
    PayloadAttachment,
    RetryPolicy,
)
from digitorn.core.app.channels.registry import ChannelRegistry

__all__ = [
    "BaseOutputChannel",
    "ChannelCapabilities",
    "ChannelHealth",
    "ChannelMeta",
    "ChannelPayload",
    "ChannelRegistry",
    "DeliveryContext",
    "DeliveryResult",
    "PayloadAttachment",
    "RetryPolicy",
]

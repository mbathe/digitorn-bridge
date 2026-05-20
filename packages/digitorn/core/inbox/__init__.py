"""Inbox subsystem - persistent cross-device notification store."""

from digitorn.core.inbox.dispatcher import (
    FCMBackend,
    NotificationBackend,
    NotificationDispatcher,
    SmtpBackend,
)
from digitorn.core.inbox.file_adapter import InboxStoreFileAdapter
from digitorn.core.inbox.kinds import InboxKind
from digitorn.core.inbox.policy import NotificationPolicy
from digitorn.core.inbox.producer import InboxProducer
from digitorn.core.inbox.store import InboxStore

__all__ = [
    "FCMBackend",
    "InboxKind",
    "InboxProducer",
    "InboxStore",
    "InboxStoreFileAdapter",
    "NotificationBackend",
    "NotificationDispatcher",
    "NotificationPolicy",
    "SmtpBackend",
]

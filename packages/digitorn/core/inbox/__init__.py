"""Inbox subsystem - persistent cross-device notification store.

Public surface::

    from digitorn.core.inbox import InboxStore, InboxProducer, InboxKind

The store is a CRUD layer over the ``inbox_items`` +
``inbox_devices`` + ``inbox_notification_prefs`` tables. The
producer is a background task that listens to the event bus's
per-user fan-out and creates inbox rows for events that merit
user attention (session completed, failed, approval requested,
credential missing, background activation finished, quota
warning).

The Flutter ``ActivityInboxService`` syncs from this store on
launch and listens to the global ``/api/users/me/events`` SSE for
live updates. See ``docs/FLUTTER_OMNIBUS_INTEGRATION.md`` §5 for
the full contract.
"""

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

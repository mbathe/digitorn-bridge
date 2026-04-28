"""Inbox kind enum - the canonical set of notification categories.

Kept in its own module so producers and stores can reference it
without pulling in SQLAlchemy or the FastAPI layer.
"""

from __future__ import annotations


class InboxKind:
    """Every kind the inbox can store.

    These strings are the contract between backend and Flutter -
    don't rename without updating ``ActivityInboxService`` on the
    client side.
    """

    SESSION_COMPLETED = "session.completed"
    SESSION_FAILED = "session.failed"
    SESSION_AWAITING_APPROVAL = "session.awaiting_approval"

    BG_ACTIVATION_COMPLETED = "bg.activation_completed"
    BG_ACTIVATION_FAILED = "bg.activation_failed"

    CREDENTIAL_EXPIRED = "credential.expired"
    CREDENTIAL_MISSING = "credential.missing"

    QUOTA_WARNING = "quota.warning"

    ALL = frozenset({
        SESSION_COMPLETED,
        SESSION_FAILED,
        SESSION_AWAITING_APPROVAL,
        BG_ACTIVATION_COMPLETED,
        BG_ACTIVATION_FAILED,
        CREDENTIAL_EXPIRED,
        CREDENTIAL_MISSING,
        QUOTA_WARNING,
    })

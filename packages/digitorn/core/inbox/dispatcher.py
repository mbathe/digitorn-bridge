"""NotificationDispatcher — delivery orchestrator for inbox events.

Architecture::

    InboxProducer.create_item(...)
          │
          ▼
    NotificationDispatcher.dispatch(user_id, item)
          │
          ├── NotificationPolicy → which channels to fire
          │
          ├── for each channel:
          │     └── backend.send(user_id, item)
          │
          └── Backends:
                  desktop → no-op (client SSE already delivers)
                  push    → FCMBackend (if configured)
                  email   → SmtpBackend (if configured)

The dispatcher is **always safe to call**: when a backend is not
configured or its dependencies (``firebase-admin``, SMTP creds)
are missing, the backend logs a debug line and returns without
raising. This keeps the inbox producer simple — it always calls
``dispatcher.dispatch(...)`` and never has to know about which
backends exist.

Configuration is via environment variables (no config.py section
needed for this round):

- ``DIGITORN_FCM_CREDENTIALS_PATH``   path to Firebase service account JSON
- ``DIGITORN_FCM_PROJECT_ID``         (optional, read from cred file if absent)
- ``DIGITORN_SMTP_HOST``              SMTP server
- ``DIGITORN_SMTP_PORT``              default 587
- ``DIGITORN_SMTP_USER``
- ``DIGITORN_SMTP_PASSWORD``
- ``DIGITORN_SMTP_FROM``              from address

If any of these is missing the corresponding backend is disabled
and logs ``notification_backend_disabled: fcm reason=no_creds`` at
startup so the admin can tell what's missing without grep'ing.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from digitorn.core.inbox.kinds import InboxKind
from digitorn.core.inbox.policy import NotificationPolicy
from digitorn.core.inbox.store import InboxStore

logger = logging.getLogger(__name__)


class NotificationBackend:
    """Abstract base for delivery backends."""

    name: str = "base"

    def is_configured(self) -> bool:
        return False

    async def send(
        self,
        *,
        user_id: str,
        item: dict[str, Any],
        context: dict[str, Any],
    ) -> bool:
        """Deliver one inbox item. Return True on success, False on
        silent no-op. Raise only for unexpected errors (caller logs
        + swallows). ``context`` carries per-user extras like
        device tokens or email addresses."""
        return False


# ────────────────────────────────────────────────────────────────────
# FCM backend — firebase-admin, lazy import
# ────────────────────────────────────────────────────────────────────


class FCMBackend(NotificationBackend):
    name = "fcm"

    def __init__(self) -> None:
        self._app: Any = None
        self._messaging: Any = None
        self._disabled_reason: str | None = None
        self._init()

    def _init(self) -> None:
        cred_path = os.environ.get("DIGITORN_FCM_CREDENTIALS_PATH", "").strip()
        if not cred_path:
            self._disabled_reason = "no_creds"
            return
        if not os.path.exists(cred_path):
            self._disabled_reason = f"creds_not_found:{cred_path}"
            return
        try:
            import firebase_admin  # type: ignore
            from firebase_admin import credentials, messaging  # type: ignore
        except ImportError:
            self._disabled_reason = "firebase_admin_not_installed"
            return

        try:
            cred = credentials.Certificate(cred_path)
            # Use a named app so re-init in tests doesn't raise.
            app_name = "digitorn-notifier"
            try:
                self._app = firebase_admin.get_app(app_name)
            except ValueError:
                self._app = firebase_admin.initialize_app(cred, name=app_name)
            self._messaging = messaging
            logger.info("FCMBackend initialized, cred=%s", cred_path)
        except Exception as exc:
            self._disabled_reason = f"init_failed:{exc}"
            logger.warning("FCMBackend init failed: %s", exc)

    def is_configured(self) -> bool:
        return self._messaging is not None

    async def send(
        self,
        *,
        user_id: str,
        item: dict[str, Any],
        context: dict[str, Any],
    ) -> bool:
        if not self.is_configured():
            return False
        devices: list[dict[str, Any]] = context.get("devices") or []
        if not devices:
            logger.debug("FCMBackend: no devices for user=%s, skipping", user_id)
            return False

        # Build one Message per device (FCM supports send_each for
        # batching but the simple one-at-a-time path is fine for v1).
        loop = asyncio.get_running_loop()
        sent = 0
        for dev in devices:
            token = dev.get("fcm_token")
            if not token:
                continue
            try:
                message = self._messaging.Message(
                    token=token,
                    notification=self._messaging.Notification(
                        title=item.get("title") or "Digitorn",
                        body=item.get("subtitle") or "",
                    ),
                    data=self._serialize_data(item),
                )
                # firebase-admin.send is sync — run in executor so we
                # don't block the event loop on the HTTP round-trip.
                await loop.run_in_executor(
                    None, self._messaging.send, message, self._app,
                )
                sent += 1
            except Exception as exc:
                # Per-device failure: log, continue. Flask tokens
                # that were revoked come back as FCM errors — the
                # device cleanup loop (not implemented here) should
                # prune them on 404/410.
                logger.warning(
                    "FCMBackend send failed user=%s device=%s: %s",
                    user_id, dev.get("id"), exc,
                )
        logger.debug(
            "FCMBackend delivered %d/%d devices for user=%s",
            sent, len(devices), user_id,
        )
        return sent > 0

    @staticmethod
    def _serialize_data(item: dict[str, Any]) -> dict[str, str]:
        """FCM ``data`` payload must be strings. Stringify the minimal
        set the client needs to route the notification tap."""
        out: dict[str, str] = {
            "kind": str(item.get("kind") or ""),
            "inbox_id": str(item.get("id") or ""),
        }
        if item.get("app_id"):
            out["app_id"] = str(item["app_id"])
        if item.get("session_id"):
            out["session_id"] = str(item["session_id"])
        if item.get("credential_provider"):
            out["credential_provider"] = str(item["credential_provider"])
        return out


# ────────────────────────────────────────────────────────────────────
# SMTP backend — stdlib smtplib, graceful degrade
# ────────────────────────────────────────────────────────────────────


class SmtpBackend(NotificationBackend):
    name = "smtp"

    def __init__(self) -> None:
        self._host = os.environ.get("DIGITORN_SMTP_HOST", "").strip()
        self._port = int(os.environ.get("DIGITORN_SMTP_PORT", "587") or 587)
        self._user = os.environ.get("DIGITORN_SMTP_USER", "").strip()
        self._password = os.environ.get("DIGITORN_SMTP_PASSWORD", "").strip()
        self._from = os.environ.get("DIGITORN_SMTP_FROM", "").strip()
        self._disabled_reason: str | None = None
        if not self._host:
            self._disabled_reason = "no_host"
        elif not self._from:
            self._disabled_reason = "no_from"

    def is_configured(self) -> bool:
        return self._disabled_reason is None

    async def send(
        self,
        *,
        user_id: str,
        item: dict[str, Any],
        context: dict[str, Any],
    ) -> bool:
        if not self.is_configured():
            return False
        email = (context.get("prefs") or {}).get("channels", {}).get("email")
        if not email:
            logger.debug("SmtpBackend: no email for user=%s, skipping", user_id)
            return False

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._sync_send, email, item)
            return True
        except Exception as exc:
            logger.warning(
                "SmtpBackend send failed user=%s to=%s: %s",
                user_id, email, exc,
            )
            return False

    def _sync_send(self, to_addr: str, item: dict[str, Any]) -> None:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["Subject"] = item.get("title") or "Digitorn notification"
        msg["From"] = self._from
        msg["To"] = to_addr
        body = item.get("subtitle") or ""
        if item.get("app_id"):
            body += f"\n\nApp: {item['app_id']}"
        if item.get("session_id"):
            body += f"\nSession: {item['session_id']}"
        msg.set_content(body or "(no body)")

        with smtplib.SMTP(self._host, self._port) as s:
            s.starttls()
            if self._user and self._password:
                s.login(self._user, self._password)
            s.send_message(msg)


# ────────────────────────────────────────────────────────────────────
# Dispatcher — glue between policy, backends, and the inbox store
# ────────────────────────────────────────────────────────────────────


class NotificationDispatcher:
    """One instance per daemon. Wired into InboxProducer.

    Call flow::

        await dispatcher.dispatch(user_id, item)
          → load prefs + devices for user
          → policy.channels_for(kind, prefs)
          → for channel in channels: backends[channel].send(...)

    Errors are swallowed — the producer cannot know about delivery
    failures, and a flaky backend should never break the inbox
    write path.
    """

    def __init__(
        self,
        *,
        store: InboxStore,
        fcm: NotificationBackend | None = None,
        smtp: NotificationBackend | None = None,
    ) -> None:
        self._store = store
        self._fcm = fcm or FCMBackend()
        self._smtp = smtp or SmtpBackend()
        # The "desktop" channel is handled by the SSE stream — the
        # dispatcher has no desktop backend. Included in the
        # summary logs so admins know it's covered.
        logger.info(
            "NotificationDispatcher: fcm=%s (reason=%s) smtp=%s (reason=%s)",
            self._fcm.is_configured(),
            getattr(self._fcm, "_disabled_reason", None),
            self._smtp.is_configured(),
            getattr(self._smtp, "_disabled_reason", None),
        )

    async def dispatch(
        self, user_id: str, item: dict[str, Any],
    ) -> dict[str, Any]:
        """Deliver one inbox item. Returns a diagnostic dict the
        caller can log (never raises).

        Shape::

            {
                "channels": ["push", "email"],
                "delivered": {"push": True, "email": False},
                "skipped_reason": None,
            }
        """
        kind = item.get("kind", "")
        try:
            prefs = await self._store.get_notification_prefs(user_id=user_id)
        except Exception as exc:
            logger.warning(
                "dispatcher: get_prefs failed for user=%s: %s", user_id, exc,
            )
            prefs = None

        channels = NotificationPolicy.channels_for(
            kind=kind, prefs=prefs,
        )
        if not channels:
            return {
                "channels": [],
                "delivered": {},
                "skipped_reason": "policy_filtered",
            }

        context = {"prefs": prefs or {}}
        delivered: dict[str, bool] = {}

        if "push" in channels:
            try:
                devices = await self._store.list_devices(user_id=user_id)
                context["devices"] = devices
                delivered["push"] = await self._fcm.send(
                    user_id=user_id, item=item, context=context,
                )
            except Exception as exc:
                logger.warning("push dispatch failed: %s", exc)
                delivered["push"] = False

        if "email" in channels:
            try:
                delivered["email"] = await self._smtp.send(
                    user_id=user_id, item=item, context=context,
                )
            except Exception as exc:
                logger.warning("email dispatch failed: %s", exc)
                delivered["email"] = False

        # "desktop" is a pass-through: the event is already on the
        # client's SSE stream, no server-side action needed. We
        # report it as "delivered" so the log shows the decision.
        if "desktop" in channels:
            delivered["desktop"] = True

        return {
            "channels": channels,
            "delivered": delivered,
            "skipped_reason": None,
        }

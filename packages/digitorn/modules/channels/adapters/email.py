"""Email adapter — IMAP inbound + SMTP outbound.

Inbound:  Polls IMAP for new messages (by UID tracking, not UNSEEN flag).
Outbound: Sends email via SMTP.

The adapter tracks message UIDs to detect new arrivals, regardless of
read/unread status. This works correctly with Gmail (which marks self-sent
emails as Seen) and all other IMAP providers.
"""

from __future__ import annotations

import asyncio
import email
import email.utils
import logging
import re
import smtplib
import time
from datetime import datetime, timezone

# IMAP SINCE accepts "DD-Mon-YYYY" (RFC3501). We also accept ISO "YYYY-MM-DD"
# and convert it. Anything else is rejected to prevent IMAP command injection.
_IMAP_DATE_RE = re.compile(
    r"^\d{1,2}-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{4}$"
)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _validate_imap_since_date(since_date: str) -> str:
    """Validate/normalize an IMAP SINCE date value.

    Accepts ``DD-Mon-YYYY`` directly or ``YYYY-MM-DD`` (converted). Raises
    ``ValueError`` on anything else to block IMAP command injection.

    Performs FULL date validation via datetime — rejects impossible dates
    like 32-Jan-2020 or 30-Feb-2024 that the regex alone would accept.
    """
    from datetime import date as _date

    if not isinstance(since_date, str):
        raise ValueError(f"Invalid IMAP SINCE date: {since_date!r}")
    if _IMAP_DATE_RE.match(since_date):
        # Verify the date is actually valid (regex allows day=32 or 99)
        try:
            d_str, m_str, y_str = since_date.split("-")
            month_idx = _MONTHS.index(m_str) + 1
            _date(int(y_str), month_idx, int(d_str))  # raises if invalid
        except (ValueError, IndexError) as exc:
            raise ValueError(
                f"Invalid IMAP SINCE date: {since_date!r} (impossible date)"
            ) from exc
        return since_date
    if _ISO_DATE_RE.match(since_date):
        try:
            y, m, d = since_date.split("-")
            # Full validation via datetime
            _date(int(y), int(m), int(d))
            month = _MONTHS[int(m) - 1]
            return f"{int(d):d}-{month}-{int(y):04d}"
        except (ValueError, IndexError) as exc:
            raise ValueError(f"Invalid IMAP SINCE date: {since_date!r}") from exc
    raise ValueError(
        f"Invalid IMAP SINCE date: {since_date!r}. "
        "Expected YYYY-MM-DD or DD-Mon-YYYY."
    )
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from digitorn.core.app.channels.base import ChannelPayload, DeliveryResult
from digitorn.modules.channels.adapter import (
    AdapterCapabilities,
    BaseChannelAdapter,
    InboundCallback,
    InboundEvent,
    make_event_id,
)

logger = logging.getLogger(__name__)


class EmailAdapter(BaseChannelAdapter):
    """Email adapter — IMAP polling + SMTP delivery."""

    CHANNEL_ID = "email"
    CHANNEL_NAME = "Email (IMAP/SMTP)"
    CHANNEL_VERSION = "1.0.0"
    CHANNEL_DESCRIPTION = "Receive emails via IMAP and send via SMTP."
    SUPPORTS_INBOUND = True
    SUPPORTS_OUTBOUND = True

    def __init__(self, channel_config: dict[str, Any] | None = None) -> None:
        super().__init__(channel_config=channel_config)
        cfg = channel_config or {}

        # IMAP config
        imap_cfg = cfg.get("imap", {})
        self._imap_host: str = imap_cfg.get("host", "")
        self._imap_port: int = imap_cfg.get("port", 993)
        self._imap_user: str = imap_cfg.get("user", "")
        self._imap_password: str = imap_cfg.get("password", "")
        self._imap_folder: str = imap_cfg.get("folder", "INBOX")
        self._poll_interval: float = cfg.get("poll_interval", 60.0)

        # SMTP config
        smtp_cfg = cfg.get("smtp", {})
        self._smtp_host: str = smtp_cfg.get("host", "")
        self._smtp_port: int = smtp_cfg.get("port", 587)
        self._smtp_user: str = smtp_cfg.get("user", "")
        self._smtp_password: str = smtp_cfg.get("password", "")
        self._from_address: str = cfg.get("from_address", self._smtp_user or self._imap_user)

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_inbound=bool(self._imap_host),
            supports_outbound=bool(self._smtp_host),
            supports_rich_text=True,
            supports_attachments=True,
            supports_threading=True,
            supported_formats=["text", "html"],
        )

    def adapter_capabilities(self) -> AdapterCapabilities:
        return self.capabilities()

    # ── Inbound (IMAP polling) ───────────────────────────────────

    async def start_listener(self, callback: InboundCallback) -> None:
        if not self._imap_host:
            logger.warning("email_adapter_no_imap — inbound disabled")
            return

        logger.info("email_adapter_started host=%s folder=%s", self._imap_host, self._imap_folder)

        # Record startup time for SINCE filter — only emails after this date
        start_date = datetime.now(tz=timezone.utc).strftime("%d-%b-%Y")
        seen_uids: set[str] = set()

        # Seed: record all UIDs currently in the mailbox so we only fire on NEW ones
        try:
            seed_uids = await asyncio.to_thread(self._get_uids_since, start_date)
            seen_uids.update(seed_uids)
            logger.info("email_adapter_seeded existing=%d since=%s", len(seed_uids), start_date)
        except Exception as exc:
            logger.warning("email_seed_error: %s", exc)

        try:
            while True:
                await asyncio.sleep(self._poll_interval)

                try:
                    new_messages = await asyncio.to_thread(
                        self._fetch_new_messages, seen_uids, start_date,
                    )
                    for msg_data in new_messages:
                        event = InboundEvent(
                            event_id=make_event_id(),
                            provider_id="",
                            adapter_type="email",
                            source=msg_data["from"],
                            message=msg_data["body"],
                            payload=msg_data,
                            metadata={
                                "message_id": msg_data.get("message_id", ""),
                                "uid": msg_data.get("uid", ""),
                            },
                            reply_context={
                                "_app_id": "",
                                "to": msg_data["from"],
                                "subject": f"Re: {msg_data.get('subject', '')}",
                                "in_reply_to": msg_data.get("message_id", ""),
                            },
                        )
                        try:
                            await callback(event)
                        except Exception as exc:
                            logger.error(
                                "email_callback_error from=%s error=%s",
                                msg_data.get("from"), exc, exc_info=True,
                            )
                except Exception as exc:
                    logger.warning("email_poll_error: %s", exc)

        except asyncio.CancelledError:
            logger.info("email_adapter_stopped")

    def _get_uids_since(self, since_date: str) -> set[str]:
        """Get all UIDs SINCE a date (runs in thread). Lightweight — no body fetch."""
        import imaplib

        safe_date = _validate_imap_since_date(since_date)
        uids: set[str] = set()
        try:
            with imaplib.IMAP4_SSL(self._imap_host, self._imap_port) as imap:
                imap.login(self._imap_user, self._imap_password)
                imap.select(self._imap_folder, readonly=True)
                _, data = imap.search(None, f"SINCE {safe_date}")
                if data and data[0]:
                    for uid in data[0].split():
                        uids.add(uid.decode() if isinstance(uid, bytes) else str(uid))
        except Exception as exc:
            logger.warning("imap_get_uids_error: %s", exc)
        return uids

    def _fetch_new_messages(
        self, seen_uids: set[str], since_date: str,
    ) -> list[dict[str, Any]]:
        """Fetch messages with UIDs not in seen_uids (runs in thread)."""
        import imaplib

        messages: list[dict[str, Any]] = []
        try:
            safe_date = _validate_imap_since_date(since_date)
        except ValueError as exc:
            logger.warning("imap_invalid_since_date: %s", exc)
            return messages
        try:
            with imaplib.IMAP4_SSL(self._imap_host, self._imap_port) as imap:
                imap.login(self._imap_user, self._imap_password)
                imap.select(self._imap_folder, readonly=True)

                # Search by date (not UNSEEN) — catches Gmail self-sent too
                _, data = imap.search(None, f"SINCE {safe_date}")
                if not data or not data[0]:
                    return messages

                for uid in data[0].split()[-50:]:  # Last 50 (most recent)
                    uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
                    if uid_str in seen_uids:
                        continue
                    seen_uids.add(uid_str)

                    _, msg_data = imap.fetch(uid, "(RFC822)")
                    if not msg_data or not msg_data[0]:
                        continue

                    raw = msg_data[0][1]
                    if isinstance(raw, bytes):
                        msg = email.message_from_bytes(raw)
                        body = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                if part.get_content_type() == "text/plain":
                                    charset = part.get_content_charset() or "utf-8"
                                    body = part.get_payload(decode=True).decode(charset, errors="replace")
                                    break
                        else:
                            charset = msg.get_content_charset() or "utf-8"
                            body = msg.get_payload(decode=True).decode(charset, errors="replace")

                        messages.append({
                            "uid": uid_str,
                            "from": email.utils.parseaddr(msg.get("From", ""))[1],
                            "to": email.utils.parseaddr(msg.get("To", ""))[1],
                            "subject": msg.get("Subject", ""),
                            "body": body[:MAX_BODY_LENGTH],
                            "date": msg.get("Date", ""),
                            "message_id": msg.get("Message-ID", ""),
                        })

        except Exception as exc:
            logger.warning("imap_fetch_error: %s", exc)

        return messages

    async def stop_listener(self) -> None:
        pass

    # ── Outbound (SMTP) ──────────────────────────────────────────

    async def deliver(
        self,
        app_id: str,
        payload: ChannelPayload,
        config: dict[str, Any],
    ) -> DeliveryResult:
        if not self._smtp_host:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error="SMTP not configured.",
            )

        to_addr = config.get("to", config.get("recipient", ""))
        if not to_addr:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error="No recipient specified.",
            )

        subject = config.get("subject", payload.title or "Digitorn Notification")

        try:
            result = await asyncio.to_thread(
                self._send_email,
                to_addr=to_addr,
                subject=subject,
                body=payload.message,
                html_body=payload.rich_message or None,
                in_reply_to=config.get("in_reply_to", ""),
            )
            return result
        except Exception as exc:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error=str(exc)[:200],
                retryable=True,
            )

    def _send_email(
        self,
        to_addr: str,
        subject: str,
        body: str,
        html_body: str | None = None,
        in_reply_to: str = "",
    ) -> DeliveryResult:
        """Send email via SMTP (runs in thread)."""
        msg = MIMEMultipart("alternative")
        msg["From"] = self._from_address
        msg["To"] = to_addr
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to

        msg.attach(MIMEText(body, "plain", "utf-8"))
        if html_body:
            msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP(self._smtp_host, self._smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            if self._smtp_user and self._smtp_password:
                server.login(self._smtp_user, self._smtp_password)
            server.sendmail(self._from_address, [to_addr], msg.as_string())

        return DeliveryResult(
            success=True,
            channel_id=self.CHANNEL_ID,
            delivery_id=msg.get("Message-ID", ""),
        )

    async def send_reply(
        self,
        reply_context: dict[str, Any],
        text: str,
        payload: ChannelPayload | None = None,
    ) -> DeliveryResult:
        effective_payload = payload or ChannelPayload(message=text)
        return await self.deliver(
            app_id=reply_context.get("_app_id", ""),
            payload=effective_payload,
            config=reply_context,
        )


# Max email body length to store (prevent huge emails from eating memory)
MAX_BODY_LENGTH = 50_000

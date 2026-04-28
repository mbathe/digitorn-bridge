"""Gmail Output Channel - send emails via Gmail SMTP.

Uses Gmail's SMTP server with an App Password (no OAuth complexity).
Supports HTML emails, plain text fallback, and attachments.

Prerequisites:
    1. Enable 2-Step Verification on your Google Account
    2. Generate an App Password: https://myaccount.google.com/apppasswords
    3. Use the app password as ``smtp_password`` in your YAML config

YAML example::

    channels:
      email_alerts:
        type: gmail
        config:
          from_address: "your.email@gmail.com"
          smtp_password: "{{env.GMAIL_APP_PASSWORD}}"
          from_name: "Digitorn Bot"
          timeout: 10

      email_personal:
        type: gmail
        config:
          from_address: "bot@gmail.com"
          smtp_password: "{{env.GMAIL_APP_PASSWORD}}"
        user_resolver:
          module: database
          action: fetch_results
          params:
            query: "SELECT email, full_name FROM users WHERE session_id = ':session_id'"
          mapping:
            to_address: email
            recipient_name: full_name

Per-delivery config (from ``output_config`` or ``user_resolver``)::

    to_address: "recipient@example.com"
    recipient_name: "Alice Dupont"
    cc: "cc1@example.com, cc2@example.com"
    bcc: "bcc@example.com"
    reply_to: "noreply@example.com"
"""

from __future__ import annotations

import asyncio
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from digitorn.core.app.channels.base import (
    BaseOutputChannel,
    ChannelCapabilities,
    ChannelPayload,
    DeliveryResult,
    RetryPolicy,
)

logger = logging.getLogger(__name__)

_GMAIL_SMTP_HOST = "smtp.gmail.com"
_GMAIL_SMTP_PORT = 587


class GmailChannel(BaseOutputChannel):
    """Send emails via Gmail SMTP with App Password authentication.

    Supports HTML and plain text emails. Uses ``payload.title`` as the
    email subject and ``payload.rich_message`` (or ``payload.message``)
    as the body.
    """

    CHANNEL_ID = "gmail"
    CHANNEL_NAME = "Gmail (SMTP)"
    CHANNEL_VERSION = "1.0.0"
    CHANNEL_DESCRIPTION = (
        "Send email notifications via Gmail SMTP. "
        "Requires a Gmail App Password (2FA must be enabled)."
    )

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_rich_text=True,
            supports_attachments=False,
            supports_threading=False,
            max_message_length=0,
            supported_formats=["text", "html"],
        )

    def config_schema(self) -> dict[str, Any]:
        return {
            "required": {
                "from_address": "Gmail address to send from",
                "smtp_password": "Gmail App Password (NOT your regular password)",
            },
            "optional": {
                "from_name": "Display name for the sender (e.g. 'Digitorn Bot')",
                "smtp_host": f"SMTP server (default: {_GMAIL_SMTP_HOST})",
                "smtp_port": f"SMTP port (default: {_GMAIL_SMTP_PORT})",
                "timeout": "SMTP timeout in seconds (default: 10)",
            },
        }

    def per_delivery_config_schema(self) -> dict[str, Any]:
        return {
            "required": {
                "to_address": "Recipient email address(es), comma-separated",
            },
            "optional": {
                "recipient_name": "Recipient display name",
                "cc": "CC recipients (comma-separated)",
                "bcc": "BCC recipients (comma-separated)",
                "reply_to": "Reply-To address",
            },
        }

    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            max_retries=2,
            backoff_base=3.0,
            backoff_max=30.0,
            backoff_multiplier=2.0,
        )

    async def validate_config(self) -> list[str]:
        errors = await super().validate_config()
        from_addr = self.channel_config.get("from_address", "")
        if from_addr and "@" not in from_addr:
            errors.append(f"Invalid from_address: '{from_addr}'")
        return errors

    async def deliver(
        self,
        app_id: str,
        payload: ChannelPayload,
        config: dict[str, Any],
    ) -> DeliveryResult:
        """Send an email via Gmail SMTP."""
        from_addr = self.channel_config.get("from_address", "")
        from_name = self.channel_config.get("from_name", "")
        password = self.channel_config.get("smtp_password", "")
        host = self.channel_config.get("smtp_host", _GMAIL_SMTP_HOST)
        port = self.channel_config.get("smtp_port", _GMAIL_SMTP_PORT)
        timeout = self.channel_config.get("timeout", 10)

        to_addr = config.get("to_address", "")
        if not to_addr:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error="No to_address in delivery config (resolver may have failed)",
            )

        if not from_addr or not password:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error="Missing from_address or smtp_password in channel config",
            )

        msg = MIMEMultipart("alternative")

        if from_name:
            msg["From"] = f"{from_name} <{from_addr}>"
        else:
            msg["From"] = from_addr

        recipient_name = config.get("recipient_name", "")
        if recipient_name:
            msg["To"] = f"{recipient_name} <{to_addr}>"
        else:
            msg["To"] = to_addr

        cc = config.get("cc", "")
        if cc:
            msg["Cc"] = cc
        reply_to = config.get("reply_to", "")
        if reply_to:
            msg["Reply-To"] = reply_to

        subject = payload.title or f"[{app_id}] Notification"
        msg["Subject"] = subject

        if payload.priority == "high" or payload.priority == "critical":
            msg["X-Priority"] = "1"
        elif payload.priority == "low":
            msg["X-Priority"] = "5"

        text_body = self.format_text(payload)
        msg.attach(MIMEText(text_body, "plain", "utf-8"))

        html_body = self._build_html(payload)
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        all_recipients = [addr.strip() for addr in to_addr.split(",")]
        if cc:
            all_recipients.extend(addr.strip() for addr in cc.split(","))
        bcc = config.get("bcc", "")
        if bcc:
            all_recipients.extend(addr.strip() for addr in bcc.split(","))

        try:
            message_id = await asyncio.to_thread(
                self._smtp_send,
                host, port, from_addr, password, all_recipients,
                msg.as_string(), timeout,
            )
            logger.info(
                "gmail_sent app=%s to=%s subject=%s",
                app_id, to_addr, subject,
            )
            return DeliveryResult(
                success=True,
                channel_id=self.CHANNEL_ID,
                delivery_id=message_id,
                metadata={"to": to_addr, "subject": subject},
            )
        except Exception as exc:
            error_msg = str(exc)
            retryable = any(kw in error_msg.lower() for kw in [
                "timeout", "connection", "temporary", "try again",
                "too many", "service unavailable",
            ])
            logger.warning(
                "gmail_send_failed app=%s to=%s error=%s",
                app_id, to_addr, error_msg,
            )
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error=error_msg,
                retryable=retryable,
            )

    @staticmethod
    def _smtp_send(
        host: str,
        port: int,
        from_addr: str,
        password: str,
        recipients: list[str],
        message: str,
        timeout: int,
    ) -> str:
        """Blocking SMTP send (runs in thread)."""
        import smtplib

        with smtplib.SMTP(host, port, timeout=timeout) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(from_addr, password)
            server.sendmail(from_addr, recipients, message)
            return f"gmail-{id(message)}"

    def _build_html(self, payload: ChannelPayload) -> str:
        """Build an HTML email body from the payload."""
        if payload.rich_message:
            return payload.rich_message

        parts = []
        parts.append("<div style='font-family: -apple-system, sans-serif; max-width: 600px;'>")

        if payload.title:
            parts.append(f"<h2 style='color: #333;'>{payload.title}</h2>")

        parts.append(f"<p>{payload.message}</p>")

        if payload.structured_data:
            parts.append("<hr style='border: none; border-top: 1px solid #eee;'>")
            parts.append("<pre style='background: #f5f5f5; padding: 12px; border-radius: 4px; font-size: 13px;'>")
            import json
            parts.append(json.dumps(payload.structured_data, indent=2, ensure_ascii=False))
            parts.append("</pre>")

        if payload.tags:
            tags_html = " ".join(
                f"<span style='background: #e0e0e0; padding: 2px 8px; border-radius: 12px; font-size: 12px;'>{t}</span>"
                for t in payload.tags
            )
            parts.append(f"<p>{tags_html}</p>")

        parts.append("<p style='color: #999; font-size: 12px;'>- Digitorn Notification</p>")
        parts.append("</div>")
        return "\n".join(parts)

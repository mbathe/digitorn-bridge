"""Webhook adapter - bidirectional HTTP."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from digitorn.core.app.channels.base import (
    ChannelCapabilities,
    ChannelPayload,
    DeliveryResult,
)
from digitorn.modules.channels.adapter import (
    AdapterCapabilities,
    BaseChannelAdapter,
    InboundCallback,
    InboundEvent,
    make_event_id,
)
from digitorn.modules.channels.security import (
    check_content_type,
    check_payload_size,
    sanitize_payload,
    verify_api_key,
    verify_hmac_signature,
)

logger = logging.getLogger(__name__)

class WebhookAdapter(BaseChannelAdapter):
    """Bidirectional HTTP webhook adapter."""

    CHANNEL_ID = "webhook"
    CHANNEL_NAME = "HTTP Webhook"
    CHANNEL_VERSION = "1.0.0"
    CHANNEL_DESCRIPTION = "Receive and send HTTP webhooks."
    SUPPORTS_INBOUND = True
    SUPPORTS_OUTBOUND = True

    def __init__(self, channel_config: dict[str, Any] | None = None) -> None:
        super().__init__(channel_config=channel_config)
        cfg = channel_config or {}

        # Inbound config
        self._inbound_path: str = cfg.get("inbound_path", "")
        self._auth_mode: str = cfg.get("auth", "none")  # none, signature, api_key
        self._signature_secret: str = cfg.get("signature_secret", "")
        self._signature_header: str = cfg.get("signature_header", "X-Signature-256")
        self._api_key: str = cfg.get("api_key", "")
        self._max_payload: int = cfg.get("max_payload_bytes", 1_048_576)

        # Outbound config
        self._outbound_url: str = cfg.get("url", "")
        self._outbound_headers: dict[str, str] = cfg.get("headers", {})
        self._outbound_timeout: float = cfg.get("timeout", 10.0)

        # Runtime
        self._callback: InboundCallback | None = None

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_rich_text=True,
            supports_attachments=False,
            supports_threading=False,
            supported_formats=["text", "json"],
        )

    def adapter_capabilities(self) -> AdapterCapabilities:
        base = self.capabilities()
        return AdapterCapabilities(
            supports_inbound=bool(self._inbound_path),
            supports_outbound=bool(self._outbound_url),
            supports_rich_text=base.supports_rich_text,
            supported_formats=base.supported_formats,
        )

    async def start_listener(self, callback: InboundCallback) -> None:
        self._callback = callback
        if self._inbound_path:
            logger.info(
                "webhook_adapter_listener_ready path=%s auth=%s",
                self._inbound_path, self._auth_mode,
            )

    async def stop_listener(self) -> None:
        self._callback = None

    async def validate_inbound(
        self,
        raw_body: bytes,
        headers: dict[str, str],
    ) -> tuple[bool, str]:
        """Validate inbound webhook request."""
        # 1. Size check
        ok, err = check_payload_size(raw_body, self._max_payload)
        if not ok:
            return False, err

        # 2. Content-Type check
        ct = headers.get("content-type", "")
        ok, err = check_content_type(ct)
        if not ok:
            return False, err

        # 3. Auth check
        if self._auth_mode == "signature":
            sig = headers.get(self._signature_header.lower(), "")
            if not verify_hmac_signature(raw_body, sig, self._signature_secret):
                return False, "Invalid webhook signature."

        elif self._auth_mode == "api_key":
            key = headers.get("x-api-key", "")
            if not verify_api_key(key, self._api_key):
                return False, "Invalid API key."

        return True, ""

    def max_inbound_payload_bytes(self) -> int:
        return self._max_payload

    async def handle_webhook_request(
        self,
        raw_body: bytes,
        headers: dict[str, str],
        source_ip: str = "",
    ) -> tuple[bool, str]:
        """Handle an HTTP request received by the FastAPI route."""
        if not self._callback:
            return False, "Listener not active."

        # Validate
        valid, error = await self.validate_inbound(raw_body, headers)
        if not valid:
            logger.warning(
                "webhook_rejected path=%s source=%s error=%s",
                self._inbound_path, source_ip, error,
            )
            return False, error

        # Parse payload
        try:
            payload = json.loads(raw_body) if raw_body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False, "Invalid JSON payload."

        # Sanitize
        payload = sanitize_payload(payload)

        # Build event
        event = InboundEvent(
            event_id=make_event_id(),
            provider_id="",  # Set by module
            adapter_type="webhook",
            source=source_ip,
            message="",
            payload=payload,
            metadata={
                "headers": {
                    k: v for k, v in headers.items()
                    if k.lower() not in (
                        "authorization", "x-api-key", "cookie",
                        "x-signature-256", "x-hub-signature-256",
                    )
                },
                "content_type": headers.get("content-type", ""),
                "method": "POST",
            },
            reply_context={
                "_app_id": "",  # Set by module
                "_source_ip": source_ip,
            },
        )

        await self._callback(event)
        return True, ""

    async def deliver(
        self,
        app_id: str,
        payload: ChannelPayload,
        config: dict[str, Any],
    ) -> DeliveryResult:
        """Send HTTP POST to the configured outbound URL."""
        url = config.get("url", self._outbound_url)
        if not url:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error="No outbound URL configured.",
            )

        # SSRF protection
        try:
            from digitorn.modules.http.security import validate_url
            error, _sanitized, _validated = validate_url(url)
            if error:
                return DeliveryResult(
                    success=False,
                    channel_id=self.CHANNEL_ID,
                    error=f"URL validation failed: {error}",
                )
        except ImportError:
            pass  # http module not available - skip SSRF check

        headers = dict(self._outbound_headers)
        headers.setdefault("Content-Type", "application/json")

        body = {
            "message": payload.message,
            "title": payload.title,
            "priority": payload.priority,
            "structured_data": payload.structured_data,
            "metadata": payload.metadata,
            "tags": payload.tags,
        }

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self._outbound_timeout),
                ) as resp:
                    success = 200 <= resp.status < 300
                    retryable = resp.status in (429, 500, 502, 503, 504)
                    return DeliveryResult(
                        success=success,
                        channel_id=self.CHANNEL_ID,
                        delivery_id=resp.headers.get("X-Request-ID", ""),
                        error=None if success else f"HTTP {resp.status}",
                        retryable=retryable,
                        metadata={"status": resp.status},
                    )
        except ImportError:
            # Fallback to urllib (off-loop: `urlopen` blocks on
            # connect + body transfer, would stall the loop / Socket.IO).
            import asyncio as _asyncio
            import urllib.request
            import urllib.error

            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            timeout = self._outbound_timeout
            channel_id = self.CHANNEL_ID

            def _send() -> DeliveryResult:
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        return DeliveryResult(
                            success=True,
                            channel_id=channel_id,
                            metadata={"status": resp.status},
                        )
                except urllib.error.HTTPError as exc:
                    return DeliveryResult(
                        success=False,
                        channel_id=channel_id,
                        error=f"HTTP {exc.code}",
                        retryable=exc.code in (429, 500, 502, 503, 504),
                    )
                except Exception as exc:
                    return DeliveryResult(
                        success=False,
                        channel_id=channel_id,
                        error=str(exc)[:200],
                        retryable=True,
                    )

            return await _asyncio.to_thread(_send)
        except Exception as exc:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error=str(exc)[:200],
                retryable=True,
            )

    async def send_reply(
        self,
        reply_context: dict[str, Any],
        text: str,
        payload: ChannelPayload | None = None,
    ) -> DeliveryResult:
        """Reply delegates to outbound deliver."""
        effective_payload = payload or ChannelPayload(message=text)
        return await self.deliver(
            app_id=reply_context.get("_app_id", ""),
            payload=effective_payload,
            config=reply_context,
        )

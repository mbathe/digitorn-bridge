"""Webhook Output Channel - generic HTTP POST delivery.

The most universal external channel: any system with an HTTP endpoint
can receive notifications. Works with:
- Slack Incoming Webhooks
- Discord Webhooks
- Microsoft Teams Connectors
- Zapier/Make/n8n Webhooks
- Custom backend APIs
- Any REST endpoint

YAML example::

    channels:
      my_webhook:
        type: webhook
        config:
          url: "https://hooks.slack.com/services/T.../B.../..."
          method: POST
          headers:
            Content-Type: "application/json"
          timeout: 10
          payload_template: |
            {"text": "{{message}}", "channel": "#alerts"}

Per-delivery config overrides::

    output_channel: "my_webhook"
    output_config:
      url: "https://other-endpoint.com/api/notify"
      headers: {"Authorization": "Bearer {{token}}"}
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from digitorn.core.app.channels.base import (
    BaseOutputChannel,
    ChannelCapabilities,
    ChannelPayload,
    DeliveryResult,
    RetryPolicy,
)

logger = logging.getLogger(__name__)


class WebhookChannel(BaseOutputChannel):
    """Generic HTTP webhook delivery channel.

    Sends a JSON POST (or configurable method) to any URL.
    Supports custom headers, timeout, and payload templates.

    Uses a singleton aiohttp.ClientSession per channel instance to avoid
    file descriptor exhaustion under high concurrency.
    """

    CHANNEL_ID = "webhook"
    CHANNEL_NAME = "Webhook (HTTP)"
    CHANNEL_VERSION = "1.0.0"
    CHANNEL_DESCRIPTION = (
        "Send notifications via HTTP POST to any webhook URL. "
        "Compatible with Slack, Discord, Teams, Zapier, n8n, and any REST API."
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._http_session: Any = None  # Lazy-initialized aiohttp.ClientSession

    async def _get_http_session(self) -> Any:
        """Get or create the singleton aiohttp ClientSession.

        Lazy-initialized on first use. Reused across all deliveries
        to avoid socket exhaustion under load.
        """
        if self._http_session is None or getattr(self._http_session, "closed", True):
            try:
                import aiohttp
                self._http_session = aiohttp.ClientSession()
            except ImportError:
                return None
        return self._http_session

    async def on_stop(self) -> None:
        """Close the singleton HTTP session on channel shutdown."""
        if self._http_session is not None and not getattr(self._http_session, "closed", True):
            try:
                await self._http_session.close()
            except Exception as exc:
                logger.debug("webhook_session_close_failed: %s", exc)
            self._http_session = None

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_rich_text=True,
            supports_attachments=False,
            supports_threading=False,
            max_message_length=0,
            supported_formats=["text", "json", "html", "markdown"],
        )

    def config_schema(self) -> dict[str, Any]:
        return {
            "required": {
                "url": "Target webhook URL (HTTPS recommended)",
            },
            "optional": {
                "method": "HTTP method: POST (default), PUT, PATCH",
                "headers": "dict of HTTP headers (e.g. Authorization, Content-Type)",
                "timeout": "Request timeout in seconds (default: 10)",
                "payload_template": "Custom JSON payload template with {{message}}, {{title}}, {{data}} placeholders",
                "verify_ssl": "Verify SSL certificates (default: true)",
            },
        }

    def per_delivery_config_schema(self) -> dict[str, Any]:
        return {
            "optional": {
                "url": "Override target URL for this delivery",
                "headers": "Additional headers (merged with global)",
                "payload": "Custom payload dict (replaces template)",
            },
        }

    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            max_retries=3,
            backoff_base=2.0,
            backoff_max=30.0,
            backoff_multiplier=2.0,
        )

    async def validate_config(self) -> list[str]:
        """Validate webhook config - check URL format."""
        errors = await super().validate_config()
        url = self.channel_config.get("url", "")
        if url and not url.startswith(("http://", "https://")):
            errors.append(f"Invalid URL: '{url}' (must start with http:// or https://)")
        method = self.channel_config.get("method", "POST").upper()
        if method not in ("POST", "PUT", "PATCH"):
            errors.append(f"Invalid method: '{method}' (must be POST, PUT, or PATCH)")
        return errors

    async def deliver(
        self,
        app_id: str,
        payload: ChannelPayload,
        config: dict[str, Any],
    ) -> DeliveryResult:
        """Send HTTP request to the webhook URL."""
        url = config.get("url", self.channel_config.get("url", ""))
        if not url:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error="No webhook URL configured",
            )

        method = self.channel_config.get("method", "POST").upper()
        timeout = self.channel_config.get("timeout", 10)
        verify_ssl = self.channel_config.get("verify_ssl", True)

        headers = dict(self.channel_config.get("headers", {}))
        headers.update(config.get("headers", {}))
        headers.setdefault("Content-Type", "application/json")

        body = self._build_body(payload, config)

        # Enforce max payload size to prevent DoS via huge payloads
        max_size = int(self.channel_config.get("max_payload_size", 1_048_576))  # 1MB default
        body_size = 0
        if isinstance(body, dict):
            body_size = len(json.dumps(body))
        elif isinstance(body, str):
            body_size = len(body.encode("utf-8"))
        if body_size > max_size:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error=f"Payload size {body_size} exceeds max_payload_size {max_size}",
                retryable=False,
            )

        try:
            import aiohttp

            start = time.monotonic()
            session = await self._get_http_session()
            if session is None:
                return await self._deliver_urllib(url, method, headers, body, timeout)
            async with session.request(
                method,
                url,
                json=body if isinstance(body, dict) else None,
                data=body if isinstance(body, str) else None,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                ssl=verify_ssl if verify_ssl else False,
            ) as resp:
                elapsed_ms = (time.monotonic() - start) * 1000
                resp_text = await resp.text()

                if 200 <= resp.status < 300:
                    return DeliveryResult(
                        success=True,
                        channel_id=self.CHANNEL_ID,
                        delivery_id=resp.headers.get("X-Request-Id", ""),
                        metadata={
                            "status": resp.status,
                            "elapsed_ms": round(elapsed_ms, 1),
                        },
                    )

                retryable = resp.status == 429 or resp.status >= 500
                return DeliveryResult(
                    success=False,
                    channel_id=self.CHANNEL_ID,
                    error=f"HTTP {resp.status}: {resp_text[:200]}",
                    retryable=retryable,
                    metadata={"status": resp.status},
                    )

        except ImportError:
            return await self._deliver_urllib(url, method, headers, body, timeout)
        except Exception as exc:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error=str(exc),
                retryable=True,
            )

    def _build_body(
        self, payload: ChannelPayload, config: dict[str, Any]
    ) -> dict[str, Any] | str:
        """Build the HTTP request body from payload + config."""
        if "payload" in config:
            return config["payload"]

        template = self.channel_config.get("payload_template")
        if template:
            return (
                template
                .replace("{{message}}", payload.message)
                .replace("{{title}}", payload.title)
                .replace("{{data}}", json.dumps(payload.structured_data))
                .replace("{{priority}}", payload.priority)
            )

        body: dict[str, Any] = {
            "message": payload.message,
            "title": payload.title,
            "priority": payload.priority,
            "timestamp": payload.metadata.get("timestamp", ""),
            "tags": payload.tags,
        }
        if payload.structured_data:
            body["data"] = payload.structured_data
        if payload.metadata:
            body["metadata"] = payload.metadata
        return body

    async def _deliver_urllib(
        self,
        url: str,
        method: str,
        headers: dict[str, str],
        body: dict[str, Any] | str,
        timeout: int,
    ) -> DeliveryResult:
        """Fallback delivery using urllib (no aiohttp dependency)."""
        import asyncio
        import urllib.request

        def _sync_request() -> DeliveryResult:
            data = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
            req = urllib.request.Request(url, data=data, method=method)
            for k, v in headers.items():
                req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    status = resp.status
                    if 200 <= status < 300:
                        return DeliveryResult(
                            success=True,
                            channel_id=self.CHANNEL_ID,
                            metadata={"status": status},
                        )
                    return DeliveryResult(
                        success=False,
                        channel_id=self.CHANNEL_ID,
                        error=f"HTTP {status}",
                        retryable=status >= 500,
                        metadata={"status": status},
                    )
            except Exception as exc:
                return DeliveryResult(
                    success=False,
                    channel_id=self.CHANNEL_ID,
                    error=str(exc),
                    retryable=True,
                )

        return await asyncio.to_thread(_sync_request)

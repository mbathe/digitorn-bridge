"""Universal Output Channel System — extensible notification delivery.

Channels are the delivery infrastructure for scheduled jobs, watchers,
and any background task that needs to notify external systems.

**Channels are NOT modules.** Modules expose tools to the LLM agent.
Channels deliver results to external systems (Slack, email, Kafka, SMS,
Telegram, webhooks, phone calls, etc.).

Architecture:

    BaseOutputChannel (ABC)        ChannelPayload (structured input)
           |                              |
    +------+------+                 DeliveryResult (structured output)
    |      |      |
   LLM  Webhook  Log   ... Slack, Gmail, Kafka, Telegram, SMS, ...
           |
    ChannelRegistry (type → instance routing, retry, discovery)

Quick start for channel authors::

    from digitorn.core.app.channels import BaseOutputChannel, ChannelPayload, DeliveryResult

    class TelegramChannel(BaseOutputChannel):
        CHANNEL_ID = "telegram"
        CHANNEL_NAME = "Telegram"
        CHANNEL_VERSION = "1.0.0"

        async def deliver(self, app_id, payload, config):
            bot_token = self.channel_config["bot_token"]
            chat_id = config.get("chat_id", self.channel_config["default_chat_id"])
            text = self.format_text(payload)
            return DeliveryResult(success=True, channel_id=self.CHANNEL_ID)

YAML configuration::

    channels:
      my_telegram:
        type: telegram
        config:
          bot_token: "{{secret.TELEGRAM_BOT_TOKEN}}"
          default_chat_id: "123456789"

"""

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

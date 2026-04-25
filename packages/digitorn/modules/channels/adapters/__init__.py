"""Channel adapter registry.

Maps adapter type names (used in YAML ``adapter:`` field) to concrete classes.
New adapters register here or via entry points.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from digitorn.modules.channels.adapter import BaseChannelAdapter

logger = logging.getLogger(__name__)

# Lazy-loaded adapter registry: type name → import path
_BUILTIN_ADAPTERS: dict[str, str] = {
    "webhook": "digitorn.modules.channels.adapters.webhook.WebhookAdapter",
    "cron": "digitorn.modules.channels.adapters.cron.CronAdapter",
    "file_watcher": "digitorn.modules.channels.adapters.file_watcher.FileWatcherAdapter",
    "email": "digitorn.modules.channels.adapters.email.EmailAdapter",
    "rss": "digitorn.modules.channels.adapters.rss.RssAdapter",
    "log": "digitorn.modules.channels.adapters.log.LogAdapter",
    "queue": "digitorn.modules.channels.adapters.queue_adapter.QueueAdapter",
    "telegram": "digitorn.modules.channels.adapters.telegram.TelegramAdapter",
    "discord": "digitorn.modules.channels.adapters.discord.DiscordAdapter",
    "slack": "digitorn.modules.channels.adapters.slack.SlackAdapter",
    "voice": "digitorn.modules.channels.adapters.voice.VoiceAdapter",
}

_custom_adapters: dict[str, type[BaseChannelAdapter]] = {}


def register_adapter(type_name: str, adapter_class: type[BaseChannelAdapter]) -> None:
    """Register a custom adapter type at runtime."""
    _custom_adapters[type_name] = adapter_class
    logger.info("channel_adapter_registered type=%s cls=%s", type_name, adapter_class.__name__)


def get_adapter_class(type_name: str) -> type[BaseChannelAdapter]:
    """Resolve adapter type name to class. Raises ValueError if unknown."""
    if type_name in _custom_adapters:
        return _custom_adapters[type_name]

    import_path = _BUILTIN_ADAPTERS.get(type_name)
    if not import_path:
        available = sorted(set(_BUILTIN_ADAPTERS) | set(_custom_adapters))
        raise ValueError(
            f"Unknown channel adapter type: '{type_name}'. "
            f"Available: {', '.join(available)}"
        )

    module_path, class_name = import_path.rsplit(".", 1)
    try:
        import importlib
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(
            f"Failed to load adapter '{type_name}' from {import_path}: {exc}"
        ) from exc

    return cls


def list_adapter_types() -> list[str]:
    """Return all available adapter type names."""
    return sorted(set(_BUILTIN_ADAPTERS) | set(_custom_adapters))

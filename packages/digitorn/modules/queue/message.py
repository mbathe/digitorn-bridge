"""Queue message data structures."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueueMessage:
    """A message in the queue system."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    queue: str = ""
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    priority: int = 5  # 0=highest, 9=lowest
    timestamp: float = field(default_factory=time.time)
    attempts: int = 0
    max_retries: int = 3
    delay_until: float | None = None
    consumer_group: str | None = None
    ack_id: str | None = None

    def is_delayed(self) -> bool:
        return self.delay_until is not None and time.time() < self.delay_until

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "queue": self.queue,
            "body": self.body,
            "headers": self.headers,
            "priority": self.priority,
            "timestamp": self.timestamp,
            "attempts": self.attempts,
            "max_retries": self.max_retries,
            "delay_until": self.delay_until,
            "consumer_group": self.consumer_group,
            "ack_id": self.ack_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueueMessage:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

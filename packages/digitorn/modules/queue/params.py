"""Queue module — input models for actions."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateQueueParams(BaseModel):
    """Create or ensure a named queue exists."""

    name: str = Field(..., description="Queue name (alphanumeric + hyphens).")
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Backend-specific config (e.g. visibility_timeout, max_retries).",
    )


class PublishParams(BaseModel):
    """Publish a message to a queue."""

    queue: str = Field(..., description="Target queue name.")
    message: Any = Field(..., description="Message body (any JSON-serializable value).")
    priority: int = Field(5, description="Priority 0 (highest) to 9 (lowest).", ge=0, le=9)
    delay_seconds: float = Field(0, description="Hold message for N seconds before delivery.", ge=0, le=900)
    headers: dict[str, str] = Field(default_factory=dict, description="Message headers/metadata.")


class SubscribeParams(BaseModel):
    """Start consuming messages from a queue in the background."""

    queue: str = Field(..., description="Queue to subscribe to.")
    batch_size: int = Field(1, description="Messages per batch.", ge=1, le=100)
    filter_headers: dict[str, str] = Field(
        default_factory=dict,
        description="Only receive messages whose headers match these key-value pairs.",
    )


class UnsubscribeParams(BaseModel):
    """Stop consuming messages from a subscription."""

    subscription_id: str = Field(..., description="Subscription ID returned by subscribe.")


class ReceiveParams(BaseModel):
    """Pull messages from a queue (poll mode)."""

    queue: str = Field(..., description="Queue to receive from.")
    timeout: float = Field(5.0, description="Wait up to N seconds for messages.", ge=0, le=30)
    batch_size: int = Field(1, description="Max messages to receive.", ge=1, le=10)
    ack_mode: str = Field("manual", description="'auto' = auto-ack on receive, 'manual' = must call ack().")


class AckParams(BaseModel):
    """Acknowledge processed messages (removes them from the queue)."""

    queue: str = Field(..., description="Queue name.")
    message_ids: list[str] = Field(..., description="List of ack_id values from received messages.", min_length=1)


class NackParams(BaseModel):
    """Negative-acknowledge messages (retry or send to dead-letter)."""

    queue: str = Field(..., description="Queue name.")
    message_ids: list[str] = Field(..., description="List of ack_id values to reject.", min_length=1)
    requeue: bool = Field(True, description="True = retry later, False = send to dead-letter queue.")


class PeekParams(BaseModel):
    """Preview messages without consuming them."""

    queue: str = Field(..., description="Queue to peek into.")
    count: int = Field(5, description="Number of messages to preview.", ge=1, le=100)


class QueueStatsParams(BaseModel):
    """Get queue depth, consumer count, and throughput statistics."""

    queue: str = Field(..., description="Queue name.")


class ListQueuesParams(BaseModel):
    """List all known queues."""


class DeleteQueueParams(BaseModel):
    """Delete a queue and all its messages permanently."""

    queue: str = Field(..., description="Queue to delete.")


class PurgeParams(BaseModel):
    """Remove all messages from a queue without deleting the queue."""

    queue: str = Field(..., description="Queue to purge.")


class DeadLetterParams(BaseModel):
    """View messages in the dead-letter queue (failed after max retries)."""

    queue: str = Field(..., description="Queue whose dead-letter messages to view.")
    count: int = Field(10, description="Number of dead-letter messages to return.", ge=1, le=100)

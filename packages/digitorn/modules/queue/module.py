"""Queue module - event-driven message queue with InMemory and Redis backends."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field as PydanticField

from digitorn.modules.base import BaseModule, ActionResult, Platform
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ConstraintSpec, ModuleManifest

from .backends import InMemoryQueueBackend, QueueBackend, create_queue_backend
from .message import QueueMessage
from .params import (
    AckParams,
    CreateQueueParams,
    DeadLetterParams,
    DeleteQueueParams,
    ListQueuesParams,
    NackParams,
    PeekParams,
    PublishParams,
    PurgeParams,
    QueueStatsParams,
    ReceiveParams,
    SubscribeParams,
    UnsubscribeParams,
)

logger = logging.getLogger(__name__)

class QueueConfig(BaseModel):
    """Pydantic config for the queue module (validated at compile time)."""

    model_config = {"extra": "forbid"}

    workspace: str = PydanticField(
        default="",
        description=(
            "Auto-injected by the daemon at module init time. "
            "Do NOT set manually in YAML - the daemon resolves it from "
            "the app's workspace/workspace_mode config."
        ),
    )
    backend_url: str | None = PydanticField(None, description="Queue backend URL (None=in-memory, redis://...=Redis Streams)")
    default_max_retries: int = PydanticField(3, ge=0, le=100, description="Default max retries before dead-letter")

class QueueModule(BaseModule):
    MODULE_ID = "queue"
    VERSION = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]
    CONFIG_MODEL = QueueConfig

    CONSTRAINTS = [
        ConstraintSpec(name="allowed_queues", type="string_list", description="Restrict which queue names the agent can use.", scope="module"),
        ConstraintSpec(name="max_queues", type="integer", description="Maximum number of queues.", scope="module", default=50),
    ]

    def get_prompt_sections(self) -> list[dict[str, Any]]:
        return []  # Actions are self-descriptive via schema

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._backend: QueueBackend | None = None
        self._subscriptions: dict[str, asyncio.Task[None]] = {}
        self._app_id: str = "default"
        self._default_max_retries: int = 3

    async def on_start(self) -> None:
        config = self._config
        if isinstance(config, QueueConfig):
            backend_url = config.backend_url
            self._default_max_retries = config.default_max_retries
        elif isinstance(config, dict):
            backend_url = config.get("backend_url")
            self._default_max_retries = config.get("default_max_retries", 3)
        else:
            backend_url = getattr(config, "backend_url", None)
            self._default_max_retries = getattr(config, "default_max_retries", 3)

        self._app_id = getattr(self, "_app_id_override", "default")
        self._backend = create_queue_backend(url=backend_url, app_id=self._app_id)
        logger.info("queue_started: backend=%s app=%s", type(self._backend).__name__, self._app_id)

    async def on_stop(self) -> None:
        # Cancel all subscription consumer tasks AND wait for them to finish
        # to prevent data loss during shutdown.
        tasks_to_wait = []
        for sid, task in self._subscriptions.items():
            task.cancel()
            tasks_to_wait.append(task)
        if tasks_to_wait:
            try:
                await asyncio.gather(*tasks_to_wait, return_exceptions=True)
            except Exception as exc:
                logger.debug("queue_subscriptions_await_failed: %s", exc)
        self._subscriptions.clear()
        if self._backend:
            try:
                await self._backend.close()
            except Exception as exc:
                logger.warning("queue_backend_close_failed: %s", exc)
            self._backend = None

    def _ensure_backend(self) -> QueueBackend:
        if self._backend is None:
            self._backend = InMemoryQueueBackend()
        return self._backend

    @action(
        description="Create or ensure a named message queue exists.",
        params_model=CreateQueueParams,
        risk_level="medium",
        side_effects=["state_mutation"],
        aliases=["creer_file", "nouvelle_file"],
        tags=["queue", "admin"],
    )
    async def create_queue(self, params: CreateQueueParams) -> ActionResult:
        backend = self._ensure_backend()
        await backend.create_queue(params.name, params.config or None)
        return ActionResult(success=True, data={"queue": params.name, "created": True})

    @action(
        description="Publish a message to a queue with optional priority and delay.",
        params_model=PublishParams,
        risk_level="medium",
        side_effects=["state_mutation"],
        aliases=["publier", "envoyer", "send", "emit"],
        tags=["queue", "write"],
    )
    async def publish(self, params: PublishParams) -> ActionResult:
        backend = self._ensure_backend()
        msg = QueueMessage(
            queue=params.queue,
            body=params.message,
            headers=params.headers,
            priority=params.priority,
            max_retries=self._default_max_retries,
            delay_until=time.time() + params.delay_seconds if params.delay_seconds > 0 else None,
        )
        msg_id = await backend.publish(params.queue, msg)
        return ActionResult(success=True, data={
            "message_id": msg_id,
            "queue": params.queue,
            "priority": params.priority,
            "delayed": params.delay_seconds > 0,
        })

    @action(
        description="Start consuming messages from a queue in the background. You will be notified when messages arrive.",
        params_model=SubscribeParams,
        risk_level="low",
        aliases=["abonner", "ecouter", "listen"],
        tags=["queue", "read"],
    )
    async def subscribe(self, params: SubscribeParams) -> ActionResult:
        backend = self._ensure_backend()
        sub_id = f"sub-{uuid.uuid4().hex[:12]}"

        async def _consumer_loop() -> None:
            while True:
                try:
                    messages = await backend.receive(params.queue, batch_size=params.batch_size, timeout=5.0)
                    for msg in messages:
                        if params.filter_headers:
                            if not all(msg.headers.get(k) == v for k, v in params.filter_headers.items()):
                                continue
                        # Notify agent via stream if available
                        if self.stream:
                            await self.stream.partial_result({
                                "event": "queue_message",
                                "subscription_id": sub_id,
                                "message": msg.to_dict(),
                            })
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.warning("queue_consumer_error: %s", exc)
                    await asyncio.sleep(1.0)

        task = asyncio.create_task(_consumer_loop())
        self._subscriptions[sub_id] = task
        return ActionResult(success=True, data={
            "subscription_id": sub_id,
            "queue": params.queue,
            "batch_size": params.batch_size,
        })

    @action(
        description="Stop a background subscription.",
        params_model=UnsubscribeParams,
        risk_level="low",
        aliases=["desabonner"],
        tags=["queue", "admin"],
    )
    async def unsubscribe(self, params: UnsubscribeParams) -> ActionResult:
        task = self._subscriptions.pop(params.subscription_id, None)
        if task is None:
            return ActionResult(success=False, error=f"Subscription '{params.subscription_id}' not found.")
        task.cancel()
        return ActionResult(success=True, data={"subscription_id": params.subscription_id, "stopped": True})

    @action(
        description="Pull messages from a queue (poll mode). Use ack_mode='manual' to acknowledge after processing.",
        params_model=ReceiveParams,
        risk_level="low",
        aliases=["recevoir", "pull", "poll"],
        tags=["queue", "read"],
    )
    async def receive(self, params: ReceiveParams) -> ActionResult:
        backend = self._ensure_backend()
        messages = await backend.receive(params.queue, batch_size=params.batch_size, timeout=params.timeout)
        if params.ack_mode == "auto":
            ack_ids = [m.ack_id for m in messages if m.ack_id]
            if ack_ids:
                await backend.ack(params.queue, ack_ids)
        return ActionResult(success=True, data={
            "messages": [m.to_dict() for m in messages],
            "count": len(messages),
        })

    @action(
        description="Acknowledge messages after successful processing.",
        params_model=AckParams,
        risk_level="low",
        side_effects=["state_mutation"],
        aliases=["confirmer", "acknowledge"],
        tags=["queue", "write"],
    )
    async def ack(self, params: AckParams) -> ActionResult:
        backend = self._ensure_backend()
        count = await backend.ack(params.queue, params.message_ids)
        return ActionResult(success=True, data={"acknowledged": count})

    @action(
        description="Reject messages - requeue for retry or send to dead-letter queue.",
        params_model=NackParams,
        risk_level="low",
        side_effects=["state_mutation"],
        aliases=["rejeter", "reject"],
        tags=["queue", "write"],
    )
    async def nack(self, params: NackParams) -> ActionResult:
        backend = self._ensure_backend()
        count = await backend.nack(params.queue, params.message_ids, requeue=params.requeue)
        return ActionResult(success=True, data={"rejected": count, "requeued": params.requeue})

    @action(
        description="Preview messages without consuming them.",
        params_model=PeekParams,
        risk_level="low",
        aliases=["apercu", "preview"],
        tags=["queue", "read"],
    )
    async def peek(self, params: PeekParams) -> ActionResult:
        backend = self._ensure_backend()
        messages = await backend.peek(params.queue, params.count)
        return ActionResult(success=True, data={
            "messages": [m.to_dict() for m in messages],
            "count": len(messages),
        })

    @action(
        description="Get queue statistics: depth, consumer count, throughput.",
        params_model=QueueStatsParams,
        risk_level="low",
        aliases=["statistiques_file", "info_file"],
        tags=["queue", "info"],
    )
    async def queue_stats(self, params: QueueStatsParams) -> ActionResult:
        backend = self._ensure_backend()
        s = await backend.stats(params.queue)
        return ActionResult(success=True, data=s.to_dict())

    @action(
        description="List all known queues.",
        params_model=ListQueuesParams,
        risk_level="low",
        aliases=["lister_files"],
        tags=["queue", "info"],
    )
    async def list_queues(self, params: ListQueuesParams) -> ActionResult:
        backend = self._ensure_backend()
        queues = await backend.list_queues()
        return ActionResult(success=True, data={"queues": queues, "count": len(queues)})

    @action(
        description="Delete a queue and all its messages permanently.",
        params_model=DeleteQueueParams,
        risk_level="high",
        side_effects=["state_mutation"],
        irreversible=True,
        aliases=["supprimer_file"],
        tags=["queue", "admin"],
    )
    async def delete_queue(self, params: DeleteQueueParams) -> ActionResult:
        backend = self._ensure_backend()
        deleted = await backend.delete_queue(params.queue)
        return ActionResult(success=True, data={"queue": params.queue, "deleted": deleted})

    @action(
        description="Remove all messages from a queue without deleting the queue.",
        params_model=PurgeParams,
        risk_level="high",
        side_effects=["state_mutation"],
        irreversible=True,
        aliases=["vider_file", "clear_queue"],
        tags=["queue", "admin"],
    )
    async def purge(self, params: PurgeParams) -> ActionResult:
        backend = self._ensure_backend()
        count = await backend.purge(params.queue)
        return ActionResult(success=True, data={"queue": params.queue, "purged": count})

    @action(
        description="View messages in the dead-letter queue - messages that failed after max retries.",
        params_model=DeadLetterParams,
        risk_level="low",
        aliases=["lettres_mortes", "dlq"],
        tags=["queue", "read"],
    )
    async def dead_letter(self, params: DeadLetterParams) -> ActionResult:
        backend = self._ensure_backend()
        messages = await backend.dead_letter(params.queue, params.count)
        return ActionResult(success=True, data={
            "messages": [m.to_dict() for m in messages],
            "count": len(messages),
        })

    def state_snapshot(self) -> dict[str, Any]:
        return {
            "app_id": self._app_id,
            "active_subscriptions": list(self._subscriptions.keys()),
        }

    async def restore_state(self, state: dict[str, Any]) -> None:
        self._app_id = state.get("app_id", "default")

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": "Event-driven message queue - InMemory and Redis Streams backends with dead-letter and priorities.",
            "author": "Digitorn Team",
            "tags": ["core", "queue", "messaging", "event-driven"],
        })

"""Drop-in proxies for modules and LLM providers hosted in workers.

These types implement the SAME interfaces as their in-process
counterparts so existing callers (tool_exec, agent_loop, hooks,
middleware) cannot tell whether they're talking to a local module or
a remote worker. That transparency is the cornerstone of the design:
without it we'd have to thread "is this remoted?" checks through the
whole codebase, and the daemon's behaviour would diverge from today.

What we INTENTIONALLY do NOT do here:
  * Patch module decorators (``@action``) -- the worker has those
    via the real module loader; the proxy just forwards by name.
  * Re-implement business logic -- proxies are pure forwarders.
  * Touch the LLM provider class hierarchy -- ``LLMProviderProxy``
    duck-types ``BaseLLMProvider``'s public surface used by
    ``agent_loop`` / ``streaming``.

Skeleton status: the surface is locked in, but the bridge to the
existing module/provider abstractions is filled in during Phase 2
(once we know exactly which interface methods need to be forwarded
and the AgentContext serialisation shape).
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from .client import WorkerClient
from .registry import WorkerEndpoint

logger = logging.getLogger(__name__)


class ModuleProxy:
    """Transparent stand-in for a Digitorn module.

    The daemon's module registry holds one of these in place of the
    real module instance. When the dispatcher calls an action --
    ``await module.bash(command=...)`` or
    ``await module.dispatch("bash", {...})`` -- the proxy serialises
    the args, sends them to the worker, and deserialises the
    ``ActionResult``-shaped response.

    Phase 1 (this file) defines the public surface only. The
    integration glue (how the proxy gets installed in the registry,
    how AgentContext is serialised into the call) is added in
    Phase 2 once we wire it into bootstrap.
    """

    def __init__(
        self,
        module_name: str,
        endpoint: WorkerEndpoint,
        *,
        client: WorkerClient | None = None,
    ) -> None:
        self._module_name = module_name
        self._endpoint = endpoint
        # Allow injecting a pre-built client (tests, connection
        # reuse across multiple proxies on the same endpoint).
        self._client = client or WorkerClient(endpoint)

    @property
    def name(self) -> str:
        return self._module_name

    @property
    def endpoint(self) -> WorkerEndpoint:
        return self._endpoint

    async def call_action(
        self,
        action: str,
        args: dict[str, Any],
        *,
        ctx_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Forward one action call to the worker. ``ctx_payload`` is
        the AgentContext-derived dict shape we agree on in Phase 2
        (session_id, user_id, agent_id, workspace, etc.).

        Returns the raw JSON-deserialised payload -- the dispatcher
        in Phase 2 wraps this into an ``ActionResult`` instance so
        the caller sees the same type as today.
        """
        logger.debug(
            "module_proxy_call module=%s action=%s endpoint=%s",
            self._module_name, action, self._endpoint.worker_id,
        )
        return await self._client.call_action(
            self._module_name, action, args, ctx=ctx_payload,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class LLMProviderProxy:
    """Transparent stand-in for an LLM provider.

    Duck-types the subset of ``BaseLLMProvider`` that ``agent_loop``
    and ``streaming`` actually call:

      * ``chat_stream(messages, tools, **gen_params)`` -> async
        iterator of chunks
      * ``count_tokens(text)`` -> int (cheap, kept in-process for
        latency unless explicitly remoted)
      * ``model`` / ``provider_name`` properties

    LLM streaming is the trickiest path: the worker forwards
    anthropic/openai SSE chunks line-by-line via
    ``WorkerClient.stream_action``, so the daemon never sees an SSL
    handshake or a 30-second LLM response held in memory.

    Phase 1 status: surface only. The chunk-shape translation
    (anthropic vs openai schemas, ``input_json_delta`` accumulation,
    tool-call reconstruction) is wired in Phase 3.
    """

    def __init__(
        self,
        endpoint: WorkerEndpoint,
        *,
        provider_name: str,
        model: str,
        client: WorkerClient | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._provider_name = provider_name
        self._model = model
        self._client = client or WorkerClient(endpoint)

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model(self) -> str:
        return self._model

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **gen_params: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream LLM chunks from the remote worker. The worker runs
        the actual provider (anthropic SDK / openai SDK / etc.) and
        forwards each chunk as one NDJSON line to the daemon.

        Critical property: this method MUST NOT bufferise. Yielding
        per chunk is what keeps the agent loop responsive and lets
        the daemon emit Socket.IO tokens to the client live.
        """
        args = {
            "messages": messages,
            "tools": tools or [],
            "model": self._model,
            "provider": self._provider_name,
            "gen_params": gen_params,
        }
        async for chunk in self._client.stream_action(
            "llm_provider", "chat_stream", args,
        ):
            yield chunk

    async def count_tokens(self, text: str) -> int:
        """Kept in-process by default: token counting is CPU-cheap
        (tiktoken / anthropic local tokenizer) and round-tripping
        through HTTP would add latency to every prompt-render. The
        proxy DOES expose a remote path for cases where the daemon
        doesn't have the tokenizer installed.
        """
        result = await self._client.call_action(
            "llm_provider", "count_tokens",
            {"text": text, "model": self._model},
        )
        return int(result.get("tokens", 0))

    async def aclose(self) -> None:
        await self._client.aclose()

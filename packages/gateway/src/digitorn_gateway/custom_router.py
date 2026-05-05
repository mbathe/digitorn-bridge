"""Custom provider router.

When the model catalogue declares `provider: custom` on an entry,
the dispatcher in `llm_call.py` routes the request HERE instead of
calling LiteLLM. This is the extension point for:

* In-house models (vLLM, Ollama, llama.cpp on a private endpoint)
* Providers LiteLLM doesn't yet support
* Anything that needs a non-OpenAI protocol (gRPC, WebSocket, custom)

The default implementation below is a STUB that raises a clear
"not implemented" error. The intent is that operators add their
own handlers as they need them, without touching the dispatcher.

Pattern to add a custom provider:

    # 1. Add an entry in models.yaml:
    #    mycorp-finetune:
    #      provider: custom
    #      model: mycorp-llama-7b
    #      endpoint: https://internal.mycorp.local/llm
    #
    # 2. In CustomRouter.handle, branch on `entry.model` and call
    #    the appropriate client. Return an OpenAI-shaped dict.

The return value MUST follow the OpenAI chat completion shape so
the route handler can serialise it identically to a LiteLLM response.
Streaming MUST be a (sync or async) iterator of OpenAI-shaped chunk
dicts.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from digitorn_gateway.models import ModelEntry

logger = logging.getLogger(__name__)


class CustomProviderNotImplemented(RuntimeError):
    """Raised when a request hits `provider: custom` but the
    gateway operator hasn't wired up a handler for that model."""


class CustomRouter:
    """Pluggable router for custom providers. Subclass + override
    `handle` / `handle_stream` to add your own. The default
    implementation raises CustomProviderNotImplemented.

    The router is constructed once at boot and reused per request -
    feel free to hold connection pools / clients on `self`.
    """

    async def handle(
        self,
        *,
        entry: ModelEntry,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Non-streaming completion. Return an OpenAI-shaped dict:

            {
              "id": "chatcmpl-...",
              "object": "chat.completion",
              "created": <unix>,
              "model": entry.model,
              "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "..."},
                "finish_reason": "stop",
              }],
              "usage": {
                "prompt_tokens": N,
                "completion_tokens": M,
                "total_tokens": N+M,
              },
            }
        """
        raise CustomProviderNotImplemented(
            f"No custom handler for model={entry.model!r} (alias={entry.alias!r}). "
            "Override CustomRouter.handle in your deployment.",
        )

    async def handle_stream(
        self,
        *,
        entry: ModelEntry,
        body: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """Streaming completion. Yield OpenAI-shaped chunk dicts:

            {
              "id": "chatcmpl-...",
              "object": "chat.completion.chunk",
              "created": <unix>,
              "model": entry.model,
              "choices": [{
                "index": 0,
                "delta": {"content": "..."} | {"role": "assistant"},
                "finish_reason": null | "stop",
              }],
            }

        End the stream with a final chunk where `finish_reason` is set.
        """
        raise CustomProviderNotImplemented(
            f"No streaming custom handler for model={entry.model!r}.",
        )
        # The yield below is unreachable but tells type-checkers this
        # is an async generator, not a coroutine.
        yield {}  # pragma: no cover


# Process-wide instance. Replace with `set_router(MyRouter())` from
# the `main.py` startup hook to install your custom implementation.
_router: CustomRouter | None = None


def get_router() -> CustomRouter:
    global _router
    if _router is None:
        _router = CustomRouter()
    return _router


def set_router(r: CustomRouter) -> None:
    global _router
    _router = r

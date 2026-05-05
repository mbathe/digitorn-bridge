"""Digitorn LLM gateway.

OpenAI-compatible HTTP service that fronts every LLM call coming from a
digitorn user authenticated with their digitorn account. The gateway:

* Validates the user's JWT (issued by `digitorn_auth`).
* Resolves the requested model alias to a concrete provider/model id.
* Delegates the actual provider call to `litellm` (used as a library,
  not as the LiteLLM proxy server). LiteLLM gives us 100+ providers,
  format conversion, streaming, retries.
* For models that are not in LiteLLM's catalogue, the routing layer
  has an explicit hook to call our own custom client - the gateway
  is therefore not bound to LiteLLM's coverage.
* Tracks per-user usage so quota can be debited centrally.

The gateway is the single entry point for every digitorn-managed LLM
call. The on-device daemon talks to it via `provider: digitorn` in the
agent YAML; the daemon never holds the underlying Anthropic / OpenAI
keys directly.
"""

__version__ = "0.1.0"

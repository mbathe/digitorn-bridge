"""Anthropic native SDK provider.

Uses the ``anthropic`` async client for Claude models.
Supports streaming, tool use, vision, and extended thinking.

Supports Claude Code OAuth tokens for testing:
  - Set api_key to "claude-code" to auto-read from ~/.claude/.credentials.json
  - Or pass the OAuth token directly (sk-ant-oat*)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, AsyncIterator

from digitorn.modules.llm_provider.providers.base import (
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    ProviderCapabilities,
    ProviderInfo,
    StreamChunk,
    TokenUsage,
)

_logger = logging.getLogger(__name__)


def _recover_tool_json(raw: str) -> dict | None:
    """Try to recover a dict from truncated JSON (e.g. huge write content)."""
    import re
    s = raw.strip()
    # Try closing braces
    for suffix in ['"}', '"}}', '"}]', '}']:
        try:
            return json.loads(s + suffix)
        except (json.JSONDecodeError, ValueError):
            continue
    # Extract key-value pairs with regex
    result = {}
    for m in re.finditer(r'"(\w+)"\s*:\s*"((?:[^"\\]|\\.)*)(?:"|$)', s):
        key, val = m.group(1), m.group(2)
        try:
            result[key] = json.loads(f'"{val}"')
        except Exception:
            result[key] = val
    return result or None

# Cache the token to avoid re-reading the file on every call
_cached_token: str | None = None
_cached_token_expires: float = 0.0


def _load_claude_code_token() -> str | None:
    """Load OAuth token from Claude Code's credentials file.

    Reads ~/.claude/.credentials.json which is written by Claude Code
    when you authenticate with your Claude Max/Pro subscription.

    The token is cached in memory and refreshed when it expires.
    This is intended for LOCAL TESTING ONLY — do not use in production.
    """
    global _cached_token, _cached_token_expires

    # Return cached token if still valid (with 5 min buffer)
    if _cached_token and time.time() < (_cached_token_expires - 300):
        return _cached_token

    # Try multiple locations
    candidates = [
        Path.home() / ".claude" / ".credentials.json",
        Path.home() / ".claude" / "credentials.json",
    ]

    for cred_path in candidates:
        if not cred_path.exists():
            continue
        try:
            data = json.loads(cred_path.read_text(encoding="utf-8"))
            oauth = data.get("claudeAiOauth", {})
            token = oauth.get("accessToken")
            if not token:
                continue

            # Check expiry (milliseconds timestamp)
            expires_at = oauth.get("expiresAt", 0)
            if isinstance(expires_at, (int, float)) and expires_at > 0:
                expires_sec = expires_at / 1000.0
                if time.time() > expires_sec:
                    _logger.warning(
                        "Claude Code OAuth token expired. "
                        "Run 'claude' to refresh it, then restart."
                    )
                    continue
                _cached_token_expires = expires_sec
            else:
                # Unknown expiry — assume 1 hour from now
                _cached_token_expires = time.time() + 3600

            _cached_token = token
            sub = oauth.get("subscriptionType", "?")
            _logger.info(
                "Loaded Claude Code OAuth token (subscription=%s, expires_in=%.0f min)",
                sub,
                (_cached_token_expires - time.time()) / 60,
            )
            return token

        except Exception as exc:
            _logger.debug("Failed to read Claude Code credentials: %s", exc)
            continue

    return None


_ANTHROPIC_MODELS: dict[str, ProviderCapabilities] = {
    "claude-opus-4-20250514": ProviderCapabilities(
        streaming=True, tool_use=True, vision=True, json_mode=True,
        system_message=True, max_context_window=200_000, max_output_tokens=32_000,
    ),
    "claude-sonnet-4-20250514": ProviderCapabilities(
        streaming=True, tool_use=True, vision=True, json_mode=True,
        system_message=True, max_context_window=200_000, max_output_tokens=16_000,
    ),
    "claude-sonnet-4-5-20250514": ProviderCapabilities(
        streaming=True, tool_use=True, vision=True, json_mode=True,
        system_message=True, max_context_window=200_000, max_output_tokens=16_000,
    ),
    "claude-haiku-4-5-20251001": ProviderCapabilities(
        streaming=True, tool_use=True, vision=True, json_mode=True,
        system_message=True, max_context_window=200_000, max_output_tokens=8_192,
    ),
}

_DEFAULT_CAPABILITIES = ProviderCapabilities(
    streaming=True, tool_use=True, vision=True, json_mode=True,
    system_message=True, max_context_window=200_000, max_output_tokens=8_192,
)


def _capabilities_for(model: str) -> ProviderCapabilities:
    """Resolve capabilities for a model, with prefix matching."""
    if model in _ANTHROPIC_MODELS:
        return _ANTHROPIC_MODELS[model]
    for known, caps in _ANTHROPIC_MODELS.items():
        if known.startswith(model) or model.startswith(known.rsplit("-", 1)[0]):
            return caps
    return _DEFAULT_CAPABILITIES


class AnthropicProvider(BaseLLMProvider):
    """Anthropic native SDK provider for Claude models."""

    BACKEND = "anthropic"

    # Optional callback set by the TUI backend to notify spinner during rate limits.
    # Signature: on_rate_limit(attempt: int, max_attempts: int, wait: float) -> None
    on_rate_limit: Any = None

    async def initialize(self) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError(
                "anthropic package required: pip install anthropic"
            ) from exc

        api_key = self.api_key
        auth_token = None

        # Support Claude Code OAuth tokens (sk-ant-oat*) and "claude-code" magic key
        if api_key == "claude-code" or (not api_key and not self.base_url):
            # Try to read OAuth token from Claude Code credentials
            token = _load_claude_code_token()
            if token:
                auth_token = token
                api_key = None
        elif api_key and api_key.startswith("sk-ant-oat"):
            # Direct OAuth token passed as api_key
            auth_token = api_key
            api_key = None

        client_kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
        }

        if auth_token:
            # OAuth mode — mimic Claude Code's exact headers so the API accepts the token.
            # Required: x-app, User-Agent, X-Claude-Code-Session-Id, anthropic-beta.
            # Also increase max_retries to 10 (same as Claude Code) because OAuth
            # tokens share rate limits with other Claude Code sessions.
            import uuid as _uuid
            _session = str(_uuid.uuid4())
            client_kwargs["auth_token"] = auth_token
            client_kwargs["max_retries"] = 10  # Claude Code uses 10
            client_kwargs["timeout"] = max(self.timeout or 120.0, 300.0)  # 5 min min
            client_kwargs["default_headers"] = {
                "x-app": "cli",
                "User-Agent": "claude-cli/1.0.34 (external, cli)",
                "X-Claude-Code-Session-Id": _session,
                "anthropic-beta": "oauth-2025-04-20,claude-code-20250219",
            }
            _logger.info(
                "Anthropic OAuth mode: max_retries=10, timeout=%.0fs "
                "(rate limit retries enabled — shared with Claude Code sessions)",
                client_kwargs["timeout"],
            )
        else:
            client_kwargs["api_key"] = api_key or None

        self._client = anthropic.AsyncAnthropic(**client_kwargs)

    async def _retry_on_rate_limit(self, coro_fn, max_attempts: int = 15):
        """Retry a coroutine on 429 RateLimitError with exponential backoff.

        Also handles 401 AuthenticationError by refreshing the OAuth token.
        This is needed because OAuth tokens share rate limits with other
        Claude Code sessions. The SDK's built-in retry may not be enough
        when another session is actively streaming.
        """
        import asyncio as _aio
        try:
            from anthropic import RateLimitError as _RLE
            from anthropic import AuthenticationError as _AE
        except ImportError:
            _RLE = Exception
            _AE = type(None)  # Never matches

        # Also catch network errors (connection refused, DNS, timeout)
        try:
            import anthropic as _anth
            _NETWORK_ERRORS = (_anth.APIConnectionError, _anth.APITimeoutError)
        except (ImportError, AttributeError):
            _NETWORK_ERRORS = (ConnectionError, TimeoutError, OSError)

        _auth_retried = False
        for attempt in range(max_attempts):
            try:
                return await coro_fn()
            except _AE:
                # 401 — try refreshing OAuth token once
                if _auth_retried:
                    raise
                _auth_retried = True
                _logger.info("Auth error (401) — refreshing OAuth token...")
                global _cached_token, _cached_token_expires
                _cached_token = None
                _cached_token_expires = 0.0
                new_token = _load_claude_code_token()
                if new_token and self._client is not None:
                    self._client.auth_token = new_token
                    _logger.info("OAuth token refreshed, retrying...")
                    continue
                raise
            except _RLE:
                if attempt >= max_attempts - 1:
                    raise
                wait = min(2 ** attempt, 30) + (attempt * 0.5)
                _logger.info(
                    "Rate limited (attempt %d/%d), waiting %.1fs...",
                    attempt + 1, max_attempts, wait,
                )
                if self.on_rate_limit is not None:
                    try:
                        self.on_rate_limit(attempt + 1, max_attempts, wait)
                    except Exception:
                        pass
                await _aio.sleep(wait)
            except _NETWORK_ERRORS as exc:
                # Network errors — retry with backoff
                if attempt >= max_attempts - 1:
                    raise
                wait = min(2 ** attempt, 16)
                _logger.warning(
                    "Network error (attempt %d/%d): %s — retrying in %.0fs",
                    attempt + 1, max_attempts, exc, wait,
                )
                if self.on_rate_limit is not None:
                    try:
                        self.on_rate_limit(attempt + 1, max_attempts, wait)
                    except Exception:
                        pass
                await _aio.sleep(wait)
        raise RuntimeError("Max retries exceeded")

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ChatResponse:
        params = self._build_params(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            tools=tools,
            tool_choice=tool_choice,
            extra=extra,
        )

        if self._client is None:
            raise RuntimeError(
                "Anthropic client not initialized — check API key configuration. "
                "If using api_key: 'claude-code', the OAuth token may be missing or expired."
            )

        response = await self._retry_on_rate_limit(
            lambda: self._client.messages.create(**params)
        )
        return self._parse_response(response)

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        if self._client is None:
            await self.initialize()
        if self._client is None:
            raise RuntimeError(
                "Anthropic client not initialized — check API key configuration. "
                "Set api_key to 'claude-code' to use Claude Code OAuth, or provide "
                "an ANTHROPIC_API_KEY."
            )

        params = self._build_params(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            tools=tools,
            tool_choice=tool_choice,
            extra=extra,
        )

        # Retry on rate limit / auth error before starting stream
        import asyncio as _aio
        try:
            from anthropic import RateLimitError as _RLE
            from anthropic import AuthenticationError as _AE
        except ImportError:
            _RLE = Exception
            _AE = type(None)

        stream_ctx = None
        _auth_retried = False
        for _attempt in range(15):
            try:
                stream_ctx = self._client.messages.stream(**params)
                break
            except _AE:
                if _auth_retried:
                    raise
                _auth_retried = True
                _logger.info("Stream auth error (401) — refreshing OAuth token...")
                global _cached_token, _cached_token_expires
                _cached_token = None
                _cached_token_expires = 0.0
                new_token = _load_claude_code_token()
                if new_token and self._client is not None:
                    self._client.auth_token = new_token
                    _logger.info("OAuth token refreshed for stream, retrying...")
                    continue
                raise
            except _RLE:
                wait = min(2 ** _attempt, 30) + (_attempt * 0.5)
                _logger.info("Stream rate limited (attempt %d/15), waiting %.1fs...", _attempt + 1, wait)
                if self.on_rate_limit is not None:
                    try:
                        self.on_rate_limit(_attempt + 1, 15, wait)
                    except Exception:
                        pass
                await _aio.sleep(wait)
        if stream_ctx is None:
            raise RuntimeError("Rate limit: could not start stream after 15 attempts")

        # Track tool input JSON fragments — SDK may fail to reconstruct large inputs
        _tool_json_acc: dict[int, list[str]] = {}  # block_index → [json_fragments]
        _tool_meta: dict[int, dict] = {}  # block_index → {"id": ..., "name": ...}
        _current_block_index = -1

        async with stream_ctx as stream:
          _stream_ok = True
          try:
            async for event in stream:
                etype = getattr(event, "type", "")

                if etype == "message_start":
                    msg = getattr(event, "message", None)
                    if msg and hasattr(msg, "usage"):
                        _in = getattr(msg.usage, "input_tokens", 0) or 0
                        if _in > 0:
                            yield StreamChunk(
                                delta="",
                                usage=TokenUsage(prompt_tokens=_in, completion_tokens=0, total_tokens=_in),
                            )

                elif etype == "message_delta":
                    _usage = getattr(event, "usage", None)
                    if _usage:
                        _out = getattr(_usage, "output_tokens", 0) or 0
                        if _out > 0:
                            yield StreamChunk(
                                delta="",
                                usage=TokenUsage(prompt_tokens=0, completion_tokens=_out, total_tokens=_out),
                            )

                elif etype == "content_block_start":
                    _current_block_index = getattr(event, "index", _current_block_index + 1)
                    cb = getattr(event, "content_block", None)
                    if cb and getattr(cb, "type", "") == "tool_use":
                        _tool_meta[_current_block_index] = {
                            "id": getattr(cb, "id", ""),
                            "name": getattr(cb, "name", ""),
                        }
                        _tool_json_acc[_current_block_index] = []

                elif etype == "content_block_delta":
                    delta_obj = getattr(event, "delta", None)
                    if delta_obj is None:
                        continue
                    delta_type = getattr(delta_obj, "type", "")
                    if delta_type == "text_delta":
                        yield StreamChunk(delta=getattr(delta_obj, "text", ""))
                    elif delta_type == "thinking_delta":
                        yield StreamChunk(
                            delta="",
                            thinking=getattr(delta_obj, "thinking", ""),
                        )
                    elif delta_type == "input_json_delta":
                        idx = getattr(event, "index", _current_block_index)
                        fragment = getattr(delta_obj, "partial_json", "")
                        if fragment and idx in _tool_json_acc:
                            _tool_json_acc[idx].append(fragment)

          except (ConnectionError, TimeoutError, OSError) as stream_exc:
            _logger.warning("Stream interrupted: %s", stream_exc)
            _stream_ok = False

          # Get final message — always needed for tool calls
          try:
              final = await stream.get_final_message()
          except Exception as final_exc:
              if _stream_ok:
                  _logger.warning("get_final_message failed: %s", final_exc)
              final = type("FakeResponse", (), {
                  "content": [],
                  "usage": type("U", (), {"input_tokens": 0, "output_tokens": 0})(),
                  "stop_reason": "error" if not _stream_ok else "end_turn",
                  "id": "partial",
              })()

          tool_calls = []
          for i, block in enumerate(final.content):
              if block.type == "tool_use":
                  tool_input = block.input or {}

                  # Check if we have accumulated fragments that might be more complete
                  if i in _tool_json_acc and _tool_json_acc[i]:
                      raw_json = "".join(_tool_json_acc[i])
                      if raw_json.strip():
                          try:
                              parsed_from_fragments = json.loads(raw_json)
                              if isinstance(parsed_from_fragments, dict):
                                  if len(parsed_from_fragments) > len(tool_input):
                                      _logger.info(
                                          "Using accumulated fragments (%d keys) over SDK result (%d keys) for %s",
                                          len(parsed_from_fragments), len(tool_input), block.name,
                                      )
                                      tool_input = parsed_from_fragments
                          except json.JSONDecodeError:
                              recovered = _recover_tool_json(raw_json)
                              if recovered and len(recovered) > len(tool_input):
                                  _logger.info(
                                      "Recovered %d keys from partial JSON for %s",
                                      len(recovered), block.name,
                                  )
                                  tool_input = recovered

                  tool_calls.append({
                      "id": block.id,
                      "name": block.name,
                      "arguments": tool_input,
                  })

          final_chunk = StreamChunk(
              delta="",
              finish_reason=final.stop_reason,
              usage=TokenUsage(
                  prompt_tokens=final.usage.input_tokens,
                  completion_tokens=final.usage.output_tokens,
                  total_tokens=final.usage.input_tokens + final.usage.output_tokens,
                  cache_read_tokens=getattr(final.usage, "cache_read_input_tokens", 0) or 0,
                  cache_creation_tokens=getattr(final.usage, "cache_creation_input_tokens", 0) or 0,
              ),
          )
          if tool_calls:
              final_chunk.tool_calls = tool_calls

          # Diag: persist authoritative final usage so the truthfulness
          # test can compare daemon-reported numbers against the provider
          # billed numbers byte-exact. Zero-cost when env var is absent.
          import os as _os
          if _os.environ.get("DIGITORN_DIAG_WIRE_PAYLOAD"):
              try:
                  import time as _time
                  from pathlib import Path as _Path
                  diag_dir = _Path(_os.environ.get("DIGITORN_DIAG_DIR", "/tmp"))
                  diag_dir.mkdir(parents=True, exist_ok=True)
                  (diag_dir / f"wire_usage_{int(_time.time()*1000)}.json").write_text(
                      json.dumps({
                          "prompt_tokens": final_chunk.usage.prompt_tokens,
                          "completion_tokens": final_chunk.usage.completion_tokens,
                          "total_tokens": final_chunk.usage.total_tokens,
                      }),
                      encoding="utf-8",
                  )
              except Exception:
                  pass

          yield final_chunk

    def get_info(self) -> ProviderInfo:
        caps = _capabilities_for(self.model)
        return ProviderInfo(
            provider_id=self.provider_id,
            backend=self.BACKEND,
            model=self.model,
            capabilities=caps,
            extra={"base_url": self.base_url or "https://api.anthropic.com"},
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
        self._client = None


    def _build_params(
        self,
        messages: list[ChatMessage],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build Anthropic API params from messages and overrides."""
        merged = self._merge_params(**kwargs)
        extra = merged.pop("extra", None) or {}

        system_parts: list[str] = []
        api_messages: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == "system":
                system_parts.append(msg.content if isinstance(msg.content, str) else str(msg.content))
            elif msg.role == "assistant" and msg.tool_calls:
                content_blocks: list[dict[str, Any]] = []
                if msg.content:
                    content_blocks.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        import json as _json
                        try:
                            args = _json.loads(args)
                        except (ValueError, _json.JSONDecodeError):
                            args = {}
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    })
                api_messages.append({"role": "assistant", "content": content_blocks})
            elif msg.role == "tool":
                api_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id or "",
                        "content": msg.content if isinstance(msg.content, str) else str(msg.content),
                    }],
                })
            else:
                # Support multimodal content blocks (text + images)
                if isinstance(msg.content, list):
                    # Content blocks — pass through, converting image_url to Anthropic format
                    blocks = []
                    for block in msg.content:
                        if not isinstance(block, dict):
                            blocks.append({"type": "text", "text": str(block)})
                        elif block.get("type") == "text":
                            blocks.append(block)
                        elif block.get("type") == "image":
                            # Already in Anthropic format
                            blocks.append(block)
                        elif block.get("type") == "image_url":
                            # OpenAI format → convert to Anthropic
                            url = block.get("image_url", {}).get("url", "")
                            if url.startswith("data:"):
                                # data:image/png;base64,iVBOR...
                                header, _, data = url.partition(",")
                                mime = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
                                blocks.append({
                                    "type": "image",
                                    "source": {"type": "base64", "media_type": mime, "data": data},
                                })
                            else:
                                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
                        elif block.get("type") == "image_ref":
                            # Unresolved ref — should have been resolved before reaching provider
                            alt = block.get("alt_text", "image")
                            blocks.append({"type": "text", "text": f"[Image: {alt}]"})
                        else:
                            blocks.append(block)
                    api_messages.append({"role": msg.role, "content": blocks})
                else:
                    content = msg.content if isinstance(msg.content, str) else str(msg.content)
                    api_messages.append({"role": msg.role, "content": content})

        params: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": merged.get("max_tokens", 4096),
        }

        if system_parts:
            params["system"] = "\n\n".join(system_parts)
        if merged.get("temperature") is not None:
            params["temperature"] = merged["temperature"]
        if merged.get("top_p") is not None:
            params["top_p"] = merged["top_p"]
        if merged.get("stop"):
            params["stop_sequences"] = merged["stop"]
        if merged.get("tools"):
            params["tools"] = self._convert_tools(merged["tools"])
        if merged.get("tool_choice"):
            converted = self._convert_tool_choice(merged["tool_choice"])
            if converted is None:
                # LP1: tool_choice="none" → drop tools entirely
                params.pop("tools", None)
            else:
                params["tool_choice"] = converted

        params.update(extra)
        return params

    def _parse_response(self, response: Any) -> ChatResponse:
        """Parse Anthropic API response into ChatResponse."""
        content_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        for block in response.content:
            if block.type == "text":
                content_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": block.input,
                    },
                })

        return ChatResponse(
            content="\n".join(content_parts),
            model=response.model,
            finish_reason=response.stop_reason,
            usage=TokenUsage(
                prompt_tokens=response.usage.input_tokens,
                completion_tokens=response.usage.output_tokens,
                total_tokens=response.usage.input_tokens + response.usage.output_tokens,
                cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
                cache_creation_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            ),
            tool_calls=tool_calls or None,
            raw={"id": response.id, "type": response.type},
        )

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI-style tool definitions to Anthropic format."""
        converted = []
        for tool in tools:
            if "function" in tool:
                fn = tool["function"]
                converted.append({
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {}),
                })
            else:
                converted.append(tool)
        return converted

    @staticmethod
    def _convert_tool_choice(choice: str | dict) -> dict[str, Any] | None:
        """Convert tool_choice to Anthropic format.

        LP1: Anthropic doesn't support "none" — to disable tools, the
        caller must not pass tools at all. Returning None signals "drop
        tool_choice and tools entirely". Previously this returned auto
        which caused the model to make unwanted tool calls.
        """
        if isinstance(choice, str):
            if choice == "none":
                return None  # Caller must skip tools entirely
            mapping = {
                "auto": {"type": "auto"},
                "required": {"type": "any"},
                "any": {"type": "any"},
            }
            return mapping.get(choice, {"type": "auto"})
        return choice

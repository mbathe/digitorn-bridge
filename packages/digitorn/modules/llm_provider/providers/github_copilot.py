"""GitHubCopilotProvider - use a GitHub Copilot subscription as an LLM backend."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from digitorn.modules.llm_provider.providers.openai_compat import (
    OpenAICompatProvider,
)

logger = logging.getLogger(__name__)

_COPILOT_BASE_URL = "https://api.githubcopilot.com"
_GH_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"

# Headers mimicking a recent VS Code Copilot Chat extension.
_EDITOR_VERSION = "vscode/1.96.0"
_EDITOR_PLUGIN_VERSION = "copilot-chat/0.27.0"
_COPILOT_INTEGRATION_ID = "vscode-chat"
_USER_AGENT = "GithubCopilot/1.270.0"

_REFRESH_BUFFER_SECONDS = 60.0

class GitHubCopilotProvider(OpenAICompatProvider):
    """OpenAI-compatible provider routed through `api.githubcopilot.com`."""

    BACKEND = "github_copilot"

    def __init__(
        self,
        provider_id: str,
        model: str,
        *,
        api_key: str = "",
        base_url: str | None = None,
        provider_hint: str | None = None,
        timeout: float | None = None,
        max_retries: int = 2,
        default_params: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            provider_id=provider_id,
            model=model,
            api_key=api_key,
            base_url=base_url or _COPILOT_BASE_URL,
            provider_hint=provider_hint,
            timeout=timeout or 120.0,
            max_retries=max_retries,
            default_params=default_params,
        )
        self._copilot_token: str | None = None
        self._copilot_token_expires_at: float = 0.0
        self._copilot_token_lock: asyncio.Lock | None = None
        # tracks which GitHub token produced the cached Copilot session,
        # so a credential rotation invalidates the cache.
        self._copilot_token_for_api_key: str | None = None

    def __setattr__(self, name: str, value: Any) -> None:
        # api_key may be hot-swapped post-init; drop the cached session
        # token to force a re-exchange with the new GitHub credential.
        if (
            name == "api_key"
            and getattr(self, "_copilot_token", None) is not None
            and value != getattr(self, "api_key", None)
        ):
            self._copilot_token = None
            self._copilot_token_expires_at = 0.0
            self._copilot_token_for_api_key = None
            logger.info(
                "github_copilot: api_key changed, dropped cached Copilot token "
                "(provider_id=%s)",
                getattr(self, "provider_id", "?"),
            )
        super().__setattr__(name, value)

    async def _get_copilot_token(self) -> str:
        if self._copilot_token_lock is None:
            self._copilot_token_lock = asyncio.Lock()
        async with self._copilot_token_lock:
            now = time.time()
            cached_for_same_key = (
                self._copilot_token_for_api_key == self.api_key
            )
            if (
                self._copilot_token
                and cached_for_same_key
                and now < self._copilot_token_expires_at - _REFRESH_BUFFER_SECONDS
            ):
                return self._copilot_token
            if not self.api_key:
                raise RuntimeError(
                    "github_copilot provider has no api_key set "
                    "(should be a GitHub OAuth token or PAT)"
                )
            logger.warning(
                "github_copilot: exchanging GitHub token for Copilot session "
                "token (provider_id=%s, gh_token_prefix=%s, gh_token_len=%d)",
                self.provider_id,
                (self.api_key[:6] + "…") if len(self.api_key) >= 6 else "?",
                len(self.api_key),
            )
            import httpx
            try:
                async with httpx.AsyncClient(timeout=30.0) as cl:
                    resp = await cl.get(
                        _GH_TOKEN_URL,
                        headers={
                            "Authorization": f"token {self.api_key}",
                            "Editor-Version": _EDITOR_VERSION,
                            "Editor-Plugin-Version": _EDITOR_PLUGIN_VERSION,
                            "User-Agent": _USER_AGENT,
                            "Accept": "application/json",
                        },
                    )
            except httpx.HTTPError as exc:
                logger.error(
                    "github_copilot: HTTP error reaching %s: %s: %s",
                    _GH_TOKEN_URL, type(exc).__name__, exc,
                )
                raise RuntimeError(
                    f"Cannot reach {_GH_TOKEN_URL} ({type(exc).__name__}: {exc})."
                    " Check network / proxy / DNS."
                ) from exc
            logger.info(
                "github_copilot: token exchange returned HTTP %s",
                resp.status_code,
            )
            if resp.status_code in (401, 403):
                raise RuntimeError(
                    f"GitHub rejected the token (HTTP {resp.status_code}). "
                    f"The PAT might be revoked, lack Copilot access, or be "
                    f"a fine-grained PAT without `Copilot Chat` permission. "
                    f"Detail: {resp.text[:200]}"
                )
            if resp.status_code >= 400:
                logger.error(
                    "github_copilot: token exchange returned %d body=%s",
                    resp.status_code, resp.text[:300],
                )
            resp.raise_for_status()
            data = resp.json()
            tok = str(data.get("token") or "")
            if not tok:
                raise RuntimeError(
                    f"Copilot token endpoint returned no token: {data}"
                )
            self._copilot_token = tok
            self._copilot_token_expires_at = float(
                data.get("expires_at") or (now + 25 * 60)
            )
            self._copilot_token_for_api_key = self.api_key
            logger.info(
                "github_copilot: refreshed Copilot session token "
                "(expires in %.0fs, provider_id=%s)",
                self._copilot_token_expires_at - now,
                self.provider_id,
            )
            return tok

    async def list_models(self) -> list[dict[str, Any]]:
        """Hit `GET /models` directly. Useful for the picker / probe."""
        import httpx
        token = await self._get_copilot_token()
        async with httpx.AsyncClient(timeout=30.0) as cl:
            resp = await cl.get(
                f"{self.base_url}/models",
                headers=self._copilot_request_headers(token),
            )
            resp.raise_for_status()
            data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        if isinstance(data, list):
            return data
        return []

    @staticmethod
    def _copilot_request_headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Editor-Version": _EDITOR_VERSION,
            "Editor-Plugin-Version": _EDITOR_PLUGIN_VERSION,
            "Copilot-Integration-Id": _COPILOT_INTEGRATION_ID,
            "User-Agent": _USER_AGENT,
            "Openai-Intent": "conversation-panel",
        }

    def _build_copilot_client(self) -> Any:
        import httpx
        import openai

        async def _inject_token(request: httpx.Request) -> None:
            try:
                tok = await self._get_copilot_token()
            except Exception as exc:
                # openai SDK wraps event-hook exceptions as a generic
                # APIConnectionError; log loud here so the real cause is visible.
                logger.error(
                    "github_copilot token-exchange failed in event hook: "
                    "%s: %s",
                    type(exc).__name__, exc,
                )
                raise
            request.headers["Authorization"] = f"Bearer {tok}"

        http_client = httpx.AsyncClient(
            timeout=self.timeout,
            headers={
                "Editor-Version": _EDITOR_VERSION,
                "Editor-Plugin-Version": _EDITOR_PLUGIN_VERSION,
                "Copilot-Integration-Id": _COPILOT_INTEGRATION_ID,
                "User-Agent": _USER_AGENT,
                "Openai-Intent": "conversation-panel",
            },
            event_hooks={"request": [_inject_token]},
        )
        client = openai.AsyncOpenAI(
            api_key=self._copilot_token or "placeholder",
            base_url=self.base_url,
            http_client=http_client,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )
        _ = client.chat
        _ = client.chat.completions
        return client

    async def initialize(self) -> None:
        """Build the openai client; token exchange is deferred to first request."""
        try:
            self._client = await asyncio.to_thread(self._build_copilot_client)
        except ImportError as exc:
            raise ImportError(
                "openai package required: pip install openai"
            ) from exc

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        self._client = self._build_copilot_client()
        return self._client

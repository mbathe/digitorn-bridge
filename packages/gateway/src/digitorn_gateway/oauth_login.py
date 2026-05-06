"""Server-side OAuth login flows for the gateway.

The dashboard surfaces a "Login with X" button for OAuth providers
(``github_copilot``, ``claude_code``, generic ``oauth2``). When the
user clicks, the dashboard POSTs to one of the ``/admin/oauth/...``
endpoints below; the gateway runs the OAuth flow against the
upstream issuer entirely server-side, polls until the user
authorizes, then persists the resulting tokens as a regular
``GatewayCredential`` row (encrypted blob, write-through cache).

Currently implemented:

* GitHub Copilot device flow (RFC 8628). VS Code's
  ``Iv1.b507a08c87ecfe98`` client_id is the only one whitelisted by
  GitHub's Copilot endpoint; using a custom client_id silently
  returns 404 forever. Mirrors the daemon's
  ``core.credentials.copilot_device_flow`` so the behaviour stays
  identical between the daemon and gateway sides.

* Generic OAuth2 device flow stub (caller supplies the device-code +
  token endpoints). Used for any provider that follows RFC 8628.

Anthropic / Claude Code: there is no public device-flow endpoint for
the client_id that grants access to ``console.anthropic.com/v1/oauth/token``,
so we offer a "paste tokens" / "import from ``~/.claude/.credentials.json``"
flow instead. That UX lives entirely in the dashboard - this module
exposes the persistence side via the existing /admin/credentials POST.

The flow store is in-memory: a daemon restart drops pending flows.
Acceptable trade-off for the user-clicks-link-then-types-code window
which is < 15 minutes by spec; the user just clicks again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── GitHub Copilot client config ──────────────────────────────────────


COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"
COPILOT_DEVICE_CODE_URL = "https://github.com/login/device/code"
COPILOT_TOKEN_URL = "https://github.com/login/oauth/access_token"

_COPILOT_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Editor-Version": "vscode/1.99.3",
    "Editor-Plugin-Version": "copilot-chat/0.27.0",
    "User-Agent": "GithubCopilot/1.270.0",
}


# ── Pending flow record ──────────────────────────────────────────────


@dataclass
class DeviceFlowRecord:
    """One in-progress OAuth device flow.

    Stored server-side keyed by ``state``. The frontend never sees
    ``device_code`` (the secret half of the flow); it only gets the
    public ``user_code`` + ``verification_uri`` + ``state``.
    """

    state: str
    provider_kind: str       # "copilot" | "oauth2"
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_at: float

    target_provider_slug: str
    target_label: str

    status: str = "pending"  # pending | connected | error | expired
    error: str | None = None
    credential_id: str | None = None
    # Endpoint for polling (varies per provider).
    token_endpoint: str = ""
    client_id: str = ""

    created_at: float = field(default_factory=time.time)

    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def public_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "user_code": self.user_code,
            "verification_uri": self.verification_uri,
            "expires_in": max(0, int(self.expires_at - time.time())),
            "interval": self.interval,
            "status": self.status,
            "error": self.error,
            "credential_id": self.credential_id,
        }


class DeviceFlowStore:
    """In-memory registry of pending device flows."""

    def __init__(self) -> None:
        self._flows: dict[str, DeviceFlowRecord] = {}
        self._lock = asyncio.Lock()
        self._client: Any = None

    def _new_state(self) -> str:
        return secrets.token_urlsafe(24)

    async def _http(self, headers: dict[str, str] | None = None) -> Any:
        # Keep one client per process to amortise TLS handshake cost
        # across the user's poll loop (every 5 s).
        if self._client is not None:
            return self._client
        import httpx
        self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def start_copilot(
        self,
        *,
        target_provider_slug: str = "github_copilot",
        target_label: str = "GitHub Copilot",
    ) -> DeviceFlowRecord:
        """Kick off GitHub Copilot's RFC 8628 device flow."""
        cl = await self._http()
        try:
            resp = await cl.post(
                COPILOT_DEVICE_CODE_URL,
                json={"client_id": COPILOT_CLIENT_ID, "scope": "read:user"},
                headers=_COPILOT_HEADERS,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Could not reach {COPILOT_DEVICE_CODE_URL}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"GitHub device-flow refused (HTTP {resp.status_code}): "
                f"{resp.text[:200]}"
            )
        data = resp.json()
        device_code = str(data.get("device_code") or "")
        user_code = str(data.get("user_code") or "")
        verify = str(data.get("verification_uri")
                     or "https://github.com/login/device")
        interval = int(data.get("interval") or 5)
        expires_in = int(data.get("expires_in") or 900)
        if not device_code or not user_code:
            raise RuntimeError(
                f"GitHub device-flow returned empty codes: {data}"
            )

        state = self._new_state()
        flow = DeviceFlowRecord(
            state=state,
            provider_kind="copilot",
            device_code=device_code,
            user_code=user_code,
            verification_uri=verify,
            interval=interval,
            expires_at=time.time() + expires_in,
            target_provider_slug=target_provider_slug,
            target_label=target_label,
            token_endpoint=COPILOT_TOKEN_URL,
            client_id=COPILOT_CLIENT_ID,
        )
        async with self._lock:
            self._sweep_expired_locked()
            self._flows[state] = flow
        logger.info(
            "oauth_login: copilot device flow started state=%s user_code=%s",
            state, user_code,
        )
        return flow

    async def start_oauth2(
        self,
        *,
        device_code_url: str,
        token_endpoint: str,
        client_id: str,
        scope: str,
        target_provider_slug: str,
        target_label: str,
    ) -> DeviceFlowRecord:
        """Generic RFC 8628 device flow for any compliant provider."""
        cl = await self._http()
        try:
            resp = await cl.post(
                device_code_url,
                data={"client_id": client_id, "scope": scope},
                headers={"Accept": "application/json"},
            )
        except Exception as exc:
            raise RuntimeError(
                f"Could not reach {device_code_url}: {type(exc).__name__}: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"Device-code endpoint refused (HTTP {resp.status_code}): "
                f"{resp.text[:200]}"
            )
        data = resp.json()
        state = self._new_state()
        flow = DeviceFlowRecord(
            state=state, provider_kind="oauth2",
            device_code=str(data["device_code"]),
            user_code=str(data["user_code"]),
            verification_uri=str(
                data.get("verification_uri")
                or data.get("verification_url") or ""
            ),
            interval=int(data.get("interval") or 5),
            expires_at=time.time() + int(data.get("expires_in") or 900),
            target_provider_slug=target_provider_slug,
            target_label=target_label,
            token_endpoint=token_endpoint, client_id=client_id,
        )
        async with self._lock:
            self._sweep_expired_locked()
            self._flows[state] = flow
        return flow

    async def get(self, state: str) -> DeviceFlowRecord | None:
        async with self._lock:
            f = self._flows.get(state)
            if f is None:
                return None
            if f.expired() and f.status == "pending":
                f.status = "expired"
                f.error = "Device code expired before user authorized"
            return f

    async def poll(
        self, state: str, *, on_success: Any = None,
    ) -> DeviceFlowRecord:
        """Poll the upstream once. On success, mutates the flow + calls
        ``on_success(flow, secret_data)`` to persist the credential."""
        flow = await self.get(state)
        if flow is None:
            raise KeyError(f"unknown device flow: {state}")
        if flow.status != "pending":
            return flow

        cl = await self._http()
        # Copilot uses JSON; generic oauth2 uses form-encoded - GitHub
        # actually accepts both for Copilot, so a JSON body works for
        # both kinds.
        try:
            resp = await cl.post(
                flow.token_endpoint,
                json={
                    "client_id": flow.client_id,
                    "device_code": flow.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    **(_COPILOT_HEADERS if flow.provider_kind == "copilot" else {}),
                },
            )
        except Exception as exc:
            logger.warning(
                "oauth_login: poll network error state=%s: %s", state, exc,
            )
            return flow
        try:
            data = resp.json()
        except json.JSONDecodeError:
            logger.warning(
                "oauth_login: poll non-json state=%s http=%d body=%r",
                state, resp.status_code, resp.text[:300],
            )
            return flow

        if "access_token" in data:
            access_token = str(data["access_token"])
            flow.status = "connected"

            # Build the secret_data shape per provider_kind.
            if flow.provider_kind == "copilot":
                secret = {
                    "oauth_token": access_token,
                    # api_key + expires_at filled by the first refresh.
                    "api_key": "",
                    "api_key_expires_at": "0",
                }
            else:
                secret = {
                    "access_token": access_token,
                    "refresh_token": str(data.get("refresh_token", "")),
                    "expires_at": str(
                        int(time.time()) + int(data.get("expires_in", 0))
                    ),
                    "token_endpoint": flow.token_endpoint,
                    "client_id": flow.client_id,
                }

            if on_success is not None:
                try:
                    cred_id = await on_success(flow, secret)
                    flow.credential_id = cred_id
                except Exception as exc:
                    logger.error(
                        "oauth_login: persist failed state=%s: %s",
                        state, exc, exc_info=True,
                    )
                    flow.status = "error"
                    flow.error = (
                        f"Auth succeeded but persisting the credential "
                        f"failed: {type(exc).__name__}: {exc}"
                    )
            logger.info(
                "oauth_login: completed state=%s credential_id=%s",
                state, flow.credential_id,
            )
            return flow

        err = str(data.get("error", "")) or ""
        if err == "authorization_pending":
            return flow
        if err == "slow_down":
            flow.interval = max(flow.interval, 10)
            return flow
        if err in ("expired_token", "expired_token_request"):
            flow.status = "expired"
            flow.error = "Device code expired before user authorized"
            return flow
        if err in ("access_denied", "incorrect_device_code",
                   "incorrect_client_credentials"):
            flow.status = "error"
            flow.error = (
                f"{err}: {data.get('error_description', '')}"
            )
            return flow
        logger.warning(
            "oauth_login: unknown poll response state=%s data=%s",
            state, data,
        )
        return flow

    async def forget(self, state: str) -> None:
        async with self._lock:
            self._flows.pop(state, None)

    def _sweep_expired_locked(self) -> None:
        now = time.time()
        cutoff = now - 3600
        stale = [
            s for s, f in self._flows.items()
            if f.created_at < cutoff or now > f.expires_at + 600
        ]
        for s in stale:
            self._flows.pop(s, None)


_default_store: DeviceFlowStore | None = None


def get_default_store() -> DeviceFlowStore:
    global _default_store
    if _default_store is None:
        _default_store = DeviceFlowStore()
    return _default_store

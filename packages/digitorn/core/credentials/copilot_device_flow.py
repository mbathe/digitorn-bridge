"""GitHub Copilot OAuth device-flow store."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# GitHub Copilot OAuth client config

COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"

_EDITOR_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Editor-Version": "vscode/1.99.3",
    "Editor-Plugin-Version": "copilot-chat/0.27.0",
    "User-Agent": "GithubCopilot/1.270.0",
}


# Pending flow record


@dataclass
class CopilotDeviceFlow:
    """One in-progress device-flow authorization."""

    state: str
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_at: float

    user_id: str
    target_name: str
    target_scope: str

    status: str = "pending"  # pending | connected | error | expired
    access_token: str | None = None
    credential_id: str | None = None
    error: str | None = None

    created_at: float = field(default_factory=time.time)

    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def public_dict(self) -> dict[str, Any]:
        """The shape returned to the client (no device_code)."""
        return {
            "state": self.state,
            "user_code": self.user_code,
            "verification_uri": self.verification_uri,
            "expires_in": max(0, int(self.expires_at - time.time())),
            "interval": self.interval,
            "status": self.status,
        }


# Store


class CopilotDeviceFlowStore:
    """In-memory registry of pending device flows."""

    def __init__(self) -> None:
        self._flows: dict[str, CopilotDeviceFlow] = {}
        self._lock = asyncio.Lock()
        # Reuse one client across requests - reduces TLS handshake
        # cost when the user polls every 5 s.
        self._client: Any = None

    def _new_state(self) -> str:
        return secrets.token_urlsafe(24)

    async def _http(self) -> Any:
        if self._client is not None:
            return self._client
        import httpx
        self._client = httpx.AsyncClient(
            timeout=20.0, headers=_EDITOR_HEADERS,
        )
        return self._client

    async def start(
        self, *, user_id: str, target_name: str, target_scope: str = "per_user",
    ) -> CopilotDeviceFlow:
        """Kick off a new device flow for `user_id`."""
        cl = await self._http()
        try:
            resp = await cl.post(
                DEVICE_CODE_URL,
                json={"client_id": COPILOT_CLIENT_ID, "scope": "read:user"},
            )
        except Exception as exc:
            raise RuntimeError(
                f"Could not reach {DEVICE_CODE_URL}: {type(exc).__name__}: {exc}"
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
        flow = CopilotDeviceFlow(
            state=state,
            device_code=device_code,
            user_code=user_code,
            verification_uri=verify,
            interval=interval,
            expires_at=time.time() + expires_in,
            user_id=user_id,
            target_name=target_name or "github_copilot",
            target_scope=target_scope or "per_user",
        )
        async with self._lock:
            self._sweep_expired_locked()
            self._flows[state] = flow
        logger.info(
            "copilot_device_flow_started state=%s user_id=%s user_code=%s",
            state, user_id, user_code,
        )
        return flow

    async def get(self, state: str) -> CopilotDeviceFlow | None:
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
    ) -> CopilotDeviceFlow:
        """Hit GitHub once for the given flow."""
        flow = await self.get(state)
        if flow is None:
            raise KeyError(f"unknown copilot device flow: {state}")
        if flow.status != "pending":
            return flow
        cl = await self._http()
        try:
            resp = await cl.post(
                TOKEN_URL,
                json={
                    "client_id": COPILOT_CLIENT_ID,
                    "device_code": flow.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
        except Exception as exc:
            logger.warning(
                "copilot_device_flow_poll_network state=%s: %s",
                state, exc,
            )
            return flow  # transient: leave pending, client will retry
        try:
            data = resp.json()
        except json.JSONDecodeError:
            logger.warning(
                "copilot_device_flow_poll_invalid_json state=%s "
                "http=%d body=%r",
                state, resp.status_code, resp.text[:300],
            )
            return flow

        _has_token = "access_token" in data
        _err = data.get("error", "")
        logger.info(
            "copilot_device_flow_poll http=%d state=%s has_token=%s error=%s "
            "keys=%s",
            resp.status_code, state, _has_token, _err or "(none)",
            list(data.keys()),
        )

        if "access_token" in data:
            access_token = str(data["access_token"])
            flow.access_token = access_token
            flow.status = "connected"
            if on_success is not None:
                try:
                    cred_id = await on_success(flow, access_token)
                    flow.credential_id = cred_id
                except Exception as exc:
                    logger.error(
                        "copilot_device_flow_persist_failed state=%s: %s",
                        state, exc, exc_info=True,
                    )
                    flow.status = "error"
                    flow.error = (
                        f"Auth succeeded but persisting the credential "
                        f"failed: {type(exc).__name__}: {exc}"
                    )
            logger.info(
                "copilot_device_flow_completed state=%s credential_id=%s",
                state, flow.credential_id,
            )
            return flow

        err = str(data.get("error", "")) or ""
        if err == "authorization_pending":
            return flow  # user hasn't typed the code yet
        if err == "slow_down":
            flow.interval = max(flow.interval, 10)
            return flow
        if err in ("expired_token", "expired_token_request"):
            flow.status = "expired"
            flow.error = "Device code expired before user authorized"
            return flow
        if err in ("access_denied", "incorrect_device_code", "incorrect_client_credentials"):
            flow.status = "error"
            flow.error = f"{err}: {data.get('error_description', '')}"
            return flow
        # Unknown response - log and stay pending.
        logger.warning(
            "copilot_device_flow_unknown_resp state=%s data=%s",
            state, data,
        )
        return flow

    async def forget(self, state: str) -> None:
        async with self._lock:
            self._flows.pop(state, None)

    def _sweep_expired_locked(self) -> None:
        now = time.time()
        cutoff = now - 3600
        stale = [s for s, f in self._flows.items()
                 if f.created_at < cutoff or now > f.expires_at + 600]
        for s in stale:
            self._flows.pop(s, None)
        if stale:
            logger.debug("copilot_device_flow_swept count=%d", len(stale))


# Module-level singleton


_default_store: CopilotDeviceFlowStore | None = None


def get_default_store() -> CopilotDeviceFlowStore:
    global _default_store
    if _default_store is None:
        _default_store = CopilotDeviceFlowStore()
    return _default_store

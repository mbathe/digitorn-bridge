"""JSON-lines IPC protocol for daemon to sandbox worker communication."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


@dataclass
class IPCRequest:
    id: int
    method: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class IPCResponse:
    id: int
    success: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    event: str = ""


def encode_request(req: IPCRequest) -> bytes:
    msg = {"id": req.id, "method": req.method, **req.payload}
    return json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"


def encode_response(resp: IPCResponse) -> bytes:
    msg: dict[str, Any] = {"id": resp.id}
    if resp.event:
        msg["event"] = resp.event
        msg["data"] = resp.data
    else:
        msg["success"] = resp.success
        if resp.error:
            msg["error"] = resp.error
        if resp.data:
            msg["data"] = resp.data
    return json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"


def decode_request(line: bytes) -> IPCRequest:
    obj = json.loads(line)
    return IPCRequest(
        id=obj.get("id", 0),
        method=obj.get("method", ""),
        payload={k: v for k, v in obj.items() if k not in ("id", "method")},
    )


def decode_response(line: bytes) -> IPCResponse:
    obj = json.loads(line)
    return IPCResponse(
        id=obj.get("id", 0),
        success=obj.get("success", True),
        data=obj.get("data", {}),
        error=obj.get("error", ""),
        event=obj.get("event", ""),
    )


_next_helper_id = 1000


def make_sandbox_request(
    workspace: str,
    session_id: str,
    namespaces: set[str] | None = None,
    hardening: dict[str, Any] | None = None,
    audit: bool = False,
) -> IPCRequest:
    """Build an IPCRequest that tells a warm worker to apply its sandbox."""
    global _next_helper_id
    _next_helper_id += 1
    return IPCRequest(
        id=_next_helper_id,
        method="sandbox",
        payload={
            "workspace": workspace,
            "session_id": session_id,
            "namespaces": sorted(namespaces) if namespaces else [],
            "hardening": hardening or {},
            "audit": audit,
        },
    )


def make_health_request() -> IPCRequest:
    """Build an IPCRequest that queries worker health stats."""
    global _next_helper_id
    _next_helper_id += 1
    return IPCRequest(id=_next_helper_id, method="health")


async def read_responses(
    reader: asyncio.StreamReader,
) -> AsyncIterator[IPCResponse]:
    """Yield responses from the worker subprocess stdout."""
    while True:
        line = await reader.readline()
        if not line:
            return
        try:
            yield decode_response(line)
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("ipc_decode_error: %s - line=%r", exc, line[:200])

"""Dataclasses for the session store."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso() -> str:
    return utc_now().isoformat()


def _bytes_estimate(value: Any) -> int:
    """Rough sizeof for cache-pressure tracking. Not exact (Python"""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.encode("utf-8")) + 49
    if isinstance(value, (int, float)):
        return 28
    if isinstance(value, bool):
        return 28
    if isinstance(value, list):
        return sum(_bytes_estimate(v) for v in value) + 56
    if isinstance(value, dict):
        return sum(
            _bytes_estimate(k) + _bytes_estimate(v)
            for k, v in value.items()
        ) + 232
    return sys.getsizeof(value)


@dataclass
class Event:
    """One row in the durable session journal."""

    type: str
    seq: int = 0
    ts: str = field(default_factory=utc_iso)

    kind: str = "event"
    app_id: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    actor_user_id: str | None = None
    actor_roles: list[str] = field(default_factory=list)

    role: str | None = None
    content: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    name: str | None = None

    payload: dict[str, Any] = field(default_factory=dict)

    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    target_user_id: str | None = None
    target_app_id: str | None = None
    target_resource: str | None = None

    ip_address: str | None = None
    user_agent: str | None = None
    correlation_id: str = ""

    success: bool = True
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        return cls(**d)

    def size_bytes(self) -> int:
        return _bytes_estimate(self.to_dict())


@dataclass
class BlobRef:
    """Reference to a content-addressed blob (image, audio, video,"""

    hash: str
    mime: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {"hash": self.hash, "mime": self.mime, "size": self.size}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BlobRef":
        return cls(hash=d["hash"], mime=d["mime"], size=int(d["size"]))


@dataclass
class ParentLink:
    """Backlink from a sub-agent session to its parent. Lets the UI"""

    parent_session_id: str
    parent_seq_at_spawn: int
    child_kind: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ParentLink":
        return cls(**d)


@dataclass
class ChildAgentRef:
    """Reference to a sub-agent spawned by this session. Carries the"""

    run_id: str
    kind: str
    spawned_at: str
    completed_at: str | None = None
    result_summary: str | None = None
    status: str = "running"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChildAgentRef":
        return cls(**d)


@dataclass
class Message:
    """One assistant or user turn, post-streaming-assembly. The"""

    role: str
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    seq: int = 0
    ts: str = field(default_factory=utc_iso)
    attachments: list[BlobRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": self.tool_calls,
            "seq": self.seq,
            "ts": self.ts,
            "attachments": [a.to_dict() for a in self.attachments],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        return cls(
            role=d["role"],
            content=d.get("content", ""),
            tool_calls=d.get("tool_calls", []),
            seq=d.get("seq", 0),
            ts=d.get("ts", utc_iso()),
            attachments=[BlobRef.from_dict(a) for a in d.get("attachments", [])],
        )


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    started_at: str = field(default_factory=utc_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolCall":
        return cls(**d)


@dataclass
class ToolResult:
    tool_call_id: str
    output: Any
    success: bool = True
    error: str | None = None
    completed_at: str = field(default_factory=utc_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolResult":
        return cls(**d)


@dataclass
class Todo:
    id: str
    text: str
    status: str = "pending"
    created_at: str = field(default_factory=utc_iso)
    updated_at: str = field(default_factory=utc_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Todo":
        return cls(**d)


@dataclass
class FileState:
    """Workspace file state captured by workspace.write/edit events."""

    path: str
    content_hash: str
    baseline_hash: str | None = None
    status: str = "approved"
    bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FileState":
        return cls(**d)


@dataclass
class ApprovalRequest:
    id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_iso)
    status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ApprovalRequest":
        return cls(**d)

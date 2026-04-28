"""Session-level usage tracking and tool result formatting.

Keeps agent_loop.py lean - all accumulation logic lives here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Token / usage tracking ──────────────────────────────────────


@dataclass
class SessionUsage:
    """Cumulative token usage across turns within a session."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    turns: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def record_response(self, response: Any) -> None:
        """Extract usage from a provider response and accumulate."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        pt = getattr(usage, "prompt_tokens", 0) or 0
        ct = getattr(usage, "completion_tokens", 0) or 0
        self.prompt_tokens += pt
        self.completion_tokens += ct

    def record_turn(self) -> None:
        self.turns += 1

    def snapshot(self) -> dict[str, int]:
        """Return a JSON-safe dict for IPC / API."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "turns": self.turns,
        }


# ── Image tool result formatting ────────────────────────────────


def format_image_tool_result(result_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert an image ActionResult into a multimodal content block.

    If the tool result contains ``is_image=True``, returns a list of content
    blocks suitable for the OpenAI/Anthropic messages format::

        [
            {"type": "text", "text": "Image: path/to/file.png (12345 bytes)"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
        ]

    If the result is NOT an image, returns None (caller uses default formatting).
    """
    if not isinstance(result_data, dict) or not result_data.get("is_image"):
        return None  # type: ignore[return-value]

    mime = result_data.get("mime_type", "image/png")
    path = result_data.get("path", "unknown")
    size = result_data.get("size_bytes", 0)
    w = result_data.get("width", 0)
    h = result_data.get("height", 0)

    # Base64 can be in data.content (old) or metadata.image_data (new)
    b64 = result_data.get("content", "")

    if not b64:
        # No inline base64 - return text-only description
        # The actual image is in metadata and will be sent to the client via SSE
        dim = f" {w}x{h}" if w and h else ""
        return [
            {"type": "text", "text": f"Image read: {path} ({size:,} bytes{dim}, {mime})"},
        ]

    return [
        {"type": "text", "text": f"Image: {path} ({size:,} bytes, {mime})"},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]

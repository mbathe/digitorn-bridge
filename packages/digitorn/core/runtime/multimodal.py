"""Multimodal message handling — image content blocks for vision models.

Handles the conversion between internal image references and the
provider-specific formats (Anthropic vs OpenAI vs text-only fallback).

Also implements Image Aging: recent images are sent at full resolution,
older images are resized, and very old images become text descriptions.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_user_message_with_images(
    text: str,
    image_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a user message with text + image references.

    Args:
        text: The user's text message.
        image_refs: List of image ref dicts from ImageStore.

    Returns:
        A message dict with multimodal content blocks.
    """
    if not image_refs:
        return {"role": "user", "content": text}

    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})

    for ref in image_refs:
        content.append({
            "type": "image_ref",
            "image_id": ref.get("image_id", ""),
            "mime": ref.get("mime", "image/png"),
            "alt_text": ref.get("alt_text", ""),
            "width": ref.get("width", 0),
            "height": ref.get("height", 0),
            "turn": ref.get("turn", 0),
        })

    return {"role": "user", "content": content}


def has_images(message: dict[str, Any]) -> bool:
    """Check if a message contains image content blocks."""
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        b.get("type") in ("image", "image_ref", "image_url")
        for b in content
        if isinstance(b, dict)
    )


async def resolve_images_for_provider(
    messages: list[dict[str, Any]],
    provider_format: str,
    image_store: Any,
    session_id: str,
    current_turn: int = 0,
    supports_vision: bool = True,
    image_detail: str = "auto",
    aging_full_turns: int = 1,
    aging_low_turns: int = 2,
    low_res_size: int = 512,
) -> list[dict[str, Any]]:
    """Resolve image references to provider-specific format.

    Applies Image Aging:
    - Current turn: full resolution base64
    - Recent turns (aging_full_turns): full resolution
    - Older turns (aging_low_turns): low resolution (resized)
    - Very old: text description only

    Args:
        messages: The message list with image_ref blocks.
        provider_format: "anthropic", "openai", or "text"
        image_store: The ImageStore instance.
        session_id: Current session ID.
        current_turn: Current turn number.
        supports_vision: Whether the model supports vision.
        image_detail: "auto", "low", or "high".
        aging_full_turns: Turns at full resolution.
        aging_low_turns: Turns at low resolution.
        low_res_size: Resize dimension for low-res images.

    Returns:
        New message list with resolved image content.
    """
    result = []

    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue

        new_content = []
        for block in content:
            if not isinstance(block, dict):
                new_content.append(block)
                continue

            btype = block.get("type", "")

            if btype == "text":
                new_content.append(block)

            elif btype == "image_ref":
                if not supports_vision:
                    # No vision — convert to text
                    alt = block.get("alt_text", "image")
                    w = block.get("width", 0)
                    h = block.get("height", 0)
                    dim = f" ({w}x{h})" if w and h else ""
                    new_content.append({
                        "type": "text",
                        "text": f"[Image: {alt}{dim}]",
                    })
                    continue

                image_id = block.get("image_id", "")
                mime = block.get("mime", "image/png")
                age = current_turn - block.get("turn", 0)

                # Image Aging strategy
                if age <= aging_full_turns:
                    # Full resolution
                    b64 = await image_store.get_base64(image_id, session_id)
                elif age <= aging_full_turns + aging_low_turns:
                    # Low resolution
                    b64 = await image_store.get_base64_resized(
                        image_id, session_id, max_size=low_res_size,
                    )
                else:
                    # Text only
                    alt = block.get("alt_text", "image")
                    new_content.append({
                        "type": "text",
                        "text": f"[Previous image: {alt}]",
                    })
                    continue

                if b64 is None:
                    # Image not found — fallback to text
                    alt = block.get("alt_text", "image")
                    new_content.append({
                        "type": "text",
                        "text": f"[Image unavailable: {alt}]",
                    })
                    continue

                # Convert to provider format
                if provider_format == "anthropic":
                    new_content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": b64,
                        },
                    })
                elif provider_format == "openai":
                    detail = "low" if age > aging_full_turns else (
                        image_detail if image_detail != "auto" else "high"
                    )
                    new_content.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{b64}",
                            "detail": detail,
                        },
                    })
                else:
                    # Text fallback
                    alt = block.get("alt_text", "image")
                    new_content.append({
                        "type": "text",
                        "text": f"[Image: {alt}]",
                    })

            elif btype == "image":
                # Already resolved (inline base64) — pass through or convert
                if not supports_vision:
                    new_content.append({
                        "type": "text",
                        "text": "[Image: inline image]",
                    })
                elif provider_format == "openai":
                    source = block.get("source", {})
                    mime = source.get("media_type", "image/png")
                    data = source.get("data", "")
                    new_content.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{data}",
                            "detail": image_detail if image_detail != "auto" else "high",
                        },
                    })
                else:
                    new_content.append(block)

            else:
                new_content.append(block)

        # If all content is text, simplify to string
        if len(new_content) == 1 and new_content[0].get("type") == "text":
            result.append({**msg, "content": new_content[0]["text"]})
        elif new_content:
            result.append({**msg, "content": new_content})
        else:
            result.append(msg)

    return result


def inject_tool_image(
    messages: list[dict[str, Any]],
    image_data: str,
    media_type: str,
    tool_name: str,
    alt_text: str = "",
) -> None:
    """Inject a tool-generated image into the messages for the LLM to see.

    Called when a tool result contains an image (e.g. browser.screenshot,
    filesystem.read on an image file).
    """
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": f"[Tool output image from {tool_name}]"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_data,
                },
            },
        ],
    })

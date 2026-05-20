"""One-shot mode - message → agent loop → result → exit."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any

from digitorn.core.runtime.agent_loop import agent_turn
from digitorn.core.runtime.types import AgentContext, TurnResult

logger = logging.getLogger(__name__)


async def run_one_shot(
    ctx: AgentContext,
    *,
    user_input: str = "",
    input_type: str = "text",
    output_type: str = "text",
    max_turns: int = 50,
    timeout: float = 300.0,
    on_tool_call: Any | None = None,
    on_tool_start: Any | None = None,
    on_thinking: Any | None = None,
    hook_runner: Any | None = None,
    on_token: Any | None = None,
    on_stream_done: Any | None = None,
) -> TurnResult:
    """Run the agent in one-shot mode."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": ctx.system_prompt},
    ]

    user_message = _build_user_message(user_input, input_type)
    messages.append(user_message)

    result = await agent_turn(
        ctx, messages,
        max_turns=max_turns,
        timeout=timeout,
        on_tool_call=on_tool_call,
        on_tool_start=on_tool_start,
        on_thinking=on_thinking,
        hook_runner=hook_runner,
        on_token=on_token,
        on_stream_done=on_stream_done,
    )

    if output_type == "json" and result.content and not result.error:
        result = _extract_json_output(result)

    return result


def _build_user_message(raw_input: str, input_type: str) -> dict[str, Any]:
    if input_type == "text" or input_type == "any":
        return {"role": "user", "content": raw_input}

    if input_type == "image":
        return _build_image_message(raw_input)

    if input_type == "file":
        return _build_file_message(raw_input)

    if input_type == "json":
        return _build_json_message(raw_input)

    return {"role": "user", "content": raw_input}


def _build_image_message(image_input: str) -> dict[str, Any]:
    path = Path(image_input)
    if path.exists():
        mime_type = mimetypes.guess_type(str(path))[0] or "image/png"
        data = base64.b64encode(path.read_bytes()).decode()
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": f"Image provided: {path.name}"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{data}"},
                },
            ],
        }
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "Image provided:"},
            {"type": "image_url", "image_url": {"url": image_input}},
        ],
    }


def _build_file_message(file_input: str) -> dict[str, Any]:
    path = Path(file_input)
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8")
            return {
                "role": "user",
                "content": f"File: {path.name}\n\n{content}",
            }
        except UnicodeDecodeError:
            data = base64.b64encode(path.read_bytes()).decode()
            return {
                "role": "user",
                "content": f"File: {path.name} (binary, base64 encoded)\n\n{data}",
            }
    return {"role": "user", "content": f"File path: {file_input}"}


def _build_json_message(json_input: str) -> dict[str, Any]:
    path = Path(json_input)
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8")
            data = json.loads(content)
            formatted = json.dumps(data, indent=2, ensure_ascii=False)
            return {
                "role": "user",
                "content": f"Input JSON:\n```json\n{formatted}\n```",
            }
        except Exception:
            pass  # Best-effort JSON formatting - fall through to raw input

    try:
        data = json.loads(json_input)
        formatted = json.dumps(data, indent=2, ensure_ascii=False)
        return {
            "role": "user",
            "content": f"Input JSON:\n```json\n{formatted}\n```",
        }
    except (json.JSONDecodeError, ValueError):
        return {"role": "user", "content": json_input}


def _extract_json_output(result: TurnResult) -> TurnResult:
    content = result.content

    if "```json" in content:
        start = content.index("```json") + 7
        end = content.index("```", start)
        json_str = content[start:end].strip()
        try:
            parsed = json.loads(json_str)
            return TurnResult(
                content=json.dumps(parsed, indent=2, ensure_ascii=False),
                tool_calls_count=result.tool_calls_count,
                turns_used=result.turns_used,
            )
        except (json.JSONDecodeError, ValueError):
            pass

    stripped = content.strip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(stripped)
            return TurnResult(
                content=json.dumps(parsed, indent=2, ensure_ascii=False),
                tool_calls_count=result.tool_calls_count,
                turns_used=result.turns_used,
            )
        except (json.JSONDecodeError, ValueError):
            pass

    return result

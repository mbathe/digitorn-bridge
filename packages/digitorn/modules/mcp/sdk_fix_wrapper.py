#!/usr/bin/env python3
"""MCP server wrapper that fixes the pre_parse_json bug in the MCP SDK.

Bug: The SDK's ``pre_parse_json`` converts JSON string arguments to dicts
when the field annotation is ``Optional[str]`` (because ``Optional[str] is
not str``).  This breaks MCP servers (like mcp-notion) that declare
``Optional[str]`` parameters for JSON string inputs.

Fix: Patch ``pre_parse_json`` to check if ``str`` is one of the annotation's
accepted types before attempting to parse.  If the field accepts ``str``,
leave the value as-is.

Usage:
    python3 -m digitorn.modules.mcp.sdk_fix_wrapper <original_command> [args...]
"""

from __future__ import annotations

import json
import sys
import typing
from typing import Annotated, Any, Union


def _annotation_accepts_str(annotation: Any) -> bool:
    """Check if a type annotation accepts str values.

    Handles: str, Optional[str], str | None, Union[str, ...],
    Annotated[str, ...], Annotated[Optional[str], ...], and nested
    combinations thereof.
    """
    if annotation is str:
        return True

    origin = typing.get_origin(annotation)

    if origin is Annotated:
        args = typing.get_args(annotation)
        if args:
            return _annotation_accepts_str(args[0])
        return False

    if origin is Union or origin is type(str | None):  # type: ignore[comparison-overlap]
        return any(_annotation_accepts_str(a) for a in typing.get_args(annotation))

    return False


def _patched_pre_parse_json(self: Any, data: dict[str, Any]) -> dict[str, Any]:
    """Fixed version of FuncMetadata.pre_parse_json.

    Same logic as original, but skips fields whose annotation accepts str.
    """
    new_data = data.copy()

    key_to_field_info: dict[str, Any] = {}
    for field_name, field_info in self.arg_model.model_fields.items():
        key_to_field_info[field_name] = field_info
        if field_info.alias:
            key_to_field_info[field_info.alias] = field_info

    for data_key, data_value in data.items():
        if data_key not in key_to_field_info:
            continue

        field_info = key_to_field_info[data_key]

        if _annotation_accepts_str(field_info.annotation):
            continue

        if isinstance(data_value, str) and field_info.annotation is not str:
            try:
                pre_parsed = json.loads(data_value)
            except json.JSONDecodeError:
                continue
            if isinstance(pre_parsed, str | int | float):
                continue
            new_data[data_key] = pre_parsed

    assert new_data.keys() == data.keys()
    return new_data


def _apply_patch() -> None:
    """Monkey-patch FuncMetadata.pre_parse_json."""
    from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata

    FuncMetadata.pre_parse_json = _patched_pre_parse_json


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: python3 -m digitorn.modules.mcp.sdk_fix_wrapper <command> [args...]",
            file=sys.stderr,
        )
        sys.exit(1)

    _apply_patch()

    command = sys.argv[1]
    sys.argv = sys.argv[1:]

    import os
    import shutil

    if os.path.isabs(command) or os.sep in command:
        script_path = command if os.path.isfile(command) else None
    else:
        script_path = shutil.which(command)

    if not script_path:
        print(f"Command not found: {command}", file=sys.stderr)
        sys.exit(1)

    import runpy

    sys.argv[0] = script_path
    runpy.run_path(script_path, run_name="__main__")


if __name__ == "__main__":
    main()

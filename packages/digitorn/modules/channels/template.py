"""Safe template renderer for channel messages."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Maximum rendered output length (256 KB)
MAX_OUTPUT_LENGTH = 262_144

# Regex for {{var.path.to.field}} - non-greedy, single-pass
_VAR_PATTERN = re.compile(r"\{\{\s*(.+?)\s*\}\}")

# Blocked prefixes - these must be resolved at compile time, not runtime
_BLOCKED_PREFIXES = ("secret.", "env.")

def render(template: str, variables: dict[str, Any]) -> str:
    """Render a template string with safe variable substitution."""
    if not template:
        return ""

    def _replace(match: re.Match[str]) -> str:
        dotpath = match.group(1).strip()

        # Block runtime secret/env access
        for prefix in _BLOCKED_PREFIXES:
            if dotpath.startswith(prefix):
                logger.warning(
                    "channel_template_blocked_ref ref=%s (must be resolved at compile time)",
                    dotpath,
                )
                return match.group(0)  # Leave placeholder as-is

        value = resolve_dotpath(dotpath, variables)
        if value is None:
            return match.group(0)  # Leave unresolved placeholders as-is

        return _format_value(value)

    # Single pass - no recursive expansion
    result = _VAR_PATTERN.sub(_replace, template)

    # Enforce max length
    if len(result) > MAX_OUTPUT_LENGTH:
        result = result[:MAX_OUTPUT_LENGTH] + "... [truncated]"

    return result

def resolve_dotpath(dotpath: str, data: dict[str, Any]) -> Any:
    """Resolve a dot-separated path into a nested dict."""
    parts = dotpath.split(".")
    current: Any = data

    for part in parts:
        if isinstance(current, dict):
            if part in current:
                current = current[part]
            else:
                return None
        elif isinstance(current, (list, tuple)):
            if part.isdigit():
                idx = int(part)
                if 0 <= idx < len(current):
                    current = current[idx]
                else:
                    return None
            else:
                return None
        else:
            return None

    return current

def _format_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)

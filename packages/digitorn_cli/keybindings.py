"""Custom keybindings loader.

Reads ~/.digitorn/keybindings.json if it exists:
{
    "submit": "enter",
    "newline": "shift+enter",
    "search": "ctrl+f",
    "sidebar": "ctrl+b",
    "bookmark": "ctrl+d",
    "quit": "ctrl+c",
    "undo": "ctrl+z",
    "clear": "ctrl+l",
    "help": "f1"
}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DEFAULTS = {
    "submit": "enter",
    "newline": "shift+enter",
    "search": "ctrl+f",
    "sidebar": "ctrl+b",
    "bookmark": "ctrl+d",
    "next_bookmark": "f2",
    "quit": "ctrl+c",
    "undo": "ctrl+z",
    "clear": "ctrl+l",
    "help": "f1",
}

_CONFIG_PATH = Path.home() / ".digitorn" / "keybindings.json"


def load_keybindings() -> dict[str, str]:
    """Load keybindings from config file, with defaults."""
    bindings = dict(_DEFAULTS)
    if _CONFIG_PATH.exists():
        try:
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                bindings.update(data)
        except Exception:
            pass
    return bindings


def save_default_keybindings() -> None:
    """Write default keybindings file if it doesn't exist."""
    if not _CONFIG_PATH.exists():
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(
            json.dumps(_DEFAULTS, indent=2), encoding="utf-8"
        )

"""Workdir slug detection, validation and resolution."""

from __future__ import annotations

import re
from pathlib import Path


# start AND end alphanum, 1-50 chars
SLUG_REGEX = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$")
SLUG_MAX_LEN = 50


def is_slug_shape(value: str) -> bool:
    """True when value lacks path/drive markers."""
    if not value:
        return False
    if "/" in value or "\\" in value:
        return False
    if value.startswith("~"):
        return False
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return False
    return True


def validate_slug(value: str) -> None:
    """Raise ValueError when the value isn't a legal slug."""
    if not isinstance(value, str):
        raise ValueError("slug must be a string")
    if not SLUG_REGEX.match(value):
        raise ValueError(
            "Invalid project slug. Use 1-50 lowercase letters, digits "
            "and dashes only, starting with a letter or digit."
        )


def _workdirs_root() -> Path:
    return Path.home() / ".digitorn" / "workdirs"


def resolve_workdir(app_id: str, user_id: str, value: str) -> Path:
    """Translate a client-supplied workdir value into an absolute path."""
    if is_slug_shape(value):
        validate_slug(value)
        return _workdirs_root() / app_id / user_id / value
    return Path(value).expanduser().resolve()


def is_named_project_path(p: Path) -> bool:
    """True iff p sits under the daemon-managed slug workdirs namespace."""
    try:
        p.resolve().relative_to(_workdirs_root().resolve())
        return True
    except ValueError:
        return False


def list_user_projects(app_id: str, user_id: str) -> list[str]:
    """List slug names for a user's projects under a given app."""
    root = _workdirs_root() / app_id / user_id
    if not root.exists() or not root.is_dir():
        return []
    out: list[str] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and SLUG_REGEX.match(entry.name):
            out.append(entry.name)
    return out

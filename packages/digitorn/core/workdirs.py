"""Workdir slug detection, validation and resolution.

The session-create endpoint accepts a free-form ``workdir`` value from
the client. Two shapes coexist:

- **Filesystem path** (Flutter desktop / CLI): an absolute or
  ``~``-prefixed path the user picked from their machine. The daemon
  expands it via ``Path.expanduser().resolve()`` and uses it as-is.

- **Project slug** (web SPA): a short identifier the user typed in
  the project picker. The daemon resolves it to a standard location
  under ``~/.digitorn/workdirs/{app_id}/{user_id}/{slug}/``, creating
  the directory on first use. Multiple sessions binding to the same
  slug share the dir — agents collaborate on the same files across
  sessions.

The two shapes are distinguished purely by inspection of the value
itself (presence of path separators / ``~`` / Windows drive letter),
without consulting the ``X-Digitorn-Client`` header. That keeps the
contract single-source-of-truth: a client could in principle send
either shape, and the daemon does the same thing in either case.

Mirrors the design discussed with the user (2026-05-16):
    ~/.digitorn/workspaces/{app_id}/{session_id}/          # daemon-private (state.json, baselines)
    ~/.digitorn/workdirs/{app_id}/{user_id}/{slug}/        # agent workdir (when value is a slug)

Slug format is strict: ``^[a-z0-9][a-z0-9-]{0,49}$`` (lowercase
alphanumerics + dash, must start with alphanum, 1-50 chars). Client
should validate before sending; daemon re-validates as defense in
depth and returns a structured 400 on violation.
"""

from __future__ import annotations

import re
from pathlib import Path


# ── Slug grammar ─────────────────────────────────────────────────────


# Must start AND end with alphanum (no leading/trailing dash). Single
# char allowed (matches just the alphanum). 2-50 chars allowed with
# any mix of alphanum + dash in the middle, but a final alphanum is
# always required.
SLUG_REGEX = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$")
SLUG_MAX_LEN = 50


def is_slug_shape(value: str) -> bool:
    """Heuristic: does the value look like a slug rather than a path?

    Returns True when the value has none of the markers that a
    filesystem path would have: forward / backward slash, leading
    tilde, or a Windows drive letter prefix (``C:``).
    """
    if not value:
        return False
    if "/" in value or "\\" in value:
        return False
    if value.startswith("~"):
        return False
    # Windows drive letter: ``X:`` or ``X:\`` etc. Only check when the
    # value is at least 2 chars to avoid false positives on lone ":".
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return False
    return True


def validate_slug(value: str) -> None:
    """Raise ValueError when the value isn't a legal slug.

    Called after ``is_slug_shape`` confirms the value is name-shaped.
    The combined check enforces: not a path AND matches the strict
    grammar. Any caller that wants a 400-formatted response should
    catch this and translate.
    """
    if not isinstance(value, str):
        raise ValueError("slug must be a string")
    if not SLUG_REGEX.match(value):
        raise ValueError(
            "Invalid project slug. Use 1-50 lowercase letters, digits "
            "and dashes only, starting with a letter or digit."
        )


# ── Path resolution ──────────────────────────────────────────────────


def _workdirs_root() -> Path:
    """Root for all slug-based workdirs. Sits next to the existing
    ``~/.digitorn/workspaces/`` namespace so the two layouts don't
    overlap and ``ls ~/.digitorn`` reads cleanly.
    """
    return Path.home() / ".digitorn" / "workdirs"


def resolve_workdir(app_id: str, user_id: str, value: str) -> Path:
    """Translate a client-supplied workdir value into an absolute path.

    Behavior:
      - ``value`` is path-shaped → ``Path(value).expanduser().resolve()``
        (legacy behavior; Flutter desktop, CLI).
      - ``value`` is slug-shaped → validate strictly, then resolve to
        ``~/.digitorn/workdirs/{app_id}/{user_id}/{slug}/``.

    The directory is **not** created here — that's the caller's job
    (matching the existing session-create flow which mkdirs both the
    daemon workspace and the user workdir explicitly). This keeps the
    helper pure (no side effects, safe to call from path discovery
    code without mutating disk).
    """
    if is_slug_shape(value):
        validate_slug(value)
        return _workdirs_root() / app_id / user_id / value
    # Path branch.
    return Path(value).expanduser().resolve()


def is_named_project_path(p: Path) -> bool:
    """True iff ``p`` sits under the daemon-managed slug workdirs
    namespace. Lets callers detect "this session belongs to a named
    project" without re-parsing the original input.
    """
    try:
        p.resolve().relative_to(_workdirs_root().resolve())
        return True
    except ValueError:
        return False


def list_user_projects(app_id: str, user_id: str) -> list[str]:
    """List slug names for a user's projects under a given app.

    Source of truth = the filesystem. A project IS a directory under
    ``~/.digitorn/workdirs/{app_id}/{user_id}/`` — no separate index
    table. Deleting the directory on disk removes the project from
    this listing immediately.
    """
    root = _workdirs_root() / app_id / user_id
    if not root.exists() or not root.is_dir():
        return []
    out: list[str] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and SLUG_REGEX.match(entry.name):
            out.append(entry.name)
    return out

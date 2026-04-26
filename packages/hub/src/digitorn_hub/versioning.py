from __future__ import annotations

import re

_SEMVER_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<pre>[0-9A-Za-z-.]+))?$"
)


class InvalidVersion(ValueError):
    pass


def parse(version: str) -> tuple[int, int, int, tuple]:
    """Return a sortable key. Pre-releases sort BEFORE the corresponding release.

    `1.0.0-alpha` < `1.0.0`
    """
    m = _SEMVER_RE.match(version.strip())
    if not m:
        raise InvalidVersion(f"not a valid semver: {version!r}")
    major = int(m.group("major"))
    minor = int(m.group("minor"))
    patch = int(m.group("patch"))
    pre = m.group("pre")
    if pre is None:
        pre_key: tuple = (1,)
    else:
        parts: list = []
        for chunk in pre.split("."):
            if chunk.isdigit():
                parts.append((0, int(chunk), ""))
            else:
                parts.append((1, 0, chunk))
        pre_key = (0, tuple(parts))
    return (major, minor, patch, pre_key)


def is_greater(a: str, b: str) -> bool:
    return parse(a) > parse(b)


def is_valid(version: str) -> bool:
    try:
        parse(version)
        return True
    except InvalidVersion:
        return False

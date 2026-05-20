"""Guard against accidentally publishing private code on PyPI.

Inspects a built wheel and exits non-zero if it contains any file
that should stay closed-source. This is the last line of defence
before `poetry publish` runs in CI.

Usage:
    python scripts/check-wheel-contents.py dist/digitorn-X.Y.Z-py3-none-any.whl

The script enforces three rules:

1. Only two top-level entries are allowed: the `digitorn/` package
   and the `digitorn-*.dist-info/` metadata directory. Anything
   else (e.g. a stray `auth/` from a misconfigured exclude
   pattern) is rejected.

2. No file path may contain a FORBIDDEN_SUBSTRING. Catches stray
   `node_modules/`, `__pycache__/`, `.env`, `CLAUDE.md`, etc.

3. No file may match a FORBIDDEN_PATTERN. Catches private repo
   markers that could leak via symlinks or relative paths.

If you add a new private package under `packages/`, also add it to
the FORBIDDEN_TOPLEVEL list AND to `[tool.poetry] exclude` in
pyproject.toml. The two lists are independent on purpose so a
typo in pyproject.toml does not silently unlock the guard.
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path


# Top-level directory names that MUST NOT appear in the wheel.
# Update when a new private sibling package is added.
FORBIDDEN_TOPLEVEL = frozenset({
    "auth",
    "digitorn-preview-sdk",
    "digitorn_cli",
    "tests",
    "tools",
    "scripts",
    "docs-site",
    "olds_doc",
})

# Substrings that, if present anywhere in a file path, fail the build.
# Defends against accidental inclusion via symlinks or strange globs.
FORBIDDEN_SUBSTRINGS = (
    "node_modules/",
    "__pycache__/",
    ".pyc",
    "/.git/",
    "/.venv/",
    "/.env",
    "CLAUDE.md",
    "/.logs/",
    "/_internal/",
    "/archive/",
)

# Hard regex catches for tokens / secrets that should never leak via
# a leftover file. The wheel passing this check is necessary but not
# sufficient: real secrets must also be kept out of source files.
FORBIDDEN_PATTERNS = (
    re.compile(r"\.pem$"),
    re.compile(r"\.key$"),
    re.compile(r"id_rsa"),
    re.compile(r"\.kdbx$"),
    re.compile(r"credentials\.json$"),
)


def check_wheel(wheel_path: Path) -> list[str]:
    """Return a list of violation messages. Empty list = wheel is clean."""
    if not wheel_path.exists():
        return [f"wheel not found: {wheel_path}"]

    violations: list[str] = []

    with zipfile.ZipFile(wheel_path) as zf:
        names = zf.namelist()

    # Rule 1 - top-level whitelist.
    toplevels = {n.split("/", 1)[0] for n in names if n}
    allowed_toplevels = {"digitorn"} | {
        t for t in toplevels if t.endswith(".dist-info") and t.startswith("digitorn-")
    }
    forbidden_present = toplevels & FORBIDDEN_TOPLEVEL
    if forbidden_present:
        for entry in sorted(forbidden_present):
            offenders = [n for n in names if n.startswith(f"{entry}/")][:5]
            violations.append(
                f"forbidden top-level '{entry}/' in wheel "
                f"({len(offenders)}+ files, e.g. {offenders[0] if offenders else '<empty>'})"
            )
    unexpected_toplevels = toplevels - allowed_toplevels - FORBIDDEN_TOPLEVEL
    if unexpected_toplevels:
        for entry in sorted(unexpected_toplevels):
            violations.append(
                f"unexpected top-level '{entry}/' - if intentional, "
                f"whitelist it in scripts/check-wheel-contents.py"
            )

    # Rule 2 - forbidden substrings anywhere in the path.
    for substring in FORBIDDEN_SUBSTRINGS:
        offenders = [n for n in names if substring in n]
        if offenders:
            violations.append(
                f"forbidden substring '{substring}' in {len(offenders)} file(s), "
                f"first: {offenders[0]}"
            )

    # Rule 3 - forbidden regex patterns.
    for pattern in FORBIDDEN_PATTERNS:
        offenders = [n for n in names if pattern.search(n)]
        if offenders:
            violations.append(
                f"forbidden pattern '{pattern.pattern}' in {len(offenders)} file(s), "
                f"first: {offenders[0]}"
            )

    return violations


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <wheel-file>", file=sys.stderr)
        return 2

    wheel = Path(sys.argv[1])
    violations = check_wheel(wheel)

    if violations:
        print(f"REJECTED: {wheel} contains private or forbidden files", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        print(
            "\nFix pyproject.toml [tool.poetry] exclude and rebuild, or update "
            "scripts/check-wheel-contents.py if the addition is intentional.",
            file=sys.stderr,
        )
        return 1

    with zipfile.ZipFile(wheel) as zf:
        count = len(zf.namelist())
    print(f"OK: {wheel} ({count} files, no forbidden content)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

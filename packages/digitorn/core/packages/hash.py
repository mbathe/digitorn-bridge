"""Content hash for installed packages.

Per the locked design (D2): SHA-256 over every file in the package
directory, sorted alphabetically by relative path, with the path
itself injected into the hash before the content. Files inside
``.digitorn/`` are **excluded** because that's where we write our
own metadata (manifest.lock, hash.sha256, installed_at.txt) and we'd
otherwise hash our own bookkeeping in a loop.

Why this scheme:

- **Sorted iteration** → deterministic across machines + filesystems
- **Path included** → renaming a file changes the hash (intent is content+layout)
- **NUL separator** → no chance of two file boundaries colliding
- **SHA-256** → standard, fast (~50 ms for 10 MB packages), strong
  enough for drift detection (we don't need adversarial security
  here, just integrity)

Used by:

- ``BuiltinSource`` to detect when a wheel-shipped package was
  upgraded and the daemon should re-deploy it
- ``PackageRegistry`` to store the hash at install time and warn
  about drift later (someone edited an installed package by hand)
- ``InstallFlow`` after a successful fetch to write
  ``.digitorn/hash.sha256``
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# Filenames / dirs we never include in the hash. ``.digitorn/`` is
# our own metadata and would create a feedback loop. Hidden files
# starting with ``.`` are kept (the user might intentionally ship
# one, e.g. ``.gitignore`` or ``.editorconfig``).
#
# IMPORTANT: ``dist`` and ``build`` are excluded GENERICALLY (any
# such dir anywhere) because user apps regularly produce these as
# session-time artefacts that we don't want to count as "drift".
# BUT for builtins / packages that ship a pre-built UI (SDK apps
# like digitorn-builder, digitorn-react-sandbox), the canonical
# location is ``web/dist/`` and that path IS part of the package
# payload. The check in ``_iter_hashable_files`` matches a path
# component anywhere in the rel_path, with ONE exception: the
# top-level ``web/dist/`` is allowed back in. This way:
#   - ``src/dist/foo.js`` (user's nested build cache) → excluded
#   - ``web/dist/index.html`` (SDK app's shipped UI) → INCLUDED
# A rebuild of ``web/dist/`` flips the hash so bootstrap re-installs
# the new bundle - which is exactly what we want for SDK apps.
_EXCLUDE_DIR_NAMES = {
    ".digitorn",
    "node_modules",
    ".vite",
    ".next",
    ".turbo",
    ".cache",
    "__pycache__",
    "dist",
    "build",
    ".output",
    ".svelte-kit",
}


def compute_package_hash(package_dir: Path) -> str:
    """Return the SHA-256 hex digest of a package directory's content.

    Walks ``package_dir`` recursively, sorts the file list by their
    POSIX-relative path, and hashes both the path bytes and file
    content with NUL separators between every chunk. Excludes
    ``.digitorn/`` (daemon-managed metadata).

    Returns the lowercase hex digest (64 chars). Raises
    ``FileNotFoundError`` if ``package_dir`` doesn't exist or isn't
    a directory.

    Performance: ~50ms for a 10 MB package, scales linearly with
    total bytes. Acceptable at boot time for the BuiltinSource scan.
    """
    if not package_dir.is_dir():
        raise FileNotFoundError(
            f"package directory not found or not a directory: {package_dir}"
        )

    h = hashlib.sha256()
    files = list(_iter_hashable_files(package_dir))
    files.sort(key=lambda p: p.as_posix())

    for rel_path in files:
        abs_path = package_dir / rel_path
        try:
            data = abs_path.read_bytes()
        except OSError as exc:
            logger.warning(
                "compute_package_hash: cannot read %s (%s) - skipping",
                abs_path, exc,
            )
            continue
        h.update(rel_path.as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")

    return h.hexdigest()


def _iter_hashable_files(root: Path):
    """Yield relative Paths for every file under ``root``, excluding metadata dirs.

    Generator rather than list so a corrupted package with millions
    of files doesn't OOM us before we even start hashing.

    Special case: the top-level ``web/dist/`` is the canonical home
    for an SDK app's pre-built UI and is part of the shipped payload,
    so it stays IN the hash even though ``dist`` is in the exclude
    set. Nested dist dirs (e.g. ``src/dist/`` for an intermediate
    build cache) are still excluded.

    Walks with ``os.scandir`` and prunes excluded subtrees at descent
    time — never enumerates files inside ``node_modules/``, ``.next/``,
    etc. ``Path.rglob`` walks the whole tree first and filters after,
    which on a 122 MB ``web/node_modules`` would stall the GET-app
    detail endpoint for tens of seconds under load.
    """
    import os
    root_str = str(root)
    # BFS over directories. Each entry is a (dir_path, rel_parts) pair
    # so we can carve out the top-level ``web/dist`` exception without
    # re-checking ancestors on every yield.
    stack: list[tuple[str, tuple[str, ...]]] = [(root_str, ())]
    while stack:
        dir_path, rel_parts = stack.pop()
        try:
            it = os.scandir(dir_path)
        except OSError as exc:
            logger.warning(
                "compute_package_hash: cannot scan %s (%s) - skipping",
                dir_path, exc,
            )
            continue
        with it as entries:
            for entry in entries:
                name = entry.name
                child_parts = rel_parts + (name,)
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    # Carve-out: keep web/dist/* in the hash so SDK
                    # app rebuilds actually flip the install hash.
                    if (
                        len(child_parts) == 2
                        and child_parts[0] == "web"
                        and child_parts[1] == "dist"
                    ):
                        stack.append((entry.path, child_parts))
                        continue
                    if name in _EXCLUDE_DIR_NAMES:
                        continue
                    stack.append((entry.path, child_parts))
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                yield Path(*child_parts)


def write_package_hash_file(package_dir: Path, hash_value: str) -> Path:
    """Persist the hash to ``<package_dir>/.digitorn/hash.sha256``.

    Used after a successful install or upgrade so future drift
    checks have a baseline to compare against. Returns the path of
    the written file.
    """
    metadata_dir = package_dir / ".digitorn"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    target = metadata_dir / "hash.sha256"
    target.write_text(hash_value, encoding="ascii")
    return target


def read_package_hash_file(package_dir: Path) -> str | None:
    """Return the previously-written hash, or None if the file is missing."""
    target = package_dir / ".digitorn" / "hash.sha256"
    if not target.is_file():
        return None
    try:
        return target.read_text(encoding="ascii").strip()
    except Exception as exc:
        logger.warning("read_package_hash_file: %s - %s", target, exc)
        return None


def detect_drift(package_dir: Path) -> tuple[bool, str, str | None]:
    """Compare the on-disk content hash to the stored baseline.

    Returns ``(drifted, current_hash, stored_hash)``:

    - ``drifted=True`` means someone (or something) modified files
      in the installed package since install/upgrade. The daemon
      should warn - drift might be intentional dev iteration, or
      it might be tampering.
    - ``stored_hash=None`` means there's no baseline yet (very old
      package or never properly installed). Don't treat that as drift.
    """
    current = compute_package_hash(package_dir)
    stored = read_package_hash_file(package_dir)
    if stored is None:
        return False, current, None
    return (current != stored), current, stored

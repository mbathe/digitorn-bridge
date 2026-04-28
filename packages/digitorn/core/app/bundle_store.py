"""Bundle store - disk-backed storage for immutable app bundles.

A bundle is the frozen set of files an app needs to run: its YAML source
plus every file the YAML references (skills, agent prompt files, etc.).
Once a bundle is created, it is content-addressed by SHA-256 and never
mutates: redeploying with identical content is a no-op, and redeploying
with changes creates a new bundle with a new hash alongside the old one
(so rollback stays cheap).

Disk layout::

    ~/.digitorn/apps/
        <app_id>/
            bundle-<short_hash>/
                app.yaml
                skills/
                    commit.md
                    review.md
                    ...
                agent_prompts/
                    coordinator.md
                meta.json                ← {bundle_hash, created_at, yaml_filename, assets: [...]}

Paths inside a bundle are always stored relative to the bundle root and
only use forward slashes - the store refuses any path that tries to
escape the bundle root with ``..`` or absolute components.

Usage::

    store = BundleStore()
    bundle = store.create(
        app_id="opencode-clone",
        yaml_content="app: {...}\\n",
        assets={
            "skills/commit.md": "# Git Commit\\n...",
            "skills/review.md": "...",
        },
        yaml_filename="app.yaml",
    )
    # bundle.bundle_hash, bundle.bundle_path, bundle.asset_count, ...

    # Reading back (used at daemon reload):
    assets = store.load_assets(bundle)
    loader = store.asset_loader(bundle)
    yaml_content = store.load_yaml(bundle)

    # Deletion (used when removing an app):
    store.delete_app(app_id)  # removes every bundle for the app
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _default_root() -> Path:
    """Default bundle root: ``~/.digitorn/apps``.

    Matches the pattern used by the rest of the daemon (keys, sessions,
    kv, etc. all live under ``~/.digitorn/``).
    """
    return Path.home() / ".digitorn" / "apps"


@dataclass
class BundleDescriptor:
    """In-memory descriptor for a bundle the store just wrote or loaded.

    This is deliberately decoupled from the SQLAlchemy ``AppBundle`` row:
    the store only knows about disk, the syncer maps these fields to the
    DB model.
    """

    app_id: str
    bundle_hash: str
    bundle_path: Path
    yaml_filename: str = "app.yaml"
    asset_paths: list[str] = field(default_factory=list)
    size_bytes: int = 0

    @property
    def short_hash(self) -> str:
        return self.bundle_hash[:12]


class BundleStoreError(RuntimeError):
    """Raised for any bundle store failure (I/O, invalid path, corrupt meta)."""


class BundleStore:
    """Disk-backed store for immutable app bundles.

    Thread safety: individual operations are atomic-ish (write to a temp
    directory, then ``os.replace`` into place) so a concurrent reader
    never observes a half-written bundle. The store does NOT hold a
    process-wide lock - rely on the AppManager to serialize deploys for
    the same ``app_id`` if you need strict ordering.
    """

    # Files the store writes itself inside every bundle directory.
    _META_FILENAME = "meta.json"

    def __init__(self, root: Path | str | None = None) -> None:
        self._root = Path(root) if root is not None else _default_root()
        self._root.mkdir(parents=True, exist_ok=True)

    # ── Path helpers ────────────────────────────────────────────────────

    @property
    def root(self) -> Path:
        return self._root

    def _app_dir(self, app_id: str) -> Path:
        self._check_safe_segment(app_id, "app_id")
        return self._root / app_id

    def _bundle_dir(self, app_id: str, bundle_hash: str) -> Path:
        self._check_safe_segment(app_id, "app_id")
        self._check_hash(bundle_hash)
        return self._app_dir(app_id) / f"bundle-{bundle_hash[:12]}"

    @staticmethod
    def _check_safe_segment(name: str, label: str) -> None:
        """Refuse names that could escape the store root."""
        if not name or not isinstance(name, str):
            raise BundleStoreError(f"{label} must be a non-empty string")
        if "/" in name or "\\" in name or ".." in name or name.startswith("."):
            raise BundleStoreError(f"{label} contains forbidden characters: {name!r}")

    @staticmethod
    def _check_hash(bundle_hash: str) -> None:
        if not bundle_hash or not isinstance(bundle_hash, str):
            raise BundleStoreError("bundle_hash must be a non-empty string")
        # SHA-256 hex is 64 chars; we accept short forms (>=8) too so
        # callers can pass bundle_hash[:12] interchangeably.
        if len(bundle_hash) < 8 or len(bundle_hash) > 64:
            raise BundleStoreError(f"bundle_hash length invalid: {len(bundle_hash)}")
        if not all(c in "0123456789abcdef" for c in bundle_hash.lower()):
            raise BundleStoreError(f"bundle_hash is not hex: {bundle_hash!r}")

    @staticmethod
    def _normalise_asset_path(rel_path: str) -> str:
        """Normalise a relative asset path and reject anything unsafe.

        Returns the normalised path (forward slashes, no leading ``./``).
        Raises ``BundleStoreError`` if the path is absolute, contains
        ``..``, or tries to escape the bundle root.
        """
        if not rel_path or not isinstance(rel_path, str):
            raise BundleStoreError("asset path must be a non-empty string")
        # Normalise separators
        path = rel_path.replace("\\", "/").strip()
        # Strip leading "./"
        while path.startswith("./"):
            path = path[2:]
        if path.startswith("/"):
            raise BundleStoreError(f"asset path must be relative: {rel_path!r}")
        parts = path.split("/")
        if any(p in ("", "..") for p in parts):
            raise BundleStoreError(f"asset path contains '..' or empty segment: {rel_path!r}")
        return path

    # ── Hashing ─────────────────────────────────────────────────────────

    @staticmethod
    def compute_hash(yaml_content: str, assets: dict[str, str]) -> str:
        """Deterministic SHA-256 over the YAML + sorted assets.

        Same content (even with dict insertion order reshuffled) always
        produces the same hash, so the syncer can deduplicate deploys.
        """
        hasher = hashlib.sha256()
        hasher.update(b"yaml:")
        hasher.update(yaml_content.encode("utf-8"))
        hasher.update(b"\n")
        for path in sorted(assets.keys()):
            normalised = BundleStore._normalise_asset_path(path)
            hasher.update(b"asset:")
            hasher.update(normalised.encode("utf-8"))
            hasher.update(b"\n")
            hasher.update(assets[path].encode("utf-8"))
            hasher.update(b"\n")
        return hasher.hexdigest()

    # ── Write path ──────────────────────────────────────────────────────

    def create(
        self,
        *,
        app_id: str,
        yaml_content: str,
        assets: dict[str, str],
        yaml_filename: str = "app.yaml",
    ) -> BundleDescriptor:
        """Persist a bundle on disk and return its descriptor.

        Idempotent on ``(app_id, bundle_hash)``: if a bundle with the
        same content already exists for this app, the existing one is
        returned without rewriting it.
        """
        self._check_safe_segment(app_id, "app_id")
        if not isinstance(yaml_content, str):
            raise BundleStoreError("yaml_content must be a string")
        assets = dict(assets or {})
        for rel in list(assets.keys()):
            assets[self._normalise_asset_path(rel)] = assets.pop(rel)

        bundle_hash = self.compute_hash(yaml_content, assets)
        bundle_dir = self._bundle_dir(app_id, bundle_hash)

        if bundle_dir.exists():
            # Already present - assume on-disk content matches the hash.
            # We re-read the meta to populate size/asset_paths accurately.
            try:
                existing = self._load_descriptor(app_id, bundle_dir)
                if existing is not None:
                    return existing
            except Exception as exc:
                logger.warning(
                    "bundle_store: existing bundle %s unreadable (%s) - recreating",
                    bundle_dir, exc,
                )
                shutil.rmtree(bundle_dir, ignore_errors=True)

        # Write to a temp directory and atomic-rename into place so a
        # concurrent reader never observes a partial bundle.
        tmp_dir = bundle_dir.with_name(bundle_dir.name + ".tmp")
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True)

        size_bytes = 0
        try:
            # YAML file
            yaml_path = tmp_dir / yaml_filename
            yaml_path.parent.mkdir(parents=True, exist_ok=True)
            yaml_bytes = yaml_content.encode("utf-8")
            yaml_path.write_bytes(yaml_bytes)
            size_bytes += len(yaml_bytes)

            # Assets
            for rel_path in sorted(assets.keys()):
                content = assets[rel_path]
                target = tmp_dir / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                data = content.encode("utf-8")
                target.write_bytes(data)
                size_bytes += len(data)

            # Meta
            meta = {
                "bundle_hash": bundle_hash,
                "app_id": app_id,
                "yaml_filename": yaml_filename,
                "assets": sorted(assets.keys()),
                "size_bytes": size_bytes,
                "schema_version": 1,
            }
            (tmp_dir / self._META_FILENAME).write_text(
                json.dumps(meta, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            # Atomic rename
            if bundle_dir.exists():
                shutil.rmtree(bundle_dir, ignore_errors=True)
            bundle_dir.parent.mkdir(parents=True, exist_ok=True)
            tmp_dir.rename(bundle_dir)
        except Exception:
            # Clean up the tmp dir on any failure so we don't leak it.
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        logger.info(
            "bundle_created app=%s hash=%s assets=%d size=%dB path=%s",
            app_id, bundle_hash[:12], len(assets), size_bytes, bundle_dir,
        )

        return BundleDescriptor(
            app_id=app_id,
            bundle_hash=bundle_hash,
            bundle_path=bundle_dir,
            yaml_filename=yaml_filename,
            asset_paths=sorted(assets.keys()),
            size_bytes=size_bytes,
        )

    # ── Read path ───────────────────────────────────────────────────────

    def exists(self, app_id: str, bundle_hash: str) -> bool:
        try:
            return self._bundle_dir(app_id, bundle_hash).is_dir()
        except BundleStoreError:
            return False

    def get(self, app_id: str, bundle_hash: str) -> BundleDescriptor | None:
        """Return the descriptor for a bundle, or None if it doesn't exist."""
        try:
            bundle_dir = self._bundle_dir(app_id, bundle_hash)
        except BundleStoreError:
            return None
        if not bundle_dir.is_dir():
            return None
        return self._load_descriptor(app_id, bundle_dir)

    def get_by_path(self, app_id: str, bundle_path: str | Path) -> BundleDescriptor | None:
        """Load a bundle by its stored absolute path.

        Useful when the DB has ``bundle_path`` already resolved and we
        don't want to re-derive it from the hash.
        """
        path = Path(bundle_path)
        if not path.is_dir():
            return None
        return self._load_descriptor(app_id, path)

    def _load_descriptor(
        self, app_id: str, bundle_dir: Path,
    ) -> BundleDescriptor | None:
        meta_path = bundle_dir / self._META_FILENAME
        if not meta_path.is_file():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise BundleStoreError(
                f"bundle meta unreadable at {meta_path}: {exc}"
            ) from exc
        return BundleDescriptor(
            app_id=app_id,
            bundle_hash=str(meta.get("bundle_hash", "")),
            bundle_path=bundle_dir,
            yaml_filename=str(meta.get("yaml_filename", "app.yaml")),
            asset_paths=list(meta.get("assets", [])),
            size_bytes=int(meta.get("size_bytes", 0)),
        )

    def list_for_app(self, app_id: str) -> list[BundleDescriptor]:
        """Return every bundle on disk for the given app, newest first."""
        try:
            app_dir = self._app_dir(app_id)
        except BundleStoreError:
            return []
        if not app_dir.is_dir():
            return []
        bundles: list[BundleDescriptor] = []
        for entry in app_dir.iterdir():
            if not entry.is_dir() or not entry.name.startswith("bundle-"):
                continue
            try:
                desc = self._load_descriptor(app_id, entry)
            except BundleStoreError as exc:
                logger.warning("bundle_store: skipping unreadable %s: %s", entry, exc)
                continue
            if desc is not None:
                bundles.append(desc)
        # Newest first - use mtime since we don't carry created_at in the meta.
        bundles.sort(
            key=lambda b: b.bundle_path.stat().st_mtime,
            reverse=True,
        )
        return bundles

    def load_yaml(self, bundle: BundleDescriptor) -> str:
        path = bundle.bundle_path / bundle.yaml_filename
        if not path.is_file():
            raise BundleStoreError(f"bundle YAML missing: {path}")
        return path.read_text(encoding="utf-8")

    def load_asset(self, bundle: BundleDescriptor, rel_path: str) -> str:
        normalised = self._normalise_asset_path(rel_path)
        path = bundle.bundle_path / normalised
        if not path.is_file():
            raise BundleStoreError(f"bundle asset missing: {normalised} in {bundle.bundle_path}")
        # Defence in depth: ensure the resolved path is still inside the bundle.
        try:
            path.resolve().relative_to(bundle.bundle_path.resolve())
        except ValueError as exc:
            raise BundleStoreError(
                f"bundle asset escapes bundle root: {normalised}"
            ) from exc
        return path.read_text(encoding="utf-8")

    def load_assets(self, bundle: BundleDescriptor) -> dict[str, str]:
        """Load every asset of the bundle into a dict (no YAML)."""
        out: dict[str, str] = {}
        for rel in bundle.asset_paths:
            out[rel] = self.load_asset(bundle, rel)
        return out

    def asset_loader(
        self, bundle: BundleDescriptor,
    ) -> Callable[[str], str | None]:
        """Return a callable that resolves a relative path to its content.

        The returned function is what the compiler passes to itself in
        bundle-reload mode instead of doing ``Path(...).read_text()``.
        Returns ``None`` when the asset is not found so callers can fall
        back to their own error handling (the compiler prefers an
        explicit error message).
        """
        def _load(rel_path: str) -> str | None:
            try:
                return self.load_asset(bundle, rel_path)
            except BundleStoreError:
                return None
        return _load

    # ── Delete path ─────────────────────────────────────────────────────

    def delete_bundle(self, app_id: str, bundle_hash: str) -> bool:
        """Delete a single bundle directory. Returns True if deleted."""
        try:
            bundle_dir = self._bundle_dir(app_id, bundle_hash)
        except BundleStoreError:
            return False
        if not bundle_dir.is_dir():
            return False
        shutil.rmtree(bundle_dir, ignore_errors=False)
        logger.info("bundle_deleted app=%s hash=%s", app_id, bundle_hash[:12])
        return True

    def delete_app(self, app_id: str) -> int:
        """Remove every bundle for an app. Returns the number of bundles deleted.

        Also removes the (now empty) app directory. Safe to call when
        nothing is on disk - returns 0.
        """
        try:
            app_dir = self._app_dir(app_id)
        except BundleStoreError:
            return 0
        if not app_dir.is_dir():
            return 0
        count = 0
        for entry in app_dir.iterdir():
            if entry.is_dir() and entry.name.startswith("bundle-"):
                count += 1
        shutil.rmtree(app_dir, ignore_errors=False)
        logger.info("bundle_store: deleted app=%s bundles=%d", app_id, count)
        return count

    # ── Introspection ───────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """Aggregate stats across every app (for /health / metrics)."""
        if not self._root.is_dir():
            return {"app_count": 0, "bundle_count": 0, "size_bytes": 0}
        app_count = 0
        bundle_count = 0
        size_bytes = 0
        for app_dir in self._root.iterdir():
            if not app_dir.is_dir():
                continue
            app_count += 1
            for entry in app_dir.iterdir():
                if entry.is_dir() and entry.name.startswith("bundle-"):
                    bundle_count += 1
                    for f in entry.rglob("*"):
                        if f.is_file():
                            try:
                                size_bytes += f.stat().st_size
                            except OSError:
                                pass
        return {
            "app_count": app_count,
            "bundle_count": bundle_count,
            "size_bytes": size_bytes,
        }

"""Copy-on-Write workspace snapshots — session-level filesystem isolation.

Provides per-session workspace snapshots so each session sees its own
copy of the workspace.  Changes are isolated — one session cannot see
another's modifications.

Strategies (tried in order):
    1. overlayfs in user namespace (kernel 5.11+) — zero-copy, instant
    2. cp --reflink=auto (btrfs/xfs) — CoW at block level
    3. rsync — fallback, full copy

On session end, changes can be committed (merge back) or discarded.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class WorkspaceSnapshot:
    """A CoW snapshot of a workspace for a single session."""

    def __init__(
        self,
        original: str,
        session_id: str,
        *,
        scratch_dir: str | None = None,
    ) -> None:
        self._original = Path(original).resolve()
        self._session_id = session_id
        self._scratch_base = Path(scratch_dir) if scratch_dir else Path(
            tempfile.gettempdir()
        ) / "digitorn-snapshots"

        self._upper: Path | None = None    # overlayfs upper layer (changes)
        self._work: Path | None = None     # overlayfs work dir
        self._merged: Path | None = None   # merged view (session workspace)
        self._strategy: str = "none"
        self._active = False

    @property
    def merged_path(self) -> str:
        """The path the session should use as its workspace."""
        if self._merged:
            return str(self._merged)
        return str(self._original)

    @property
    def strategy(self) -> str:
        return self._strategy

    @property
    def is_active(self) -> bool:
        return self._active

    async def create(self) -> str:
        """Create the workspace snapshot. Returns the merged path."""
        self._scratch_base.mkdir(parents=True, exist_ok=True)

        # Try strategies in order
        if await self._try_overlayfs():
            self._strategy = "overlayfs"
        elif await self._try_reflink():
            self._strategy = "reflink"
        else:
            await self._fallback_copy()
            self._strategy = "copy"

        self._active = True
        logger.info(
            "snapshot_created session=%s strategy=%s path=%s",
            self._session_id, self._strategy, self.merged_path,
        )
        return self.merged_path

    async def commit(self) -> bool:
        """Merge changes back to the original workspace."""
        if not self._active:
            return False

        if self._strategy == "overlayfs":
            return await self._commit_overlay()
        elif self._strategy in ("reflink", "copy"):
            return await self._commit_copy()

        return False

    async def discard(self) -> None:
        """Discard session changes and clean up."""
        if not self._active:
            return

        if self._strategy == "overlayfs":
            await self._cleanup_overlay()
        elif self._strategy in ("reflink", "copy"):
            await self._cleanup_copy()

        self._active = False
        logger.info("snapshot_discarded session=%s", self._session_id)

    # ── overlayfs ────────────────────────────────────────────────

    async def _try_overlayfs(self) -> bool:
        """Try to create an overlayfs mount (requires user namespace + kernel 5.11+)."""
        try:
            session_dir = self._scratch_base / self._session_id
            self._upper = session_dir / "upper"
            self._work = session_dir / "work"
            self._merged = session_dir / "merged"

            for d in (self._upper, self._work, self._merged):
                d.mkdir(parents=True, exist_ok=True)

            # mount -t overlay overlay -o lowerdir=...,upperdir=...,workdir=... merged
            proc = await asyncio.create_subprocess_exec(
                "mount", "-t", "overlay", "overlay",
                "-o", f"lowerdir={self._original},upperdir={self._upper},"
                      f"workdir={self._work}",
                str(self._merged),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)

            if proc.returncode == 0:
                return True

            # Clean up on failure
            for d in (self._merged, self._work, self._upper):
                if d.exists():
                    d.rmdir()
            self._upper = self._work = self._merged = None
            return False

        except Exception:
            self._upper = self._work = self._merged = None
            return False

    async def _commit_overlay(self) -> bool:
        """Merge overlayfs upper layer back to original."""
        if not self._upper or not self._upper.exists():
            return False

        try:
            # rsync the upper (changed files) back to original
            proc = await asyncio.create_subprocess_exec(
                "rsync", "-a", "--remove-source-files",
                f"{self._upper}/", f"{self._original}/",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
            return proc.returncode == 0
        except Exception as exc:
            logger.warning("overlay_commit_failed: %s", exc)
            return False
        finally:
            await self._cleanup_overlay()

    async def _cleanup_overlay(self) -> None:
        if self._merged and self._merged.exists():
            try:
                proc = await asyncio.create_subprocess_exec(
                    "umount", str(self._merged),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=5)
            except Exception:
                pass

        session_dir = self._scratch_base / self._session_id
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)

    # ── reflink / copy ───────────────────────────────────────────

    async def _try_reflink(self) -> bool:
        """Try CoW copy via cp --reflink=auto (btrfs/xfs)."""
        self._merged = self._scratch_base / self._session_id / "workspace"
        self._merged.parent.mkdir(parents=True, exist_ok=True)

        try:
            proc = await asyncio.create_subprocess_exec(
                "cp", "--reflink=auto", "-a",
                str(self._original), str(self._merged),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

            if proc.returncode == 0:
                return True

            # Check if reflink is supported
            if b"not supported" in stderr or b"Operation not supported" in stderr:
                if self._merged.exists():
                    shutil.rmtree(self._merged, ignore_errors=True)
                self._merged = None
                return False

            # cp succeeded without reflink (auto fallback) — that's fine
            return proc.returncode == 0

        except Exception:
            self._merged = None
            return False

    async def _fallback_copy(self) -> None:
        """Full copy as last resort."""
        self._merged = self._scratch_base / self._session_id / "workspace"
        self._merged.parent.mkdir(parents=True, exist_ok=True)

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            shutil.copytree,
            str(self._original),
            str(self._merged),
        )

    async def _commit_copy(self) -> bool:
        """Sync changes from copy back to original."""
        if not self._merged or not self._merged.exists():
            return False

        try:
            proc = await asyncio.create_subprocess_exec(
                "rsync", "-a", "--delete",
                f"{self._merged}/", f"{self._original}/",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)
            return proc.returncode == 0
        except Exception as exc:
            logger.warning("copy_commit_failed: %s", exc)
            return False
        finally:
            await self._cleanup_copy()

    async def _cleanup_copy(self) -> None:
        session_dir = self._scratch_base / self._session_id
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)

"""Image Store - disk-backed storage for multimodal images.

Images are stored on disk and referenced by lightweight ImageRef objects.
Base64 encoding is deferred to the moment of LLM injection (on-demand).

Usage::

    store = ImageStore()
    ref = await store.store(png_bytes, "image/png", session_id="abc")
    base64 = await store.get_base64(ref.image_id, session_id="abc")
    await store.cleanup_session("abc")
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

_MIME_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}

_EXT_TO_MIME = {v: k for k, v in _MIME_TO_EXT.items()}
_EXT_TO_MIME[".jpg"] = "image/jpeg"
_EXT_TO_MIME[".jpeg"] = "image/jpeg"

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp", ".tiff", ".ico"}


def is_image_path(path: str | Path) -> bool:
    """Check if a file path looks like an image."""
    return Path(path).suffix.lower() in _IMAGE_EXTENSIONS


def mime_for_path(path: str | Path) -> str:
    """Get MIME type from file extension."""
    ext = Path(path).suffix.lower()
    return _EXT_TO_MIME.get(ext, "image/png")


@dataclass
class ImageRef:
    """Lightweight reference to a stored image."""

    image_id: str
    path: str
    mime: str
    size: int
    width: int = 0
    height: int = 0
    alt_text: str = ""
    created_at: float = field(default_factory=time.time)
    turn: int = 0  # Turn number when the image was added

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_id": self.image_id,
            "mime": self.mime,
            "size": self.size,
            "width": self.width,
            "height": self.height,
            "alt_text": self.alt_text,
            "turn": self.turn,
        }


class ImageStore:
    """Disk-backed image storage with session isolation."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        if base_dir:
            self._base_dir = Path(base_dir)
        else:
            try:
                from digitorn.core.config import get_settings
                cfg_dir = get_settings().images.storage_dir
                if cfg_dir:
                    self._base_dir = Path(cfg_dir)
                else:
                    self._base_dir = Path.home() / ".digitorn" / "images"
            except Exception:
                self._base_dir = Path.home() / ".digitorn" / "images"

        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._refs: dict[str, dict[str, ImageRef]] = {}  # session_id → {image_id → ref}

    async def store(
        self,
        data: bytes,
        mime: str,
        session_id: str,
        alt_text: str = "",
        turn: int = 0,
    ) -> ImageRef:
        """Store an image on disk and return a reference."""
        image_id = uuid4().hex[:12]
        ext = _MIME_TO_EXT.get(mime, ".png")

        session_dir = self._base_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / f"{image_id}{ext}"

        path.write_bytes(data)

        # Try to get dimensions
        width, height = 0, 0
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(data))
            width, height = img.size
        except Exception:
            pass

        ref = ImageRef(
            image_id=image_id,
            path=str(path),
            mime=mime,
            size=len(data),
            width=width,
            height=height,
            alt_text=alt_text,
            turn=turn,
        )

        self._refs.setdefault(session_id, {})[image_id] = ref

        logger.info(
            "image_stored id=%s session=%s size=%d %dx%d %s",
            image_id, session_id[:8], len(data), width, height, mime,
        )
        return ref

    async def store_base64(
        self,
        b64_data: str,
        mime: str,
        session_id: str,
        alt_text: str = "",
        turn: int = 0,
    ) -> ImageRef:
        """Store a base64-encoded image."""
        data = base64.b64decode(b64_data)
        return await self.store(data, mime, session_id, alt_text, turn)

    async def get(self, image_id: str, session_id: str) -> bytes | None:
        """Get raw image bytes."""
        ref = self._get_ref(image_id, session_id)
        if ref is None:
            return None
        path = Path(ref.path)
        if not path.exists():
            return None
        return path.read_bytes()

    async def get_base64(self, image_id: str, session_id: str) -> str | None:
        """Get image as base64 string."""
        data = await self.get(image_id, session_id)
        if data is None:
            return None
        return base64.b64encode(data).decode("ascii")

    async def get_base64_resized(
        self, image_id: str, session_id: str, max_size: int = 512,
    ) -> str | None:
        """Get image as base64, resized to max_size px (longest side)."""
        data = await self.get(image_id, session_id)
        if data is None:
            return None
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(data))
            w, h = img.size
            if max(w, h) > max_size:
                ratio = max_size / max(w, h)
                new_w, new_h = int(w * ratio), int(h * ratio)
                img = img.resize((new_w, new_h), Image.LANCZOS)
            buf = io.BytesIO()
            fmt = "PNG" if img.mode == "RGBA" else "JPEG"
            img.save(buf, format=fmt, quality=80)
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except ImportError:
            # No PIL - return full size
            return base64.b64encode(data).decode("ascii")

    def get_ref(self, image_id: str, session_id: str) -> ImageRef | None:
        """Get an image reference."""
        return self._get_ref(image_id, session_id)

    def list_refs(self, session_id: str) -> list[ImageRef]:
        """List all images for a session."""
        return list(self._refs.get(session_id, {}).values())

    def cleanup_session(self, session_id: str) -> int:
        """Delete all images for a session. Returns count deleted."""
        refs = self._refs.pop(session_id, {})
        count = 0
        for ref in refs.values():
            try:
                Path(ref.path).unlink(missing_ok=True)
                count += 1
            except Exception:
                pass
        # Remove session dir if empty
        session_dir = self._base_dir / session_id
        try:
            if session_dir.exists() and not any(session_dir.iterdir()):
                session_dir.rmdir()
        except Exception:
            pass
        return count

    def _get_ref(self, image_id: str, session_id: str) -> ImageRef | None:
        refs = self._refs.get(session_id, {})
        ref = refs.get(image_id)
        if ref is not None:
            return ref
        # Fallback: scan disk
        session_dir = self._base_dir / session_id
        if not session_dir.exists():
            return None
        for f in session_dir.iterdir():
            if f.stem == image_id:
                mime = mime_for_path(f)
                ref = ImageRef(
                    image_id=image_id, path=str(f), mime=mime,
                    size=f.stat().st_size,
                )
                self._refs.setdefault(session_id, {})[image_id] = ref
                return ref
        return None


# ── Singleton ─────────────────────────────────────────────────────

_store: ImageStore | None = None


def get_image_store() -> ImageStore:
    """Return the global image store singleton."""
    global _store
    if _store is None:
        _store = ImageStore()
    return _store

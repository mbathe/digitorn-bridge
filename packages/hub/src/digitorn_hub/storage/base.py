from __future__ import annotations

from abc import ABC, abstractmethod


class ObjectStorage(ABC):
    @abstractmethod
    async def put_object(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None: ...

    @abstractmethod
    async def delete_object(self, key: str) -> None: ...

    @abstractmethod
    async def presigned_get_url(self, key: str, ttl_seconds: int) -> str: ...

    @abstractmethod
    async def object_exists(self, key: str) -> bool: ...

    @abstractmethod
    async def get_object_bytes(self, key: str) -> bytes:
        """Read the full body of `key` into memory. Raises if missing.

        Reserved for SMALL objects (icons, thumbnails) — for archive
        downloads use presigned URLs so the bytes never traverse the
        Hub server."""
        ...


from __future__ import annotations

import io
from functools import lru_cache

import aioboto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from ..settings import Settings, get_settings
from .base import ObjectStorage


class S3Storage(ObjectStorage):
    """S3-compatible object storage (Oracle OCI Object Storage, MinIO, AWS S3, R2)."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._session = aioboto3.Session()

    def _client_kwargs(self) -> dict:
        return {
            "service_name": "s3",
            "endpoint_url": self._settings.s3_endpoint_url or None,
            "region_name": self._settings.s3_region,
            "aws_access_key_id": self._settings.s3_access_key,
            "aws_secret_access_key": self._settings.s3_secret_key,
            "config": BotoConfig(
                signature_version="s3v4",
                s3={
                    "addressing_style": "path",
                    "payload_signing_enabled": True,
                },
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        }

    @property
    def bucket(self) -> str:
        return self._settings.s3_bucket

    async def put_object(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        body = io.BytesIO(data)
        async with self._session.client(**self._client_kwargs()) as s3:
            await s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                ContentLength=len(data),
            )

    async def delete_object(self, key: str) -> None:
        async with self._session.client(**self._client_kwargs()) as s3:
            await s3.delete_object(Bucket=self.bucket, Key=key)

    async def presigned_get_url(self, key: str, ttl_seconds: int) -> str:
        async with self._session.client(**self._client_kwargs()) as s3:
            return await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=ttl_seconds,
            )

    async def get_object_bytes(self, key: str) -> bytes:
        async with self._session.client(**self._client_kwargs()) as s3:
            obj = await s3.get_object(Bucket=self.bucket, Key=key)
            return await obj["Body"].read()

    async def object_exists(self, key: str) -> bool:
        async with self._session.client(**self._client_kwargs()) as s3:
            try:
                await s3.head_object(Bucket=self.bucket, Key=key)
                return True
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in {"404", "NoSuchKey", "NotFound"}:
                    return False
                raise



@lru_cache(maxsize=1)
def get_storage() -> ObjectStorage:
    return S3Storage(get_settings())

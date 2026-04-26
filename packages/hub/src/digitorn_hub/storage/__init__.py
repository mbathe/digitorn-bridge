from .base import ObjectStorage
from .s3 import S3Storage, get_storage

__all__ = ["ObjectStorage", "S3Storage", "get_storage"]

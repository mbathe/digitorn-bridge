"""JSON encoding helpers shared by client + server side."""
from __future__ import annotations


import logging

logger = logging.getLogger(__name__)
import base64
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

# Marker tags used to round-trip non-JSON-native types. The decoder
# checks for these keys and reconstructs the original Python value.
_BYTES_TAG = "__bytes_b64__"
_PATH_TAG = "__path__"
_DATETIME_TAG = "__datetime__"
_DATE_TAG = "__date__"
_DECIMAL_TAG = "__decimal__"

def _encode_default(obj: Any) -> Any:
    if isinstance(obj, (bytes, bytearray)):
        return {_BYTES_TAG: base64.b64encode(bytes(obj)).decode("ascii")}
    if isinstance(obj, Path):
        return {_PATH_TAG: str(obj)}
    if isinstance(obj, datetime):
        return {_DATETIME_TAG: obj.isoformat()}
    if isinstance(obj, date):
        return {_DATE_TAG: obj.isoformat()}
    if isinstance(obj, Decimal):
        return {_DECIMAL_TAG: str(obj)}
    if isinstance(obj, Enum):
        return obj.value
    # Pydantic v2 model.
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="json")
        except Exception as exc:
            logger.debug("serializers best-effort block failed: %s", exc)
    # Numpy scalars.
    if hasattr(obj, "item") and not isinstance(obj, type):
        try:
            return obj.item()
        except Exception as exc:
            logger.debug("serializers best-effort block failed: %s", exc)
    return str(obj)

def _decode_hook(obj: dict[str, Any]) -> Any:
    if len(obj) == 1:
        if _BYTES_TAG in obj:
            return base64.b64decode(obj[_BYTES_TAG])
        if _PATH_TAG in obj:
            return Path(obj[_PATH_TAG])
        if _DATETIME_TAG in obj:
            return datetime.fromisoformat(obj[_DATETIME_TAG])
        if _DATE_TAG in obj:
            return date.fromisoformat(obj[_DATE_TAG])
        if _DECIMAL_TAG in obj:
            return Decimal(obj[_DECIMAL_TAG])
    return obj

def dumps(payload: Any) -> str:
    """Serialise `payload` to a JSON string with our extended type."""
    return json.dumps(
        payload, default=_encode_default, ensure_ascii=False,
    )

def loads(raw: str) -> Any:
    """Inverse of `dumps`. Reconstructs `bytes` / `Path` /."""
    return json.loads(raw, object_hook=_decode_hook)

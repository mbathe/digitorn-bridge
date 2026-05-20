"""Serialize/deserialize CompiledApp for worker IPC."""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

logger = logging.getLogger(__name__)


def compiled_app_to_json(compiled: Any) -> str:
    """Serialize a CompiledApp to a JSON string."""
    return json.dumps(_to_serializable(compiled), ensure_ascii=False)


def compiled_app_from_json(data: str) -> Any:
    """Deserialize a CompiledApp from a JSON string."""
    from digitorn.core.app.compiler import (
        CompiledApp,
        CompiledAgent,
        CompiledBrain,
        CompiledChannelInstance,
        CompiledContextConfig,
        CompiledExecution,
        CompiledHook,
        CompiledInput,
        CompiledModuleConfig,
        CompiledOutput,
        CompiledSetupStep,
        CompiledTrigger,
    )
    from digitorn.core.app.schema import AppMeta
    from digitorn.core.security import SecurityProfile, ModuleGrant

    registry = {
        "CompiledApp": CompiledApp,
        "CompiledAgent": CompiledAgent,
        "CompiledBrain": CompiledBrain,
        "CompiledChannelInstance": CompiledChannelInstance,
        "CompiledContextConfig": CompiledContextConfig,
        "CompiledExecution": CompiledExecution,
        "CompiledHook": CompiledHook,
        "CompiledInput": CompiledInput,
        "CompiledModuleConfig": CompiledModuleConfig,
        "CompiledOutput": CompiledOutput,
        "CompiledSetupStep": CompiledSetupStep,
        "CompiledTrigger": CompiledTrigger,
        "AppMeta": AppMeta,
        "SecurityProfile": SecurityProfile,
        "ModuleGrant": ModuleGrant,
    }

    raw = json.loads(data)
    return _from_serializable(raw, registry)


def _to_serializable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj

    if isinstance(obj, Path):
        return {"__path__": str(obj)}

    if isinstance(obj, frozenset):
        return {"__frozenset__": sorted(obj, key=str)}

    if isinstance(obj, set):
        return {"__set__": sorted(obj, key=str)}

    if isinstance(obj, type):
        return None

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        fields = {}
        for f in dataclasses.fields(obj):
            val = getattr(obj, f.name)
            fields[f.name] = _to_serializable(val)
        return {
            "__dc__": type(obj).__name__,
            **fields,
        }

    if hasattr(obj, "model_dump"):
        dump = obj.model_dump()
        return {
            "__dc__": type(obj).__name__,
            **{k: _to_serializable(v) for k, v in dump.items()},
        }

    if isinstance(obj, dict):
        return {k: _to_serializable(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple)):
        return [_to_serializable(item) for item in obj]

    return str(obj)


def _from_serializable(obj: Any, registry: dict[str, type]) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj

    if isinstance(obj, list):
        return [_from_serializable(item, registry) for item in obj]

    if isinstance(obj, dict):
        if "__path__" in obj:
            return Path(obj["__path__"])

        if "__frozenset__" in obj:
            return frozenset(obj["__frozenset__"])

        if "__set__" in obj:
            return set(obj["__set__"])

        if "__dc__" in obj:
            return _reconstruct_dataclass(obj, registry)

        return {k: _from_serializable(v, registry) for k, v in obj.items()}

    return obj


def _reconstruct_dataclass(
    obj: dict[str, Any],
    registry: dict[str, type],
) -> Any:
    type_name = obj["__dc__"]
    cls = registry.get(type_name)
    if cls is None:
        logger.warning("unknown_dataclass_type: %s - returning dict", type_name)
        return {k: v for k, v in obj.items() if k != "__dc__"}

    fields_data = {k: v for k, v in obj.items() if k != "__dc__"}

    if hasattr(cls, "model_validate"):
        return cls.model_validate(fields_data)

    resolved = {}
    if dataclasses.is_dataclass(cls):
        field_types = {f.name: f for f in dataclasses.fields(cls)}
        for key, value in fields_data.items():
            resolved[key] = _from_serializable(value, registry)

        valid_keys = set(field_types.keys())
        resolved = {k: v for k, v in resolved.items() if k in valid_keys}

    return cls(**resolved)

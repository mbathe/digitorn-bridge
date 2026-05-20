"""Runtime configuration registry: introspect, persist, hot-reload."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


_OVERRIDE_PATH = Path.home() / ".digitorn" / "runtime_overrides.yaml"
_SUBSCRIBER_CALLBACK_TIMEOUT = 5.0


SubscriberCallback = Callable[[str, Any], "Any | Awaitable[Any]"]


@dataclass
class FieldSchema:
    """JSON-serialisable description of one config field."""

    key: str
    section: str
    py_type: str
    default: Any
    current: Any
    description: str = ""
    min_value: float | None = None
    max_value: float | None = None
    choices: list[Any] | None = None
    hot_reloadable: bool = False
    sensitive: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.sensitive:
            d["default"] = _mask(d.get("default"))
            d["current"] = _mask(d.get("current"))
        return d


_HOT_RELOADABLE_KEYS: frozenset[str] = frozenset({
    "runtime.max_consecutive_failures",
    "runtime.max_consecutive_failures_hard",
    "runtime.max_repeat_window",
    "runtime.max_repeats",
    "runtime.max_consecutive_same_tool",
    "server.rate_limit_rpm",
    "logging.level",
})

_SENSITIVE_SUFFIXES: tuple[str, ...] = (
    "api_key", "_key", "_secret", "_token", "password",
    "master_key", "jwt_secret",
)


def _mask(v: Any) -> Any:
    if v is None or v == "":
        return v
    s = str(v)
    if len(s) <= 6:
        return "***"
    return f"{s[:2]}***{s[-2:]}"


def _is_sensitive(key: str) -> bool:
    low = key.lower()
    return any(low.endswith(s) for s in _SENSITIVE_SUFFIXES)


class ConfigRegistry:
    """Singleton owning the schema, overrides, and subscriber graph."""

    def __init__(self) -> None:
        self._schema: dict[str, FieldSchema] = {}
        self._overrides: dict[str, Any] = {}
        self._subscribers: dict[str, list[SubscriberCallback]] = {}
        self._write_lock = asyncio.Lock()
        self._loaded = False

    def load(self) -> None:
        """Load overrides from disk and introspect the Settings schema."""
        if self._loaded:
            return
        self._overrides = _read_overrides_sync()
        self._schema = _introspect_settings_safe(self._overrides)
        self._loaded = True
        logger.info(
            "config_registry_loaded fields=%d overrides=%d path=%s",
            len(self._schema), len(self._overrides), _OVERRIDE_PATH,
        )

    def schema(self) -> list[dict[str, Any]]:
        """Return every field as a JSON-safe dict (sensitive values masked)."""
        items = sorted(
            self._schema.values(), key=lambda f: (f.section, f.key),
        )
        return [f.to_dict() for f in items]

    def get(self, key: str) -> Any:
        """Return the current effective value or None if unknown."""
        s = self._schema.get(key)
        if s is None:
            return self._overrides.get(key)
        return s.current

    def overrides(self) -> dict[str, Any]:
        """Return a copy of the override dict (sensitive masked)."""
        out: dict[str, Any] = {}
        for k, v in self._overrides.items():
            out[k] = _mask(v) if _is_sensitive(k) else v
        return out

    async def set(self, key: str, value: Any) -> dict[str, Any]:
        """Set an override, persist to disk, notify subscribers."""
        if not self._loaded:
            raise RuntimeError("config_registry not loaded")

        schema = self._schema.get(key)
        if schema is None:
            return {
                "ok": False,
                "error": f"unknown_key:{key}",
                "hint": "call GET /api/admin/daemon/config to see the schema",
            }

        try:
            coerced = _coerce_value(schema, value)
        except (ValueError, TypeError) as exc:
            return {
                "ok": False,
                "error": "invalid_value",
                "detail": str(exc),
                "expected": schema.py_type,
            }

        prev = schema.current
        self._overrides[key] = coerced
        schema.current = coerced

        async with self._write_lock:
            try:
                await asyncio.to_thread(_write_overrides, dict(self._overrides))
            except Exception as exc:
                self._overrides[key] = prev
                schema.current = prev
                return {
                    "ok": False,
                    "error": "persist_failed",
                    "detail": str(exc),
                }

        await self._notify_subscribers(key, coerced)

        logger.info(
            "config_registry_set key=%s prev=%r new=%r hot_reloadable=%s",
            key,
            _mask(prev) if schema.sensitive else prev,
            _mask(coerced) if schema.sensitive else coerced,
            schema.hot_reloadable,
        )
        return {
            "ok": True,
            "key": key,
            "current": _mask(coerced) if schema.sensitive else coerced,
            "hot_reloadable": schema.hot_reloadable,
            "restart_required": not schema.hot_reloadable,
        }

    async def reset(self, key: str) -> dict[str, Any]:
        """Remove an override, falling back to the upstream source."""
        if not self._loaded:
            raise RuntimeError("config_registry not loaded")
        schema = self._schema.get(key)
        if schema is None:
            return {"ok": False, "error": f"unknown_key:{key}"}
        if key not in self._overrides:
            return {"ok": True, "key": key, "noop": True}
        prev_override = self._overrides.pop(key)
        schema.current = schema.default
        async with self._write_lock:
            try:
                await asyncio.to_thread(_write_overrides, dict(self._overrides))
            except Exception as exc:
                self._overrides[key] = prev_override
                schema.current = prev_override
                return {
                    "ok": False, "error": "persist_failed", "detail": str(exc),
                }
        await self._notify_subscribers(key, schema.default)
        return {"ok": True, "key": key, "reverted_to": schema.default}

    def subscribe(self, key: str, cb: SubscriberCallback) -> None:
        """Register a callback fired when key changes."""
        self._subscribers.setdefault(key, []).append(cb)

    def unsubscribe(self, key: str, cb: SubscriberCallback) -> None:
        lst = self._subscribers.get(key)
        if not lst:
            return
        try:
            lst.remove(cb)
        except ValueError:
            pass

    async def _notify_subscribers(self, key: str, value: Any) -> None:
        callbacks = list(self._subscribers.get(key, []))
        if not callbacks:
            return
        for cb in callbacks:
            asyncio.create_task(
                self._run_subscriber(key, value, cb),
                name=f"config_registry_subscriber:{key}",
            )

    @staticmethod
    async def _run_subscriber(
        key: str, value: Any, cb: SubscriberCallback,
    ) -> None:
        try:
            res = cb(key, value)
            if asyncio.iscoroutine(res):
                await asyncio.wait_for(
                    res, timeout=_SUBSCRIBER_CALLBACK_TIMEOUT,
                )
        except asyncio.TimeoutError:
            logger.warning(
                "config_registry_subscriber_timeout key=%s cb=%s",
                key, getattr(cb, "__name__", repr(cb)),
            )
        except Exception as exc:
            logger.warning(
                "config_registry_subscriber_failed key=%s cb=%s err=%s",
                key, getattr(cb, "__name__", repr(cb)), exc,
            )


def _introspect_settings_safe(
    overrides: dict[str, Any],
) -> dict[str, FieldSchema]:
    out: dict[str, FieldSchema] = {}
    try:
        from digitorn.core.config import Settings
    except Exception as exc:
        logger.warning("config_registry_settings_import_failed: %s", exc)
        return out
    try:
        instance = Settings()  # type: ignore[call-arg]
    except Exception as exc:
        logger.warning("config_registry_settings_init_failed: %s", exc)
        return out
    _walk_model(instance, "", instance.__class__.model_fields, out, overrides)
    return out


def _walk_model(
    instance: Any,
    prefix: str,
    fields_meta: dict[str, Any],
    out: dict[str, FieldSchema],
    overrides: dict[str, Any],
) -> None:
    from pydantic import BaseModel as _PydBaseModel
    for fname, finfo in fields_meta.items():
        key = f"{prefix}{fname}" if not prefix else f"{prefix}.{fname}"
        value = getattr(instance, fname, None)
        section = key.split(".", 1)[0]

        if isinstance(value, _PydBaseModel):
            try:
                child_fields = value.__class__.model_fields
            except Exception:
                child_fields = {}
            _walk_model(value, key, child_fields, out, overrides)
            continue

        py_type, choices = _classify_type(finfo.annotation)
        default = _safe_default(finfo)
        current = overrides.get(key, value)
        min_v, max_v = _extract_bounds(finfo)
        out[key] = FieldSchema(
            key=key,
            section=section,
            py_type=py_type,
            default=default,
            current=current,
            description=str(getattr(finfo, "description", "") or ""),
            min_value=min_v,
            max_value=max_v,
            choices=choices,
            hot_reloadable=key in _HOT_RELOADABLE_KEYS,
            sensitive=_is_sensitive(key),
        )


def _classify_type(
    annotation: Any,
) -> tuple[str, list[Any] | None]:
    import typing
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Literal:
        return "literal", list(args)
    if annotation is bool or annotation == "bool":
        return "bool", None
    if annotation is int or annotation == "int":
        return "int", None
    if annotation is float or annotation == "float":
        return "float", None
    if annotation is str or annotation == "str":
        return "str", None
    if origin in (list, set, tuple):
        return "list", None
    if origin is dict:
        return "dict", None
    if origin is typing.Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _classify_type(non_none[0])
        return "union", None
    return "any", None


def _safe_default(finfo: Any) -> Any:
    d = getattr(finfo, "default", None)
    try:
        from pydantic_core import PydanticUndefined
    except Exception:
        PydanticUndefined = object()
    if d is PydanticUndefined:
        factory = getattr(finfo, "default_factory", None)
        if callable(factory):
            try:
                return factory()
            except Exception:
                return None
        return None
    return d


def _extract_bounds(finfo: Any) -> tuple[float | None, float | None]:
    meta = list(getattr(finfo, "metadata", []) or [])
    lo: float | None = None
    hi: float | None = None
    for m in meta:
        if hasattr(m, "ge") and m.ge is not None:
            lo = float(m.ge)
        if hasattr(m, "gt") and m.gt is not None:
            lo = float(m.gt)
        if hasattr(m, "le") and m.le is not None:
            hi = float(m.le)
        if hasattr(m, "lt") and m.lt is not None:
            hi = float(m.lt)
    return lo, hi


def _coerce_value(schema: FieldSchema, raw: Any) -> Any:
    t = schema.py_type
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            low = raw.strip().lower()
            if low in ("true", "1", "yes", "on"):
                return True
            if low in ("false", "0", "no", "off"):
                return False
        if isinstance(raw, int):
            return bool(raw)
        raise ValueError(f"not a bool: {raw!r}")
    if t == "int":
        try:
            v = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"not an int: {raw!r}") from exc
        if schema.min_value is not None and v < schema.min_value:
            raise ValueError(
                f"below minimum {int(schema.min_value)}",
            )
        if schema.max_value is not None and v > schema.max_value:
            raise ValueError(
                f"above maximum {int(schema.max_value)}",
            )
        return v
    if t == "float":
        try:
            v = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"not a float: {raw!r}") from exc
        if schema.min_value is not None and v < schema.min_value:
            raise ValueError(f"below minimum {schema.min_value}")
        if schema.max_value is not None and v > schema.max_value:
            raise ValueError(f"above maximum {schema.max_value}")
        return v
    if t == "literal":
        if schema.choices and raw not in schema.choices:
            raise ValueError(
                f"not in choices: {schema.choices!r}",
            )
        return raw
    if t == "list":
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            return [p.strip() for p in raw.split(",") if p.strip()]
        raise ValueError(f"not a list: {raw!r}")
    if t == "dict":
        if isinstance(raw, dict):
            return raw
        raise ValueError(f"not a dict: {raw!r}")
    return raw


def _read_overrides_sync() -> dict[str, Any]:
    if not _OVERRIDE_PATH.exists():
        return {}
    try:
        import yaml
    except ImportError:
        logger.warning(
            "config_registry_pyyaml_missing - overrides ignored",
        )
        return {}
    try:
        raw = _OVERRIDE_PATH.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("config_registry_overrides_read_failed: %s", exc)
        return {}
    try:
        data = yaml.safe_load(raw) or {}
    except Exception as exc:
        logger.warning("config_registry_overrides_parse_failed: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_overrides(state: dict[str, Any]) -> None:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("pyyaml not installed") from exc
    _OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _OVERRIDE_PATH.with_suffix(".yaml.tmp")
    text = yaml.safe_dump(state, sort_keys=True, allow_unicode=True)
    header = (
        "# Digitorn daemon runtime overrides.\n"
        "# Written by the admin dashboard. Edit at your own risk.\n"
        f"# Last write: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n\n"
    )
    tmp.write_text(header + text, encoding="utf-8")
    os.replace(tmp, _OVERRIDE_PATH)


_REGISTRY: ConfigRegistry | None = None


def get_registry() -> ConfigRegistry | None:
    return _REGISTRY


def install_config_registry() -> ConfigRegistry:
    """Synchronous installer for the lifespan startup (idempotent)."""
    global _REGISTRY
    if _REGISTRY is not None:
        return _REGISTRY
    reg = ConfigRegistry()
    reg.load()
    _REGISTRY = reg
    return reg


def shutdown_config_registry() -> None:
    global _REGISTRY
    _REGISTRY = None

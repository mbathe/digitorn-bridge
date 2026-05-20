"""Digitorn logging - structlog-style facade over stdlib logging."""

from __future__ import annotations

import logging
from typing import Any

class _LoggerAdapter:
    """Thin facade with a structlog-compatible kwargs API."""

    __slots__ = ("_logger",)

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _format(self, msg: str, kwargs: dict[str, Any]) -> str:
        if not kwargs:
            return msg
        # Stable, readable kv order
        bits = " ".join(f"{k}={v!r}" if isinstance(v, str) and " " in v
                        else f"{k}={v}"
                        for k, v in kwargs.items())
        return f"{msg} {bits}"

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        # `args` carries positional %-format substitutions for legacy
        # call sites; `kwargs` carries structlog-style kv pairs.
        # Pop logging-internal kwargs (exc_info, stack_info, etc.)
        log_kwargs = self._extract_logging_kwargs(kwargs)
        if args and not kwargs:
            self._logger.debug(msg, *args, **log_kwargs)
            return
        self._logger.debug(self._format(msg % args if args else msg, kwargs), **log_kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        log_kwargs = self._extract_logging_kwargs(kwargs)
        if args and not kwargs:
            self._logger.info(msg, *args, **log_kwargs)
            return
        self._logger.info(self._format(msg % args if args else msg, kwargs), **log_kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        log_kwargs = self._extract_logging_kwargs(kwargs)
        if args and not kwargs:
            self._logger.warning(msg, *args, **log_kwargs)
            return
        self._logger.warning(self._format(msg % args if args else msg, kwargs), **log_kwargs)

    # Common alias seen in the codebase
    warn = warning

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        log_kwargs = self._extract_logging_kwargs(kwargs)
        if args and not kwargs:
            self._logger.error(msg, *args, **log_kwargs)
            return
        self._logger.error(self._format(msg % args if args else msg, kwargs), **log_kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        log_kwargs = self._extract_logging_kwargs(kwargs)
        log_kwargs.setdefault("exc_info", True)
        if args and not kwargs:
            self._logger.error(msg, *args, **log_kwargs)
            return
        self._logger.error(self._format(msg % args if args else msg, kwargs), **log_kwargs)

    def critical(self, msg: str, *args: Any, **kwargs: Any) -> None:
        log_kwargs = self._extract_logging_kwargs(kwargs)
        if args and not kwargs:
            self._logger.critical(msg, *args, **log_kwargs)
            return
        self._logger.critical(self._format(msg % args if args else msg, kwargs), **log_kwargs)

    @staticmethod
    def _extract_logging_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for k in ("exc_info", "stack_info", "stacklevel", "extra"):
            if k in kwargs:
                result[k] = kwargs.pop(k)
        return result

    # Pass-through for everything else the underlying logger supports
    # (isEnabledFor, setLevel, addHandler, etc.) so existing code that
    # introspects the logger keeps working.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._logger, name)

def get_logger(name: str) -> _LoggerAdapter:
    """Return a structlog-style adapter wrapping a stdlib logger."""
    return _LoggerAdapter(logging.getLogger(name))

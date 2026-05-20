"""LLMOS Bridge - Structured logging configuration."""

from __future__ import annotations

import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger

# Context variables - automatically injected into log records when set.
_ctx_plan_id: ContextVar[str | None] = ContextVar("plan_id", default=None)
_ctx_action_id: ContextVar[str | None] = ContextVar("action_id", default=None)
_ctx_session_id: ContextVar[str | None] = ContextVar("session_id", default=None)

def bind_plan_context(
    plan_id: str | None = None,
    action_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Bind execution context to the current async task / thread."""
    if plan_id is not None:
        _ctx_plan_id.set(plan_id)
    if action_id is not None:
        _ctx_action_id.set(action_id)
    if session_id is not None:
        _ctx_session_id.set(session_id)

def clear_plan_context() -> None:
    _ctx_plan_id.set(None)
    _ctx_action_id.set(None)
    _ctx_session_id.set(None)

def _inject_context_vars(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    if (plan_id := _ctx_plan_id.get()) is not None:
        event_dict["plan_id"] = plan_id
    if (action_id := _ctx_action_id.get()) is not None:
        event_dict["action_id"] = action_id
    if (session_id := _ctx_session_id.get()) is not None:
        event_dict["session_id"] = session_id
    return event_dict

def _drop_color_message(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    event_dict.pop("color_message", None)
    return event_dict

# Patterns that look like secrets - match value portions after key= in structured logs
_SECRET_PATTERNS = re.compile(
    r"(?i)"
    r"(?:"
    r"(?:api[_-]?key|token|secret|password|passwd|authorization|bearer|credential)"
    r"\s*[=:]\s*"
    r")"
    r"(['\"]?)(\S{6,})\1",
)

# Direct value patterns (tokens that are recognizable by prefix)
_SECRET_VALUE_RE = re.compile(
    r"(?:"
    r"sk-[a-zA-Z0-9]{20,}"        # OpenAI-style
    r"|xoxb-[a-zA-Z0-9\-]{20,}"   # Slack bot
    r"|xoxp-[a-zA-Z0-9\-]{20,}"   # Slack user
    r"|ghp_[a-zA-Z0-9]{30,}"      # GitHub PAT
    r"|ghs_[a-zA-Z0-9]{30,}"      # GitHub app
    r"|glpat-[a-zA-Z0-9\-]{20,}"  # GitLab PAT
    r"|eyJ[a-zA-Z0-9_\-]{20,}"    # JWT (base64 "eyJ...")
    r")"
)

_REDACTED = "***REDACTED***"

def _redact_string(value: str) -> str:
    result = _SECRET_PATTERNS.sub(
        lambda m: m.group(0).replace(m.group(2), _REDACTED), value
    )
    result = _SECRET_VALUE_RE.sub(_REDACTED, result)
    return result

def _redact_secrets(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = _redact_string(value)
    return event_dict

def configure_logging(
    level: str = "info",
    format: str = "console",
    log_file: str | None = None,
) -> None:
    """Configure structlog and stdlib logging."""
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_context_vars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        _drop_color_message,
        _redact_secrets,
    ]

    if format == "json":
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handlers: list[logging.Handler] = []

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    handlers.append(stream_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    root_logger = logging.getLogger()
    root_logger.handlers = handlers
    root_logger.setLevel(level.upper())

    # Silence noisy third-party loggers.
    for noisy in ("uvicorn.access", "httpx", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger for *name*."""
    return structlog.get_logger(name)

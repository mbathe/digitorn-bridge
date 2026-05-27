"""Per-task marker that distinguishes hook/setup callers from LLM callers."""
from __future__ import annotations

import contextvars

_internal_call: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "digitorn_internal_call", default=False,
)


def is_internal_call() -> bool:
    return _internal_call.get()


class internal_call_scope:
    __slots__ = ("_token",)

    def __enter__(self) -> "internal_call_scope":
        self._token = _internal_call.set(True)
        return self

    def __exit__(self, *_exc: object) -> None:
        _internal_call.reset(self._token)

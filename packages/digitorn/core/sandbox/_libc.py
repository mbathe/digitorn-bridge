"""Shared libc loader; avoids tempfile creation in find_library (breaks under Landlock)."""

from __future__ import annotations

import ctypes
import functools


@functools.cache
def get_libc() -> ctypes.CDLL:
    """Get a cached libc handle without creating temp files."""
    for path in (
        "/lib/x86_64-linux-gnu/libc.so.6",
        "/lib64/libc.so.6",
        "/lib/aarch64-linux-gnu/libc.so.6",
    ):
        try:
            return ctypes.CDLL(path, use_errno=True)
        except OSError:
            continue
    return ctypes.CDLL(None, use_errno=True)

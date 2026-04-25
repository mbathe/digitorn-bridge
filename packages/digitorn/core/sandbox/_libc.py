"""Shared libc loader — avoids tempfile creation in find_library.

ctypes.util.find_library("c") internally creates a temp file in /tmp,
which fails after Landlock restricts filesystem access. This module
loads libc directly by known paths, falling back to CDLL(None).
"""

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
    # Fallback: CDLL(None) loads the already-linked libc
    return ctypes.CDLL(None, use_errno=True)

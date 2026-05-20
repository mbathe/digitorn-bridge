"""Digitorn TUI - pure client terminal interface."""

from __future__ import annotations


import logging

logger = logging.getLogger(__name__)
from pathlib import Path
from typing import Any


def launch_tui(
    *,
    daemon_url: str = "http://127.0.0.1:8000",
    app_id: str | None = None,
    app_path: Path | str | None = None,
    session_id: str | None = None,
    auth_headers: dict[str, str] | None = None,
    initial_message: str | None = None,
    exit_on_complete: bool = False,
) -> None:
    """Launch the Textual TUI as a daemon client.

    Args:
        daemon_url: Daemon HTTP base URL.
        app_id: Deployed app ID (mutually exclusive with app_path).
        app_path: YAML file to auto-deploy then connect.
        session_id: Resume an existing session.
        auth_headers: Pre-resolved auth headers.
        initial_message: Auto-send this message on startup.
        exit_on_complete: Exit after the first turn completes.
    """
    from .app import DigitornTUI
    from .backends.daemon import DaemonBackend

    if app_id is None and app_path is None:
        raise ValueError("Either app_id or app_path is required")

    backend = DaemonBackend(
        daemon_url=daemon_url,
        app_id=app_id or "",
        session_id=session_id,
        app_path=Path(app_path) if app_path else None,
        auth_headers=auth_headers,
    )

    tui = DigitornTUI(
        backend=backend,
        initial_message=initial_message,
        exit_on_complete=exit_on_complete,
    )
    try:
        tui.run()
    finally:
        # ALWAYS reset terminal - prevents mouse tracking garbage
        import sys
        try:
            sys.stdout.write(
                "\033[?1000l\033[?1003l\033[?1006l\033[?1015l\033[?25h"
            )
            sys.stdout.flush()
        except Exception as exc:
            logger.debug("__init__ best-effort block failed: %s", exc)
        # Stop SSE thread + close HTTP client
        stop = getattr(backend, "_event_stop", None)
        if stop:
            stop.set()
        try:
            backend._http.close()
        except Exception as exc:
            logger.debug("__init__ best-effort block failed: %s", exc)

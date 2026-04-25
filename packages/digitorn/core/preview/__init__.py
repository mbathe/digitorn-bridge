"""App preview subsystem — dev servers managed by the daemon.

The :class:`PreviewManager` spawns the ``preview.command`` declared in an
app's YAML on deploy, supervises it, and tears it down on undeploy. A
future reverse-proxy route forwards HTTP + WebSocket traffic to
``localhost:{port}`` so Flutter clients can open an iframe on
``/api/apps/{app_id}/preview/dev/*``.
"""

from digitorn.core.preview.manager import (
    PreviewManager,
    PreviewState,
    PreviewStatus,
)

__all__ = ["PreviewManager", "PreviewState", "PreviewStatus"]

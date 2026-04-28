"""UI metadata API - client rendering hints (icons, channels, fallbacks).

Single endpoint so far: ``GET /api/ui/tool_display_defaults``.

The client fetches this once at boot and uses it as a lookup table
for tool_call events that don't carry a full ``display`` block
(legacy daemons, third-party modules, older event log entries).

Every tool_call / tool_start SSE event already carries a
``data.display`` object built server-side - the client should
read that first and only consult this catalog for the icon
name → concrete-icon mapping and the channel → side-panel
mapping. The ``fallbacks.patterns`` array is a safety net for
tools with no display block at all.
"""

from __future__ import annotations

from fastapi import APIRouter

from digitorn.core.runtime.tool_display import DISPLAY_DEFAULTS

router = APIRouter(prefix="/api/ui", tags=["ui"])


@router.get("/tool_display_defaults")
async def tool_display_defaults() -> dict:
    """Return the static display catalog (icons, channels, fallbacks)."""
    return DISPLAY_DEFAULTS

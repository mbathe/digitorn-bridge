"""UI metadata API - client rendering hints (icons, channels, fallbacks)."""

from __future__ import annotations

from fastapi import APIRouter

from digitorn.core.runtime.tool_display import DISPLAY_DEFAULTS

router = APIRouter(prefix="/api/ui", tags=["ui"])


@router.get("/tool_display_defaults")
async def tool_display_defaults() -> dict:
    """Return the static display catalog (icons, channels, fallbacks)."""
    return DISPLAY_DEFAULTS

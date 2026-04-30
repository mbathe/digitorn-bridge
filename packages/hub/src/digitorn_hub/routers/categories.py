"""GET /api/v1/categories - the canonical category catalogue.

Every client (web, Flutter, future SDKs) hydrates its chip filters and
gradient palettes from this endpoint. There's no parameter, no auth, no
caching surprise: the same payload is served to everyone, callers
cache it client-side (24 h is fine, the list moves slowly).
"""
from __future__ import annotations

from fastapi import APIRouter

from ..catalog import CategoryOut, all_categories

router = APIRouter(prefix="/categories", tags=["catalog"])


@router.get("", response_model=list[CategoryOut])
async def list_categories() -> list[CategoryOut]:
    return all_categories()

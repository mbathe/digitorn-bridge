"""Web module - parameter models. Hidden params reduce LLM confusion."""

from __future__ import annotations

from pydantic import BaseModel, Field

_HIDDEN = {"hidden": True}

class SearchParams(BaseModel):
    """Search the web."""

    query: str = Field(..., min_length=2, description="Search query.")
    limit: int = Field(default=5, ge=1, le=20, json_schema_extra=_HIDDEN)
    allowed_domains: list[str] | None = Field(None, json_schema_extra=_HIDDEN)
    blocked_domains: list[str] | None = Field(None, json_schema_extra=_HIDDEN)

class FetchParams(BaseModel):
    """Fetch a web page and return its content as text."""

    url: str = Field(..., description="URL to fetch.")
    prompt: str = Field(default="", description="What to extract from the page. Leave empty for full page content.")
    extract: bool = Field(default=False, description="Extract main content only (removes nav, ads, etc). Default: false.")
    max_length: int = Field(default=50000, json_schema_extra=_HIDDEN)
    raw: bool = Field(default=False, json_schema_extra=_HIDDEN)
    format: str = Field(default="text", json_schema_extra=_HIDDEN)

class ExtractParams(BaseModel):
    """Extract content from a web page using CSS selector."""

    url: str = Field(..., description="URL to extract from.")
    selector: str = Field(default="main, article, .content, #content, body", json_schema_extra=_HIDDEN)
    max_length: int = Field(default=30000, json_schema_extra=_HIDDEN)

class DownloadParams(BaseModel):
    """Download a file from a URL."""

    url: str = Field(..., description="URL to download.")
    path: str = Field(..., description="Local file path to save to.")

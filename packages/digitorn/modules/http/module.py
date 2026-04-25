"""HTTP module — agent-optimized HTTP client with background downloads.

Provides full HTTP capabilities to AI agents: API calls, web scraping,
file downloads with progress tracking, and form submissions.

Security layers:
  - SSRF protection: blocks private/reserved IPs before every request.
  - URL allowlist/blocklist: configurable per-app in YAML constraints.
  - Sensitive header masking: Authorization, Cookie, API keys never returned raw.
  - Response size cap: prevents memory exhaustion from huge responses.
  - TLS verification: enabled by default, configurable override requires allow_insecure_tls.
  - Timeout enforcement on every request.
  - Full audit log: every request recorded with URL, method, status, timestamp.

Background downloads:
  - download:          start a streaming download, get a download_id back.
  - download_status:   check progress (bytes, speed, ETA, percentage).
  - download_cancel:   cancel and clean up partial file.
  - download_list:     list all downloads with status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field as PydanticField

from digitorn.modules.base import ActionResult, BaseModule, Platform, ResourceEstimate
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ConstraintSpec, ModuleManifest

from .params import (
    DeleteParams,
    DownloadIdParams,
    DownloadListParams,
    DownloadParams,
    FetchPageParams,
    GetParams,
    HeadParams,
    JsonApiParams,
    OptionsParams,
    PatchParams,
    PostParams,
    PutParams,
    RequestParams,
    SubmitFormParams,
    UploadFileParams,
)
from .security import format_bytes, mask_headers, validate_url

logger = logging.getLogger(__name__)


@dataclass
class DownloadTask:
    """Tracks a background file download."""

    download_id: str
    url: str
    destination: str
    started_at: str
    total_bytes: int | None = None
    downloaded_bytes: int = 0
    speed_bytes_per_sec: float = 0.0
    status: str = "downloading"
    error: str | None = None
    finished_at: str | None = None
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _last_speed_check: float = field(default=0.0, repr=False)
    _last_speed_bytes: int = field(default=0, repr=False)

    @property
    def elapsed_seconds(self) -> float:
        start = datetime.fromisoformat(self.started_at)
        if self.finished_at:
            end = datetime.fromisoformat(self.finished_at)
        else:
            end = datetime.now(timezone.utc)
        return (end - start).total_seconds()

    @property
    def percent(self) -> float | None:
        if self.total_bytes and self.total_bytes > 0:
            return round(self.downloaded_bytes / self.total_bytes * 100, 1)
        return None

    @property
    def eta_seconds(self) -> float | None:
        if (
            self.speed_bytes_per_sec > 0
            and self.total_bytes
            and self.total_bytes > self.downloaded_bytes
        ):
            remaining = self.total_bytes - self.downloaded_bytes
            return round(remaining / self.speed_bytes_per_sec, 1)
        return None

    def to_summary(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "download_id": self.download_id,
            "url": self.url,
            "destination": self.destination,
            "status": self.status,
            "downloaded_bytes": self.downloaded_bytes,
            "downloaded_human": format_bytes(self.downloaded_bytes),
            "total_bytes": self.total_bytes,
            "total_human": format_bytes(self.total_bytes) if self.total_bytes else None,
            "percent": self.percent,
            "speed_bytes_per_sec": round(self.speed_bytes_per_sec),
            "speed_human": f"{format_bytes(int(self.speed_bytes_per_sec))}/s",
            "eta_seconds": self.eta_seconds,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "error": self.error,
        }
        if self.status == "downloading":
            pct = f" ({self.percent}%)" if self.percent is not None else ""
            data["hint"] = (
                f"Downloading{pct} at {data['speed_human']}. "
                + (f"ETA: {self.eta_seconds}s. " if self.eta_seconds else "")
                + "Use download_status to check again or download_cancel to stop."
            )
        elif self.status == "completed":
            data["hint"] = (
                f"Download completed: {data['downloaded_human']} "
                f"in {data['elapsed_seconds']}s."
            )
        elif self.status == "failed":
            data["hint"] = f"Download failed: {self.error}"
        elif self.status == "cancelled":
            data["hint"] = "Download was cancelled."
        return data


def _audit(
    action_name: str,
    method: str,
    url: str,
    status_code: int | None,
    error: str | None,
) -> None:
    logger.info(
        "http_audit action=%s method=%s url=%r status=%s error=%s ts=%s",
        action_name, method, url, status_code, error,
        datetime.now(timezone.utc).isoformat(),
    )


_STATUS_HINTS: dict[int, str] = {
    200: "Success.",
    201: "Resource created successfully.",
    202: "Request accepted, processing in progress.",
    204: "Success, no content returned.",
    301: "Permanently moved.",
    302: "Temporarily redirected.",
    304: "Not modified (cached version is current).",
    307: "Temporarily redirected (same method).",
    308: "Permanently redirected (same method).",
    400: "Bad request. Check your parameters.",
    401: "Authentication required. Provide credentials.",
    403: "Forbidden. The server rejected your request.",
    404: "Not found. Check the URL.",
    405: "Method not allowed for this URL.",
    409: "Conflict. The resource state prevents this operation.",
    413: "Payload too large.",
    422: "Unprocessable entity. Check the request body format.",
    429: "Rate limited. Wait before retrying.",
    500: "Internal server error. Try again later.",
    502: "Bad gateway. The upstream server is down.",
    503: "Service unavailable. Try again later.",
    504: "Gateway timeout. The upstream server is slow.",
}


def _status_hint(code: int) -> str:
    if code in _STATUS_HINTS:
        return _STATUS_HINTS[code]
    if 200 <= code < 300:
        return "Success."
    if 300 <= code < 400:
        return "Redirect."
    if 400 <= code < 500:
        return "Client error."
    if 500 <= code < 600:
        return "Server error."
    return f"Unexpected status code {code}."


_STRIP_TAGS_RE = re.compile(
    r"<(script|style|nav|header|footer|noscript|iframe)[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_HTML_ENTITY_MAP = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&apos;": "'", "&nbsp;": " ",
}
_ENTITY_RE = re.compile(r"&#(\d+);")
_BLOCK_TAGS = re.compile(
    r"</(p|div|br|h[1-6]|li|tr|td|th|blockquote|pre|section|article)>",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_LINK_RE = re.compile(
    r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)


def _extract_text(html: str, max_length: int = 50_000) -> str:
    """Extract readable text from HTML."""
    text = _STRIP_TAGS_RE.sub("", html)
    text = _BLOCK_TAGS.sub(r"\n", text)
    text = _TAG_RE.sub("", text)
    for entity, char in _HTML_ENTITY_MAP.items():
        text = text.replace(entity, char)
    text = _ENTITY_RE.sub(lambda m: chr(int(m.group(1))), text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    if len(text) > max_length:
        text = text[:max_length] + f"\n\n[Truncated at {max_length} characters]"
    return text


def _extract_title(html: str) -> str | None:
    m = _TITLE_RE.search(html)
    return _TAG_RE.sub("", m.group(1)).strip() if m else None


def _extract_links(html: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for href, text in _LINK_RE.findall(html):
        clean_text = _TAG_RE.sub("", text).strip()
        if href and not href.startswith(("#", "javascript:")):
            links.append({"href": href, "text": clean_text or href})
    return links[:100]


class HttpConfig(BaseModel):
    """Pydantic config for the HTTP module (validated at compile time)."""

    model_config = {"extra": "forbid"}

    workspace: str = PydanticField(
        default="",
        description=(
            "Auto-injected by the daemon at module init time. "
            "Do NOT set manually in YAML — the daemon resolves it from "
            "the app's workspace/workspace_mode config."
        ),
    )
    timeout: int = PydanticField(30, ge=1, le=300, description="Default request timeout in seconds")
    max_response_size: int = PydanticField(10_000_000, ge=1000, le=500_000_000, description="Maximum response body size")
    allow_insecure_tls: bool = PydanticField(False, description="Allow HTTPS requests without certificate verification")
    user_agent: str | None = PydanticField(None, description="Custom User-Agent header")


class HttpModule(BaseModule):
    MODULE_ID = "http"
    VERSION = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.LINUX, Platform.MACOS, Platform.WINDOWS]
    CONFIG_MODEL = HttpConfig

    CONSTRAINTS = [
        ConstraintSpec(name="allowed_hosts", type="string_list", description="Allowed HTTP hosts for write requests (POST/PUT/DELETE).", scope="universal"),
        ConstraintSpec(name="blocked_hosts", type="string_list", description="Blocked HTTP hosts for all requests.", scope="universal"),
    ]

    def get_prompt_sections(self) -> list[dict[str, Any]]:
        return []  # Actions are self-descriptive via schema


    def __init__(self) -> None:
        super().__init__()
        self._downloads: dict[str, DownloadTask] = {}
        self._allowed_hosts: list[str] = []
        self._blocked_hosts: list[str] = []
        self._allow_insecure_tls: bool = False
        self._has_security_profile: bool = False

    async def on_start(self) -> None:
        constraints = getattr(self, "_constraints", {}) or {}
        self._allowed_hosts = constraints.get("allowed_hosts", [])
        self._blocked_hosts = constraints.get("blocked_hosts", [])
        self._allow_insecure_tls = constraints.get("allow_insecure_tls", False)

    async def on_stop(self) -> None:
        """Cancel all active downloads."""
        for dl in list(self._downloads.values()):
            if dl.status == "downloading" and dl._task and not dl._task.done():
                dl._task.cancel()
                dl.status = "cancelled"
                dl.finished_at = datetime.now(timezone.utc).isoformat()
        self._downloads.clear()

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(
            update={
                "description": (
                    "HTTP client — make API calls, fetch web pages, download files, "
                    "submit forms, and upload files. Background downloads with progress "
                    "tracking for large files. SSRF-protected, TLS-verified by default."
                ),
            }
        )

    def get_context_snippet(self) -> str | None:
        parts = ["HTTP client ready."]
        if self._allowed_hosts:
            parts.append(f"Allowed hosts: {', '.join(self._allowed_hosts)}")
        if self._blocked_hosts:
            parts.append(f"Blocked hosts: {', '.join(self._blocked_hosts)}")
        active = sum(1 for d in self._downloads.values() if d.status == "downloading")
        if active:
            parts.append(f"Active downloads: {active}")
        return "\n".join(parts)


    def _validate_url(self, url: str) -> tuple[str | None, str]:
        """Validate URL. Returns (error, pinned_url).

        The pinned_url replaces the hostname with the resolved IP
        to prevent DNS rebinding attacks (TOCTOU between validation
        and actual connection).
        """
        from .security import ValidatedURL
        error, _, validated = validate_url(
            url,
            allowed_hosts=self._allowed_hosts or None,
            blocked_hosts=self._blocked_hosts or None,
        )
        if error:
            return error, url
        pinned = validated.pinned_url if validated else url
        return None, pinned

    def _check_tls(self, verify_tls: bool) -> tuple[bool, str | None]:
        """Check TLS setting against policy. Returns (effective_verify, error)."""
        if not verify_tls and not self._allow_insecure_tls:
            return True, None
        return verify_tls, None

    async def _do_request(
        self,
        action_name: str,
        *,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: str | None = None,
        json_body: dict | list | None = None,
        query_params: dict[str, str] | None = None,
        timeout: float = 30.0,
        follow_redirects: bool = True,
        max_redirects: int = 10,
        verify_tls: bool = True,
        max_response_bytes: int = 5_000_000,
        form_data: dict[str, str] | None = None,
    ) -> ActionResult:
        """Shared request logic for all HTTP actions."""
        url_error, pinned_url = self._validate_url(url)
        if url_error:
            _audit(action_name, method, url, None, url_error)
            return ActionResult(success=False, error=url_error)

        _WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
        if method.upper() in _WRITE_METHODS and not self._allowed_hosts:
            if self._has_security_profile:
                from urllib.parse import urlparse
                _parsed = urlparse(url)
                _host = (_parsed.hostname or "").lower()
                _SAFE = {"localhost", "127.0.0.1", "::1"}
                if _host not in _SAFE:
                    _audit(action_name, method, url, None, "blocked by egress policy")
                    return ActionResult(
                        success=False,
                        error=(
                            f"HTTP {method} to external host '{_host}' is blocked — "
                            f"no allowed_hosts constraint configured. "
                            f"Add modules.http.constraints.allowed_hosts: ['{_host}'] "
                            f"in your YAML to allow write requests to this host."
                        ),
                    )

        effective_tls, _ = self._check_tls(verify_tls)

        req_headers = dict(headers or {})
        if "User-Agent" not in req_headers:
            req_headers["User-Agent"] = "Digitorn-HTTP/1.0"

        # DNS rebinding protection: use pinned URL (resolved IP) for the
        # actual request.  Set Host header to original hostname for TLS SNI
        # and virtual host routing.
        from urllib.parse import urlparse as _urlparse
        _original_parsed = _urlparse(url)
        _original_host = _original_parsed.hostname or ""
        if pinned_url != url and "Host" not in req_headers:
            port_suffix = f":{_original_parsed.port}" if _original_parsed.port else ""
            req_headers["Host"] = f"{_original_host}{port_suffix}"

        try:
            async with httpx.AsyncClient(
                verify=effective_tls,
                follow_redirects=follow_redirects,
                max_redirects=max_redirects,
                timeout=httpx.Timeout(timeout),
            ) as client:
                content = None
                json_data = None
                data = None

                if json_body is not None:
                    json_data = json_body
                elif form_data is not None:
                    data = form_data
                elif body is not None:
                    content = body.encode("utf-8")

                response = await client.request(
                    method.upper(),
                    pinned_url,
                    headers=req_headers,
                    params=query_params,
                    content=content,
                    json=json_data,
                    data=data,
                )

        except httpx.TimeoutException:
            _audit(action_name, method, url, None, "timeout")
            return ActionResult(success=False, error=f"Request timed out after {timeout}s.")
        except httpx.ConnectError as exc:
            _audit(action_name, method, url, None, str(exc))
            return ActionResult(success=False, error=f"Connection failed: {exc}")
        except httpx.TooManyRedirects:
            _audit(action_name, method, url, None, "too_many_redirects")
            return ActionResult(success=False, error=f"Too many redirects (max {max_redirects}).")
        except Exception as exc:
            _audit(action_name, method, url, None, str(exc))
            return ActionResult(success=False, error=f"HTTP error: {exc}")

        status = response.status_code
        resp_headers = dict(response.headers)
        content_type = response.headers.get("content-type", "")

        raw_body = response.content
        truncated = False
        if len(raw_body) > max_response_bytes:
            raw_body = raw_body[:max_response_bytes]
            truncated = True

        parsed_body: Any
        if "application/json" in content_type:
            try:
                parsed_body = json.loads(raw_body)
            except (json.JSONDecodeError, ValueError):
                parsed_body = raw_body.decode("utf-8", errors="replace")
        elif "text/" in content_type or "xml" in content_type:
            parsed_body = raw_body.decode("utf-8", errors="replace")
        elif len(raw_body) == 0:
            parsed_body = None
        else:
            parsed_body = None

        redirect_chain = [str(r.url) for r in response.history] if response.history else []

        result_data: dict[str, Any] = {
            "url": str(response.url),
            "method": method.upper(),
            "status_code": status,
            "status_text": response.reason_phrase or "",
            "headers": mask_headers(resp_headers),
            "content_type": content_type,
            "size_bytes": len(response.content),
            "hint": _status_hint(status),
        }

        if parsed_body is not None:
            result_data["body"] = parsed_body
        elif len(raw_body) > 0:
            result_data["body_info"] = (
                f"Binary content ({format_bytes(len(response.content))}). "
                "Use download() to save to disk."
            )

        if truncated:
            result_data["truncated"] = True
            result_data["hint"] += (
                f" Response truncated: showing {format_bytes(max_response_bytes)} "
                f"of {format_bytes(len(response.content))}. Use download() for full content."
            )

        if redirect_chain:
            result_data["redirect_chain"] = redirect_chain

        _audit(action_name, method, url, status, None if status < 400 else f"status_{status}")

        return ActionResult(
            success=status < 400,
            data=result_data,
            error=None if status < 400 else f"HTTP {status}: {_status_hint(status)}",
        )


    @action(
        description=(
            "Make an HTTP request with full control over method, headers, body, "
            "query params, and authentication. Universal action for any HTTP call."
        ),
        params_model=RequestParams,
        permissions=["net.http"],
        risk_level="medium",
        data_classification="internal",
        side_effects=["network_request"],
        tags=["http", "request", "api"],
        aliases=["requete", "requete_http", "fetch", "curl", "appel_api"],
        cli_label='Request',
        cli_param='url',
        cli_show_output=True,
        cli_output_lines=3,
    )
    async def request(self, params: RequestParams) -> ActionResult:
        return await self._do_request(
            "request",
            method=params.method,
            url=params.url,
            headers=params.headers,
            body=params.body,
            json_body=params.json_body,
            query_params=params.query_params,
            timeout=params.timeout,
            follow_redirects=params.follow_redirects,
            max_redirects=params.max_redirects,
            verify_tls=params.verify_tls,
            max_response_bytes=params.max_response_bytes,
        )

    @action(
        description="HTTP GET — fetch a URL and auto-parse the response based on content type (JSON, text, HTML).",
        params_model=GetParams,
        permissions=["net.http"],
        risk_level="low",
        data_classification="internal Example: get(url='https://api.example.com/users')",
        side_effects=["network_request"],
        tags=["http", "read", "api"],
        aliases=["obtenir", "recuperer", "telecharger_page", "fetch_url"],
        cli_label='GET',
        cli_param='url',
        cli_show_output=True,
        cli_output_lines=3,
    )
    async def get(self, params: GetParams) -> ActionResult:
        return await self._do_request(
            "get",
            method="GET",
            url=params.url,
            headers=params.headers,
            query_params=params.query_params,
            timeout=params.timeout,
            verify_tls=params.verify_tls,
            max_response_bytes=params.max_response_bytes,
        )

    @action(
        description="HTTP POST — send data to a URL with automatic JSON serialization.",
        params_model=PostParams,
        permissions=["net.http"],
        risk_level="medium",
        data_classification="internal Example: post(url='https://api.example.com/users', data={'name': 'Alice'})",
        side_effects=["network_request", "external_api"],
        tags=["http", "write", "api"],
        aliases=["envoyer", "poster", "soumettre", "api_post"],
        cli_label='POST',
        cli_param='url',
        cli_show_output=True,
        cli_output_lines=3,
    )
    async def post(self, params: PostParams) -> ActionResult:
        return await self._do_request(
            "post",
            method="POST",
            url=params.url,
            headers=params.headers,
            body=params.body,
            json_body=params.json_body,
            timeout=params.timeout,
            verify_tls=params.verify_tls,
        )

    @action(
        description="HTTP PUT — replace a resource at the target URL.",
        params_model=PutParams,
        permissions=["net.http"],
        risk_level="medium",
        data_classification="internal Example: put(url='https://api.example.com/users/1', data={'name': 'Bob'})",
        side_effects=["network_request", "external_api"],
        tags=["http", "write", "api"],
        aliases=["remplacer", "mettre_a_jour", "api_put"],
        cli_label='PUT',
        cli_param='url',
    )
    async def put(self, params: PutParams) -> ActionResult:
        return await self._do_request(
            "put",
            method="PUT",
            url=params.url,
            headers=params.headers,
            body=params.body,
            json_body=params.json_body,
            timeout=params.timeout,
            verify_tls=params.verify_tls,
        )

    @action(
        description="HTTP PATCH — partially update a resource at the target URL.",
        params_model=PatchParams,
        permissions=["net.http"],
        risk_level="medium",
        data_classification="internal",
        side_effects=["network_request", "external_api"],
        tags=["http", "write", "api"],
        aliases=["modifier_partiel", "api_patch"],
        cli_label='PATCH',
        cli_param='url',
    )
    async def patch(self, params: PatchParams) -> ActionResult:
        return await self._do_request(
            "patch",
            method="PATCH",
            url=params.url,
            headers=params.headers,
            body=params.body,
            json_body=params.json_body,
            timeout=params.timeout,
            verify_tls=params.verify_tls,
        )

    @action(
        description="HTTP DELETE — remove a resource at the target URL.",
        params_model=DeleteParams,
        permissions=["net.http"],
        risk_level="medium",
        irreversible=True,
        data_classification="internal Example: delete(url='https://api.example.com/users/1')",
        side_effects=["network_request", "external_api"],
        tags=["http", "delete", "api"],
        aliases=["supprimer_distant", "api_delete", "effacer_ressource"],
        cli_label='DELETE',
        cli_param='url',
    )
    async def delete(self, params: DeleteParams) -> ActionResult:
        return await self._do_request(
            "delete",
            method="DELETE",
            url=params.url,
            headers=params.headers,
            query_params=params.query_params,
            timeout=params.timeout,
            verify_tls=params.verify_tls,
        )

    @action(
        description=(
            "HTTP HEAD — retrieve response headers without downloading the body. "
            "Useful for checking if a URL exists, getting content size, or last-modified timestamps."
        ),
        params_model=HeadParams,
        permissions=["net.http"],
        risk_level="low",
        data_classification="internal",
        side_effects=["network_request"],
        tags=["http", "read", "metadata"],
        aliases=["en_tete", "verifier_url", "check_url"],
        cli_label='HEAD',
        cli_param='url',
    )
    async def head(self, params: HeadParams) -> ActionResult:
        return await self._do_request(
            "head",
            method="HEAD",
            url=params.url,
            headers=params.headers,
            timeout=params.timeout,
            verify_tls=params.verify_tls,
            max_response_bytes=0,
        )

    @action(
        description="HTTP OPTIONS — discover allowed methods and CORS configuration for a URL.",
        params_model=OptionsParams,
        permissions=["net.http"],
        risk_level="low",
        data_classification="internal",
        side_effects=["network_request"],
        tags=["http", "read", "metadata"],
        aliases=["methodes_autorisees", "cors_check"],
        cli_label='OPTIONS',
        cli_param='url',
    )
    async def options(self, params: OptionsParams) -> ActionResult:
        return await self._do_request(
            "options",
            method="OPTIONS",
            url=params.url,
            headers=params.headers,
            timeout=params.timeout,
            verify_tls=params.verify_tls,
            max_response_bytes=0,
        )


    @action(
        description=(
            "Call a JSON API endpoint. Auto-sends Accept: application/json, "
            "parses JSON response, supports Bearer token auth. "
            "The easiest way to call REST APIs."
        ),
        params_model=JsonApiParams,
        permissions=["net.http"],
        risk_level="medium",
        data_classification="internal Example: json_api(url='https://api.example.com/data', method='POST', data={'key': 'value'})",
        side_effects=["network_request", "external_api"],
        tags=["http", "api", "json"],
        aliases=["api_json", "appel_api_json", "rest_api", "api_call"],
        cli_label='API',
        cli_param='url',
        cli_show_output=True,
        cli_output_lines=3,
    )
    async def json_api(self, params: JsonApiParams) -> ActionResult:
        headers = dict(params.headers or {})
        headers.setdefault("Accept", "application/json")
        if params.auth_bearer:
            headers["Authorization"] = f"Bearer {params.auth_bearer}"
        return await self._do_request(
            "json_api",
            method=params.method,
            url=params.url,
            headers=headers,
            json_body=params.data,
            query_params=params.query_params,
            timeout=params.timeout,
            verify_tls=params.verify_tls,
        )

    @action(
        description="Submit an HTML form (application/x-www-form-urlencoded). Auto-encodes key-value pairs.",
        params_model=SubmitFormParams,
        permissions=["net.http"],
        risk_level="medium",
        data_classification="internal",
        side_effects=["network_request", "external_api"],
        tags=["http", "form", "submit"],
        aliases=["formulaire", "soumettre_formulaire", "form_post"],
        cli_label="Submit form",
        cli_param="url",
    )
    async def submit_form(self, params: SubmitFormParams) -> ActionResult:
        return await self._do_request(
            "submit_form",
            method=params.method,
            url=params.url,
            headers=params.headers,
            form_data=params.fields,
            timeout=params.timeout,
            verify_tls=params.verify_tls,
        )

    @action(
        description=(
            "Upload a file via multipart/form-data POST. "
            "The file must exist on the local filesystem."
        ),
        params_model=UploadFileParams,
        permissions=["net.http", "fs.read"],
        risk_level="medium",
        data_classification="internal",
        side_effects=["network_request", "external_api"],
        tags=["http", "upload", "file"],
        aliases=["telecharger_vers", "envoyer_fichier", "upload"],
        cli_label="Upload",
        cli_param="url",
    )
    async def upload_file(self, params: UploadFileParams) -> ActionResult:
        url_error, _pinned = self._validate_url(params.url)
        if url_error:
            return ActionResult(success=False, error=url_error)

        path = Path(params.file_path).resolve()
        if not path.exists():
            return ActionResult(success=False, error=f"File not found: {path}")
        if not path.is_file():
            return ActionResult(success=False, error=f"Not a file: {path}")
        if path.stat().st_size > params.max_upload_bytes:
            return ActionResult(
                success=False,
                error=f"File too large: {format_bytes(path.stat().st_size)} (max {format_bytes(params.max_upload_bytes)}).",
            )

        effective_tls, _ = self._check_tls(params.verify_tls)
        req_headers = dict(params.headers or {})

        try:
            async with httpx.AsyncClient(
                verify=effective_tls,
                timeout=httpx.Timeout(params.timeout),
            ) as client:
                files = {params.field_name: (path.name, path.read_bytes())}
                data = params.extra_fields or {}
                response = await client.post(
                    params.url,
                    headers=req_headers,
                    files=files,
                    data=data,
                )
        except httpx.TimeoutException:
            _audit("upload_file", "POST", params.url, None, "timeout")
            return ActionResult(success=False, error=f"Upload timed out after {params.timeout}s.")
        except Exception as exc:
            _audit("upload_file", "POST", params.url, None, str(exc))
            return ActionResult(success=False, error=f"Upload error: {exc}")

        status = response.status_code
        _audit("upload_file", "POST", params.url, status, None if status < 400 else f"status_{status}")

        body: Any
        try:
            body = response.json()
        except Exception:
            body = response.text[:5000]

        return ActionResult(
            success=status < 400,
            data={
                "url": str(response.url),
                "status_code": status,
                "file": str(path),
                "file_size": path.stat().st_size,
                "body": body,
                "hint": _status_hint(status),
            },
            error=None if status < 400 else f"Upload failed: HTTP {status}",
        )


    @action(
        description=(
            "Fetch a web page and extract readable text from HTML. "
            "Strips scripts, styles, and navigation. Returns text with "
            "basic structure, page title, and links found on the page."
        ),
        params_model=FetchPageParams,
        permissions=["net.http"],
        risk_level="low",
        data_classification="internal Example: fetch_page(url='https://docs.python.org/3/')",
        side_effects=["network_request"],
        tags=["http", "scrape", "html", "read"],
        aliases=["page_web", "scraper", "extraire_page", "lire_page", "web_page"],
        cli_label='Fetch',
        cli_param='url',
        cli_show_output=True,
        cli_output_lines=3,
    )
    async def fetch_page(self, params: FetchPageParams) -> ActionResult:
        result = await self._do_request(
            "fetch_page",
            method="GET",
            url=params.url,
            headers=params.headers,
            timeout=params.timeout,
            verify_tls=params.verify_tls,
            max_response_bytes=params.max_response_bytes,
        )
        if not result.success:
            return result

        html = result.data.get("body", "")
        if not isinstance(html, str):
            return ActionResult(
                success=False,
                error="Response is not HTML text.",
                data=result.data,
            )

        title = _extract_title(html)
        text = _extract_text(html, params.max_text_length)
        links = _extract_links(html) if params.extract_links else []

        return ActionResult(
            success=True,
            data={
                "url": result.data.get("url"),
                "title": title,
                "text": text,
                "text_length": len(text),
                "truncated": len(text) >= params.max_text_length,
                "links": links,
                "link_count": len(links),
                "hint": f"Extracted {len(text)} characters from page"
                        + (f" '{title}'" if title else "")
                        + f". Found {len(links)} links.",
            },
        )


    async def _download_worker(
        self, task: DownloadTask, params: DownloadParams
    ) -> None:
        """Streaming download worker — runs as asyncio.Task."""
        effective_tls, _ = self._check_tls(params.verify_tls)

        try:
            async with httpx.AsyncClient(
                verify=effective_tls,
                follow_redirects=True,
                timeout=httpx.Timeout(params.timeout, connect=30.0),
            ) as client:
                async with client.stream(
                    "GET",
                    params.url,
                    headers=params.headers or {},
                ) as response:
                    if response.status_code >= 400:
                        task.status = "failed"
                        task.error = f"HTTP {response.status_code}: {_status_hint(response.status_code)}"
                        task.finished_at = datetime.now(timezone.utc).isoformat()
                        _audit("download", "GET", params.url, response.status_code, task.error)
                        return

                    cl = response.headers.get("content-length")
                    if cl and cl.isdigit():
                        task.total_bytes = int(cl)

                    dest = Path(params.destination)
                    dest.parent.mkdir(parents=True, exist_ok=True)

                    task._last_speed_check = time.monotonic()
                    task._last_speed_bytes = 0

                    with open(dest, "wb") as f:
                        async for chunk in response.aiter_bytes(params.chunk_size):
                            f.write(chunk)
                            task.downloaded_bytes += len(chunk)

                            now = time.monotonic()
                            elapsed = now - task._last_speed_check
                            if elapsed >= 1.0:
                                delta_bytes = task.downloaded_bytes - task._last_speed_bytes
                                task.speed_bytes_per_sec = delta_bytes / elapsed
                                task._last_speed_check = now
                                task._last_speed_bytes = task.downloaded_bytes

            task.status = "completed"
            task.finished_at = datetime.now(timezone.utc).isoformat()
            _audit("download", "GET", params.url, 200, None)
            logger.info(
                "download_completed id=%s bytes=%d elapsed=%.1fs",
                task.download_id, task.downloaded_bytes, task.elapsed_seconds,
            )
            self._notify_bg({
                "task_id": task.download_id,
                "tool_name": "http.download",
                "status": "completed",
                "elapsed_seconds": task.elapsed_seconds,
                "result": {
                    "download_id": task.download_id,
                    "url": task.url,
                    "destination": task.destination,
                    "bytes": task.downloaded_bytes,
                },
            })

        except asyncio.CancelledError:
            task.status = "cancelled"
            task.finished_at = datetime.now(timezone.utc).isoformat()
            dest = Path(params.destination)
            if dest.exists():
                dest.unlink(missing_ok=True)
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
            task.finished_at = datetime.now(timezone.utc).isoformat()
            _audit("download", "GET", params.url, None, str(exc))
            self._notify_bg({
                "task_id": task.download_id,
                "tool_name": "http.download",
                "status": "failed",
                "elapsed_seconds": task.elapsed_seconds,
                "error": str(exc),
            })

    @action(
        description=(
            "Start a background file download and return a download_id. "
            "Uses streaming to handle files of any size without memory pressure. "
            "The system waits ~300ms to detect immediate failures (DNS error, 404, "
            "permission denied). Use download_status to check progress, "
            "download_cancel to stop. Supports downloads lasting hours."
        ),
        params_model=DownloadParams,
        permissions=["net.http", "fs.write"],
        risk_level="medium",
        data_classification="internal",
        side_effects=["network_request", "filesystem_write"],
        tags=["http", "download", "background", "file"],
        aliases=["telecharger", "download_file", "telecharger_fichier", "dl"],
        cli_label='Download',
        cli_param='url',
    )
    async def download(self, params: DownloadParams) -> ActionResult:
        url_error, _pinned = self._validate_url(params.url)
        if url_error:
            return ActionResult(success=False, error=url_error)

        dest = Path(params.destination).resolve()
        if dest.exists() and not params.overwrite:
            return ActionResult(
                success=False,
                error=f"File already exists: {dest}. Set overwrite=true to replace.",
            )

        download_id = uuid.uuid4().hex[:12]
        task = DownloadTask(
            download_id=download_id,
            url=params.url,
            destination=str(dest),
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        task._task = asyncio.create_task(self._download_worker(task, params))
        self._downloads[download_id] = task

        _SETTLE_SECONDS = 0.3
        await asyncio.sleep(_SETTLE_SECONDS)

        if task.status == "failed":
            _audit("download", "GET", params.url, None, task.error)
            return ActionResult(
                success=False,
                error=f"Download failed immediately: {task.error}",
                data=task.to_summary(),
            )

        if task.status == "completed":
            return ActionResult(
                success=True,
                data=task.to_summary(),
            )

        _audit("download", "GET", params.url, None, "started")
        return ActionResult(
            success=True,
            data=task.to_summary(),
        )

    @action(
        description=(
            "Check the progress of a background download: bytes downloaded, "
            "speed, ETA, and completion percentage."
        ),
        params_model=DownloadIdParams,
        permissions=["sys.info"],
        risk_level="low",
        data_classification="internal",
        tags=["http", "download", "status"],
        aliases=["statut_telechargement", "dl_status", "download_progress"],
        cli_label='Download status',
        cli_param='download_id',
    )
    async def download_status(self, params: DownloadIdParams) -> ActionResult:
        task = self._downloads.get(params.download_id)
        if task is None:
            return ActionResult(
                success=False,
                error=f"Download '{params.download_id}' not found. Use download_list to see available downloads.",
            )
        return ActionResult(success=True, data=task.to_summary())

    @action(
        description=(
            "Cancel a running background download. "
            "The partially downloaded file is deleted."
        ),
        params_model=DownloadIdParams,
        permissions=["sys.info"],
        risk_level="low",
        data_classification="internal",
        tags=["http", "download", "cancel"],
        aliases=["annuler_telechargement", "dl_cancel", "stop_download"],
        cli_label="Cancel download",
        cli_param="download_id",
    )
    async def download_cancel(self, params: DownloadIdParams) -> ActionResult:
        task = self._downloads.get(params.download_id)
        if task is None:
            return ActionResult(
                success=False,
                error=f"Download '{params.download_id}' not found.",
            )
        if task.status != "downloading":
            return ActionResult(
                success=True,
                data={
                    "download_id": params.download_id,
                    "status": task.status,
                    "hint": f"Download already {task.status}.",
                },
            )

        if task._task and not task._task.done():
            task._task.cancel()
            try:
                await task._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug("download_cancel_await_failed: %s", exc)

        task.status = "cancelled"
        task.finished_at = datetime.now(timezone.utc).isoformat()

        # Delete partial file (the hint already promises this)
        try:
            from pathlib import Path as _Path
            if task.dest_path:
                p = _Path(task.dest_path)
                if p.exists() and p.is_file():
                    p.unlink()
        except Exception as exc:
            logger.debug("download_cancel_unlink_failed path=%s: %s", task.dest_path, exc)

        _audit("download_cancel", "GET", task.url, None, "cancelled")
        return ActionResult(
            success=True,
            data={
                "download_id": params.download_id,
                "status": "cancelled",
                "downloaded_bytes": task.downloaded_bytes,
                "downloaded_human": format_bytes(task.downloaded_bytes),
                "hint": "Download cancelled. Partial file deleted.",
            },
        )

    @action(
        description=(
            "List all background downloads (active and completed) "
            "with their status, progress, and speed."
        ),
        params_model=DownloadListParams,
        permissions=["sys.info"],
        risk_level="low",
        data_classification="internal",
        tags=["http", "download", "list"],
        aliases=["liste_telechargements", "dl_list"],
        cli_label='Downloads',
    )
    async def download_list(self, params: DownloadListParams) -> ActionResult:
        sorted_dls = sorted(
            self._downloads.values(),
            key=lambda d: (d.status != "downloading", d.started_at),
        )
        downloads = [d.to_summary() for d in sorted_dls]
        active = sum(1 for d in self._downloads.values() if d.status == "downloading")
        completed = sum(1 for d in self._downloads.values() if d.status == "completed")
        failed = sum(1 for d in self._downloads.values() if d.status == "failed")

        data: dict[str, Any] = {
            "downloads": downloads,
            "total": len(downloads),
            "active": active,
            "completed": completed,
            "failed": failed,
        }

        if not downloads:
            data["hint"] = "No downloads. Use download(url, destination) to start one."
        elif active > 0:
            data["hint"] = (
                f"{active} active, {completed} completed, {failed} failed. "
                "Use download_status(download_id) for progress details."
            )
        else:
            data["hint"] = (
                f"All downloads finished: {completed} completed, {failed} failed."
            )

        return ActionResult(success=True, data=data)


    async def estimate_cost(self, action_name: str, params: dict[str, Any]) -> ResourceEstimate:
        return ResourceEstimate(
            estimated_duration_seconds=5.0,
            estimated_cpu_percent=5.0,
            estimated_memory_mb=20.0,
            estimated_io_operations=2,
            confidence=0.3,
        )


    async def health_check(self) -> dict[str, Any]:
        active_downloads = sum(1 for d in self._downloads.values() if d.status == "downloading")
        return {
            "status": "ok",
            "module_id": self.MODULE_ID,
            "version": self.VERSION,
            "httpx_version": httpx.__version__,
            "allowed_hosts": self._allowed_hosts,
            "blocked_hosts": self._blocked_hosts,
            "active_downloads": active_downloads,
        }

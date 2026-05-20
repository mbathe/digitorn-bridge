"""Web module - fast search, fetch, and parse web content."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
from typing import Any

import aiohttp

from pydantic import BaseModel, Field as PydanticField

from digitorn.modules.base import ActionResult, BaseModule
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ConstraintSpec, ModuleManifest
from digitorn.modules.web.params import (
    DownloadParams,
    ExtractParams,
    FetchParams,
    SearchParams,
)
from digitorn.modules.web.parser import (
    extract_meta_description,
    extract_title,
    extract_with_selector,
    html_to_markdown,
    html_to_text,
    scan_for_injection,
)

logger = logging.getLogger(__name__)

_DEFAULT_UA = "Mozilla/5.0 (compatible; Digitorn/1.0; +https://digitorn.dev)"
_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=30)
_SEARCH_TIMEOUT = aiohttp.ClientTimeout(total=20)
class _FetchCache:
    """Short-TTL cache for fetched pages."""

    __slots__ = ("_store", "_ttl", "_max_size")

    def __init__(self, ttl: float = 300.0, max_size: int = 50):
        self._store: dict[str, tuple[float, str]] = {}
        self._ttl = ttl
        self._max_size = max_size

    def get(self, url: str) -> str | None:
        entry = self._store.get(url)
        if entry is None:
            return None
        ts, html = entry
        if time.monotonic() - ts > self._ttl:
            del self._store[url]
            return None
        return html

    def set(self, url: str, html: str) -> None:
        if len(self._store) >= self._max_size:
            oldest = min(self._store, key=lambda k: self._store[k][0])
            del self._store[oldest]
        self._store[url] = (time.monotonic(), html)

    def invalidate(self) -> None:
        self._store.clear()
class WebConfig(BaseModel):
    """Pydantic config for the web module (validated at compile time)."""

    model_config = {"extra": "forbid"}

    workspace: str = PydanticField(
        default="",
        description=(
            "Auto-injected by the daemon at module init time. "
            "Do NOT set manually in YAML - the daemon resolves it from "
            "the app's workspace/workspace_mode config."
        ),
    )
    search_backend: str = PydanticField("duckduckgo", description="Primary search backend (duckduckgo, brave, tavily, searxng, google)")
    search_fallback: str | None = PydanticField(None, description="Fallback search backend")
    user_agent: str | None = PydanticField(None, description="Custom User-Agent")
    cache_ttl: float = PydanticField(900.0, ge=0, description="Fetch cache TTL in seconds (default 15 minutes)")
    max_content_length: int = PydanticField(50000, ge=1000, le=1_000_000, description="Max content length from fetched pages")
    fetch_timeout: float = PydanticField(30.0, ge=1.0, le=300.0, description="HTTP fetch timeout in seconds")

class WebModule(BaseModule):
    """Web search, fetch, and content extraction."""

    MODULE_ID = "web"
    VERSION = "1.0.0"
    CONFIG_MODEL = WebConfig

    CONSTRAINTS = [
        ConstraintSpec(name="allowed_domains", type="string_list", description="Restrict web search/fetch to these domains.", scope="universal"),
        ConstraintSpec(name="blocked_domains", type="string_list", description="Block these domains from search/fetch.", scope="universal"),
    ]

    def get_prompt_sections(self) -> list[dict[str, Any]]:
        return []  # Actions are self-descriptive via schema

    def __init__(self) -> None:
        super().__init__()
        self._search_backend: str = "duckduckgo"
        self._search_fallback: str | None = None
        self._api_keys: dict[str, str] = {}
        self._user_agent: str = _DEFAULT_UA
        self._max_content_length: int = 50000
        self._cache = _FetchCache(ttl=900.0, max_size=100)
        self._session: aiohttp.ClientSession | None = None
        self._allowed_domains: list[str] | None = None
        self._blocked_domains: list[str] = []
        self._detect_injection: bool = True
        self._extra_injection_patterns: list[str] = []

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": "Web search, fetch, and parse with multi-backend search (DuckDuckGo default)",
            "author": "Digitorn Team",
        })

    async def on_start(self) -> None:
        pass

    async def on_config_update(self, config: dict[str, Any]) -> None:
        await super().on_config_update(config)
        if isinstance(self._config, WebConfig):
            self._search_backend = self._config.search_backend
            self._search_fallback = self._config.search_fallback
            self._user_agent = self._config.user_agent or _DEFAULT_UA
            self._max_content_length = self._config.max_content_length
            cache_ttl = self._config.cache_ttl
            # Update fetch timeout from config (overrides module-level default)
            global _FETCH_TIMEOUT
            _FETCH_TIMEOUT = aiohttp.ClientTimeout(total=self._config.fetch_timeout)
        else:
            search_config = config.get("search", {})
            self._search_backend = search_config.get("primary", config.get("search_backend", "duckduckgo"))
            self._search_fallback = search_config.get("fallback")
            self._user_agent = config.get("user_agent", _DEFAULT_UA)
            self._max_content_length = config.get("max_content_length", 50000)
            cache_ttl = config.get("cache_ttl", 300.0)

        # Always read these from raw config dict (not in CONFIG_MODEL)
        if isinstance(config, dict):
            search_config = config.get("search", {})
            self._api_keys = search_config.get("api_keys", config.get("api_keys", {}))
            cache_max = config.get("cache_max_size", 50)
            egress = config.get("egress", {})
            self._allowed_domains = egress.get("allowed_domains")
            self._blocked_domains = egress.get("blocked_domains", [])
            security = config.get("security", {})
            self._detect_injection = security.get("detect_injection", True)
            self._extra_injection_patterns = security.get("injection_patterns", [])
        else:
            cache_max = 50

        self._cache = _FetchCache(ttl=cache_ttl, max_size=cache_max)

    async def on_stop(self) -> None:
        """Close the aiohttp ClientSession + invalidate the fetch cache."""
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception as exc:
                logger.debug("web_session_close_failed: %s", exc)
        self._session = None
        try:
            self._cache.invalidate()
        except Exception as exc:
            logger.debug("web_cache_invalidate_failed: %s", exc)

    def _check_domain(self, url: str) -> str | None:
        try:
            from urllib.parse import urlparse
            host = urlparse(url).hostname or ""
        except Exception:
            return f"Invalid URL: {url}"

        for blocked in self._blocked_domains:
            if host == blocked or host.endswith(f".{blocked}"):
                return f"Domain '{host}' is blocked by egress policy"

        if self._allowed_domains is not None:
            for allowed in self._allowed_domains:
                if host == allowed or host.endswith(f".{allowed}"):
                    return None
            return f"Domain '{host}' is not in the allowed domains list"

        return None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": self._user_agent},
                timeout=_FETCH_TIMEOUT,
            )
        return self._session
    @action(
        description="Search the web for information.",
        tool_prompt=(
            "Search the web. Returns results with title, URL, and snippet.\n"
            "\n"
            "## When to use\n"
            "- Finding documentation, error solutions, API references\n"
            "- Getting current information not in your training data\n"
            "- Researching libraries, frameworks, best practices\n"
            "- Verifying facts or checking latest versions\n"
            "\n"
            "## When NOT to use\n"
            "- Information already in the codebase - Grep the project first\n"
            "- Questions the user can answer - ask them instead\n"
            "- Repeated searches for the same topic - remember results with Remember\n"
            "\n"
            "## Workflow\n"
            "1. Search('python asyncio timeout handling') → get URLs\n"
            "2. Fetch(url) on the 2-3 most relevant results → get full content\n"
            "3. Remember the key findings so they survive compaction\n"
            "\n"
            "## Tips\n"
            "- Be specific: 'python asyncio timeout error' not 'python error'\n"
            "- Add version/year for current info: 'react 19 server components 2026'\n"
            "- Results include title, URL, and snippet - scan snippets before fetching"
        ),
        params_model=SearchParams,
        risk_level="low",
        tags=["web", "search"],
        cli_label='Search',
        cli_param='query',
        cli_show_output=True,
        cli_output_lines=5,
    )
    async def search(self, params: SearchParams) -> ActionResult:
        # Validate domain filter conflict
        if params.allowed_domains and params.blocked_domains:
            return ActionResult(
                success=False,
                error="Cannot specify both allowed_domains and blocked_domains. Choose one.",
            )

        backend = self._search_backend
        try:
            results = await self._search_with_backend(backend, params.query, params.limit)
            # Apply per-query domain filtering
            results = self._filter_results_by_domain(results, params.allowed_domains, params.blocked_domains)
            return ActionResult(success=True, data={
                "query": params.query,
                "results": results,
                "count": len(results),
                "backend": backend,
                "sources": [r["url"] for r in results if r.get("url")],
            })
        except Exception as exc:
            if self._search_fallback and self._search_fallback != backend:
                try:
                    results = await self._search_with_backend(
                        self._search_fallback, params.query, params.limit,
                    )
                    results = self._filter_results_by_domain(results, params.allowed_domains, params.blocked_domains)
                    return ActionResult(success=True, data={
                        "query": params.query,
                        "results": results,
                        "count": len(results),
                        "backend": self._search_fallback,
                        "sources": [r["url"] for r in results if r.get("url")],
                        "note": f"Primary backend '{backend}' failed, used fallback",
                    })
                except Exception as fallback_exc:
                    return ActionResult(
                        success=False,
                        error=f"Search failed on both backends. Primary ({backend}): {exc}. Fallback ({self._search_fallback}): {fallback_exc}",
                    )
            return ActionResult(success=False, error=f"Search failed ({backend}): {exc}")

    @staticmethod
    def _filter_results_by_domain(
        results: list[dict[str, str]],
        allowed: list[str] | None,
        blocked: list[str] | None,
    ) -> list[dict[str, str]]:
        if not allowed and not blocked:
            return results

        from urllib.parse import urlparse

        filtered = []
        for r in results:
            url = r.get("url", "")
            try:
                host = urlparse(url).hostname or ""
            except Exception:
                continue

            if blocked:
                if any(host == d or host.endswith(f".{d}") for d in blocked):
                    continue
            if allowed:
                if not any(host == d or host.endswith(f".{d}") for d in allowed):
                    continue
            filtered.append(r)
        return filtered

    @action(
        description="Fetch a web page and return its content as text.",
        tool_prompt=(
            "Fetch a web page and convert to clean readable text.\n"
            "\n"
            "## When to use\n"
            "- After Search - fetch the 2-3 most relevant URLs for full content\n"
            "- Reading documentation pages, API references, tutorials\n"
            "- Extracting specific data from a known URL\n"
            "\n"
            "## When NOT to use\n"
            "- Don't fetch every search result - scan snippets first, fetch only what's relevant\n"
            "- Don't fetch the same URL twice - results are cached 5 minutes\n"
            "- Don't fetch large download pages - use Bash with curl for raw downloads\n"
            "\n"
            "## Modes\n"
            "- Default: full page → markdown text (scripts/ads stripped)\n"
            "- extract=true: main content only (article body, strips nav/footer)\n"
            "- prompt='pricing table': hint to focus extraction on specific content\n"
            "\n"
            "## Behavior\n"
            "- After fetching, Remember the key facts - don't re-fetch later\n"
            "- If a page is too long, use prompt to extract the relevant section\n"
            "- Skip dead links, paywalls, and obviously spammy content"
        ),
        params_model=FetchParams,
        risk_level="low",
        tags=["web", "fetch"],
        cli_label='Fetch',
        cli_param='url',
        cli_show_output=True,
        cli_output_lines=3,
    )
    async def fetch(self, params: FetchParams) -> ActionResult:
        # Extract mode: delegate to extract action
        if params.extract:
            return await self.extract(ExtractParams(
                url=params.url,
                max_length=params.max_length,
            ))

        url = params.url

        if url.startswith("http://"):
            url = "https://" + url[7:]

        if err := self._check_domain(url):
            return ActionResult(success=False, error=err)

        fmt = params.format if params.format in ("text", "markdown", "html") else "text"
        # Legacy compat: raw=true overrides format
        if params.raw:
            fmt = "html"

        cached_html = self._cache.get(url)
        if cached_html is not None and fmt != "html":
            # html2text + BeautifulSoup parsing on large HTML pages can
            # block 200ms+. Off-load to keep the loop responsive.
            if fmt == "markdown":
                content = await asyncio.to_thread(html_to_markdown, cached_html, params.max_length)
            else:
                content = await asyncio.to_thread(html_to_text, cached_html, params.max_length)
            # Apply prompt-based filtering if set
            if params.prompt:
                content = self._apply_prompt_filter(content, params.prompt, params.max_length)
            title = extract_title(cached_html)
            return ActionResult(success=True, data={
                "url": url,
                "title": title,
                "content": content,
                "length": len(content),
                "format": fmt,
                "cached": True,
            })

        try:
            session = await self._get_session()
            # Use allow_redirects but track cross-host redirects
            async with session.get(url, timeout=_FETCH_TIMEOUT, allow_redirects=True) as resp:
                final_url = str(resp.url)

                if final_url != url:
                    from urllib.parse import urlparse
                    orig_host = urlparse(url).hostname
                    final_host = urlparse(final_url).hostname
                    if orig_host != final_host:
                        return ActionResult(success=True, data={
                            "url": url,
                            "redirected_to": final_url,
                            "redirect_host": final_host,
                            "content": (
                                f"URL redirected to a different host: {final_url}\n"
                                f"Make a new fetch request with the redirect URL to get the content."
                            ),
                            "is_redirect": True,
                        })

                if resp.status != 200:
                    return ActionResult(
                        success=False,
                        error=f"HTTP {resp.status} fetching {url}",
                    )

                content_type = resp.headers.get("Content-Type", "")
                if "application/pdf" in content_type:
                    size = int(resp.headers.get("Content-Length", 0))
                    return ActionResult(success=True, data={
                        "url": url,
                        "content": f"[PDF document: {size:,} bytes. Use web.download() to save locally, then filesystem.read() with pages parameter to extract text.]",
                        "is_binary": True,
                        "content_type": content_type,
                    })
                if not content_type.startswith("text/") and "html" not in content_type and "json" not in content_type and "xml" not in content_type:
                    size = int(resp.headers.get("Content-Length", 0))
                    return ActionResult(success=True, data={
                        "url": url,
                        "content": f"[Binary content: {content_type}, {size:,} bytes. Use web.download() to save locally.]",
                        "is_binary": True,
                        "content_type": content_type,
                    })

                html = await resp.text(errors="replace")
                self._cache.set(url, html)

            if fmt == "html":
                content = html[:params.max_length]
            elif fmt == "markdown":
                content = await asyncio.to_thread(html_to_markdown, html, params.max_length)
            else:
                content = await asyncio.to_thread(html_to_text, html, params.max_length)

            if params.prompt:
                content = self._apply_prompt_filter(content, params.prompt, params.max_length)

            title = extract_title(html)
            description = extract_meta_description(html)

            result_data: dict[str, Any] = {
                "url": url,
                "title": title,
                "description": description,
                "content": content,
                "length": len(content),
                "format": fmt,
                "cached": False,
            }

            # Include final URL if different (same-host redirect)
            if final_url != url:
                result_data["final_url"] = final_url

            injection_warning = scan_for_injection(content) if self._detect_injection else None
            if injection_warning:
                result_data["security_warning"] = injection_warning
                logger.warning("web_injection_detected url=%s warning=%s", url, injection_warning)

            return ActionResult(success=True, data=result_data)

        except asyncio.TimeoutError:
            return ActionResult(success=False, error=f"Timeout fetching {url}")
        except Exception as exc:
            return ActionResult(success=False, error=f"Failed to fetch {url}: {exc}")

    @staticmethod
    def _apply_prompt_filter(content: str, prompt: str, max_length: int) -> str:
        keywords = [w.lower() for w in prompt.split() if len(w) > 3]
        if not keywords:
            return content[:max_length]

        paragraphs = content.split("\n\n")
        scored: list[tuple[int, str]] = []
        for para in paragraphs:
            para_lower = para.lower()
            score = sum(1 for kw in keywords if kw in para_lower)
            if score > 0:
                scored.append((score, para))

        if not scored:
            # No matches - return beginning of content
            return content[:max_length]

        # Sort by relevance score, take best paragraphs
        scored.sort(key=lambda x: -x[0])
        result_parts = []
        total_len = 0
        for _score, para in scored:
            if total_len + len(para) > max_length:
                break
            result_parts.append(para)
            total_len += len(para) + 2

        return "\n\n".join(result_parts)

    @action(
        description="Extract content from a web page using CSS selectors. Internal - use Fetch(extract=true) instead.",
        params_model=ExtractParams,
        risk_level="low",
        tags=["web", "extract", "internal"],
        cli_label='Extract',
        cli_param='url',
        cli_show_output=True,
        cli_output_lines=3,
    )
    async def extract(self, params: ExtractParams) -> ActionResult:
        if err := self._check_domain(params.url):
            return ActionResult(success=False, error=err)
        cached_html = self._cache.get(params.url)
        if cached_html is None:
            try:
                session = await self._get_session()
                async with session.get(params.url, timeout=_FETCH_TIMEOUT) as resp:
                    if resp.status != 200:
                        return ActionResult(success=False, error=f"HTTP {resp.status}")
                    cached_html = await resp.text(errors="replace")
                self._cache.set(params.url, cached_html)
            except Exception as exc:
                return ActionResult(success=False, error=f"Failed to fetch: {exc}")

        # BeautifulSoup parse off-loop. Selector evaluation on a heavy
        # page can otherwise stall the loop for 100s of ms.
        content = await asyncio.to_thread(
            extract_with_selector, cached_html, params.selector, params.max_length,
        )
        title = extract_title(cached_html)

        return ActionResult(success=True, data={
            "url": params.url,
            "title": title,
            "selector": params.selector,
            "content": content,
            "length": len(content),
        })

    @action(
        description=(
            "Download a file from a URL to a local path. "
            "Supports large files with streaming. Returns the file size in bytes. "
            "The download happens in the foreground -- use background tasks for very large files. Example: download(url='https://example.com/data.csv', path='/tmp/data.csv')"
        ),
        params_model=DownloadParams,
        risk_level="medium",
        tags=["web", "download"],
        cli_label='Download',
        cli_param='url',
    )
    async def download(self, params: DownloadParams) -> ActionResult:
        if err := self._check_domain(params.url):
            return ActionResult(success=False, error=err)
        try:
            session = await self._get_session()
            async with session.get(params.url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    return ActionResult(success=False, error=f"HTTP {resp.status}")

                total = 0
                # Off-load disk writes; batch chunks to one `to_thread`.
                import asyncio as _asyncio
                buf: list[bytes] = []
                buf_size = 0
                FLUSH_AT = 256 * 1024  # 256 KB per flush

                def _open_w() -> Any:
                    return open(params.path, "wb")
                def _flush(handle: Any, payload: bytes) -> None:
                    handle.write(payload)
                def _close(handle: Any) -> None:
                    handle.close()

                f = await _asyncio.to_thread(_open_w)
                try:
                    async for chunk in resp.content.iter_chunked(8192):
                        buf.append(chunk)
                        buf_size += len(chunk)
                        total += len(chunk)
                        if buf_size >= FLUSH_AT:
                            payload = b"".join(buf)
                            buf.clear()
                            buf_size = 0
                            await _asyncio.to_thread(_flush, f, payload)
                    if buf:
                        await _asyncio.to_thread(_flush, f, b"".join(buf))
                finally:
                    await _asyncio.to_thread(_close, f)

            return ActionResult(success=True, data={
                "url": params.url,
                "path": params.path,
                "size_bytes": total,
            })
        except Exception as exc:
            return ActionResult(success=False, error=f"Download failed: {exc}")
    async def _search_with_backend(
        self, backend: str, query: str, limit: int,
    ) -> list[dict[str, str]]:
        if backend == "duckduckgo":
            return await self._search_duckduckgo(query, limit)
        elif backend == "brave":
            return await self._search_brave(query, limit)
        elif backend == "tavily":
            return await self._search_tavily(query, limit)
        elif backend == "searxng":
            return await self._search_searxng(query, limit)
        elif backend == "google":
            return await self._search_google(query, limit)
        else:
            raise ValueError(f"Unknown search backend: {backend}")

    async def _search_duckduckgo(self, query: str, limit: int) -> list[dict[str, str]]:
        session = await self._get_session()
        url = "https://html.duckduckgo.com/html/"

        async with session.post(
            url,
            data={"q": query},
            timeout=_SEARCH_TIMEOUT,
        ) as resp:
            html = await resp.text(errors="replace")

        def _parse() -> list[dict[str, str]]:
            out: list[dict[str, str]] = []
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "html.parser")
                for result_div in soup.select(".result"):
                    title_el = result_div.select_one(".result__a")
                    snippet_el = result_div.select_one(".result__snippet")
                    if title_el:
                        href = title_el.get("href", "")
                        if "uddg=" in href:
                            href = urllib.parse.unquote(href.split("uddg=")[-1].split("&")[0])
                        out.append({
                            "title": title_el.get_text(strip=True),
                            "url": href,
                            "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                        })
                    if len(out) >= limit:
                        break
            except ImportError:
                logger.warning("web: bs4 not available for DuckDuckGo parsing")
            return out

        return await asyncio.to_thread(_parse)

    async def _search_brave(self, query: str, limit: int) -> list[dict[str, str]]:
        api_key = self._api_keys.get("brave")
        if not api_key:
            raise ValueError("Brave Search requires api_keys.brave in config")

        session = await self._get_session()
        url = "https://api.search.brave.com/res/v1/web/search"

        async with session.get(
            url,
            params={"q": query, "count": limit},
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
            timeout=_SEARCH_TIMEOUT,
        ) as resp:
            data = await resp.json()

        results = []
        for item in data.get("web", {}).get("results", [])[:limit]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            })
        return results

    async def _search_tavily(self, query: str, limit: int) -> list[dict[str, str]]:
        api_key = self._api_keys.get("tavily")
        if not api_key:
            raise ValueError("Tavily Search requires api_keys.tavily in config")

        session = await self._get_session()

        async with session.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "max_results": limit},
            timeout=_SEARCH_TIMEOUT,
        ) as resp:
            data = await resp.json()

        results = []
        for item in data.get("results", [])[:limit]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", "")[:300],
            })
        return results

    async def _search_searxng(self, query: str, limit: int) -> list[dict[str, str]]:
        base_url = self._api_keys.get("searxng_url", "http://localhost:8080")
        session = await self._get_session()

        async with session.get(
            f"{base_url}/search",
            params={"q": query, "format": "json", "pageno": 1},
            timeout=_SEARCH_TIMEOUT,
        ) as resp:
            data = await resp.json()

        results = []
        for item in data.get("results", [])[:limit]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            })
        return results

    async def _search_google(self, query: str, limit: int) -> list[dict[str, str]]:
        api_key = self._api_keys.get("google")
        cx = self._api_keys.get("google_cx")
        if not api_key or not cx:
            raise ValueError("Google Search requires api_keys.google and api_keys.google_cx")

        session = await self._get_session()

        async with session.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": api_key, "cx": cx, "q": query, "num": min(limit, 10)},
            timeout=_SEARCH_TIMEOUT,
        ) as resp:
            data = await resp.json()

        results = []
        for item in data.get("items", [])[:limit]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            })
        return results

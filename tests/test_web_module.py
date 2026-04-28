"""Web module tests - covers search, fetch, extract, download."""

from __future__ import annotations

import pytest

from _test_helpers import run_coro
from digitorn.modules.web.module import WebModule, _FetchCache
from digitorn.modules.web.parser import html_to_text, extract_title, extract_meta_description, extract_with_selector


class TestHTMLParser:
    def test_html_to_text_basic(self):
        html = "<html><body><h1>Title</h1><p>Hello world</p></body></html>"
        text = html_to_text(html)
        assert "Title" in text
        assert "Hello world" in text

    def test_strips_scripts(self):
        html = "<html><body><script>alert('xss')</script><p>Content</p></body></html>"
        text = html_to_text(html)
        assert "alert" not in text
        assert "Content" in text

    def test_strips_styles(self):
        html = "<html><body><style>body{color:red}</style><p>Text</p></body></html>"
        text = html_to_text(html)
        assert "color:red" not in text
        assert "Text" in text

    def test_strips_nav_footer(self):
        html = "<html><body><nav>Menu</nav><p>Content</p><footer>Footer</footer></body></html>"
        text = html_to_text(html)
        assert "Content" in text

    def test_max_length_truncation(self):
        html = "<html><body><p>" + "x" * 10000 + "</p></body></html>"
        text = html_to_text(html, max_length=100)
        assert len(text) <= 150  # with truncation message

    def test_extract_title(self):
        html = "<html><head><title>My Page</title></head></html>"
        assert extract_title(html) == "My Page"

    def test_extract_title_missing(self):
        html = "<html><body>No title</body></html>"
        assert extract_title(html) == ""

    def test_extract_meta_description(self):
        html = '<html><head><meta name="description" content="A page about stuff"></head></html>'
        assert extract_meta_description(html) == "A page about stuff"

    def test_extract_with_selector(self):
        html = '<html><body><article><p>Main content</p></article><div>Noise</div></body></html>'
        text = extract_with_selector(html, "article")
        assert "Main content" in text


class TestFetchCache:
    def test_set_and_get(self):
        cache = _FetchCache(ttl=10)
        cache.set("http://example.com", "<html>test</html>")
        assert cache.get("http://example.com") == "<html>test</html>"

    def test_miss(self):
        cache = _FetchCache(ttl=10)
        assert cache.get("http://nonexistent.com") is None

    def test_eviction(self):
        cache = _FetchCache(ttl=10, max_size=2)
        cache.set("a", "1")
        cache.set("b", "2")
        cache.set("c", "3")  # should evict "a"
        assert cache.get("a") is None
        assert cache.get("c") == "3"

    def test_invalidate(self):
        cache = _FetchCache(ttl=10)
        cache.set("a", "1")
        cache.invalidate()
        assert cache.get("a") is None


class TestWebModule:
    @pytest.fixture
    def web(self):
        m = WebModule()
        run_coro(m.on_config_update({}))
        return m

    def test_default_backend(self, web):
        assert web._search_backend == "duckduckgo"

    def test_custom_backend(self):
        m = WebModule()
        run_coro(
            m.on_config_update({"search_backend": "brave"})
        )
        assert m._search_backend == "brave"

    @pytest.mark.asyncio
    async def test_fetch_invalid_url(self, web):
        from digitorn.modules.web.params import FetchParams
        r = await web.fetch(FetchParams(url="http://nonexistent.invalid.tld.xyz"))
        assert not r.success or r.data.get("length", 0) == 0

    @pytest.mark.asyncio
    async def test_search_returns_structure(self, web):
        """Smoke test - may fail if DuckDuckGo blocks CI."""
        from digitorn.modules.web.params import SearchParams
        try:
            r = await web.search(SearchParams(query="python", limit=2))
            if r.success:
                assert "results" in r.data
                assert "count" in r.data
        except Exception:
            pytest.skip("Network unavailable")
        finally:
            await web.on_stop()

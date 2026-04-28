"""HTTP module tests - security helpers, HTML extraction, data structures, constraints."""

from __future__ import annotations

import pytest

from digitorn.modules.http.module import (
    DownloadTask,
    HttpConfig,
    HttpModule,
    _extract_text,
    _extract_title,
    _extract_links,
    _status_hint,
)
from digitorn.modules.http.security import (
    format_bytes,
    is_private_ip,
    mask_headers,
    validate_url,
)


# ═══════════════════════════════════════════════════════════════
# CONFIG_MODEL
# ═══════════════════════════════════════════════════════════════


class TestHttpConfig:
    def test_defaults(self):
        c = HttpConfig()
        assert c.timeout == 30
        assert c.max_response_size == 10_000_000
        assert c.allow_insecure_tls is False
        assert c.user_agent is None

    def test_custom(self):
        c = HttpConfig(timeout=60, allow_insecure_tls=True, user_agent="MyBot/1.0")
        assert c.timeout == 60
        assert c.allow_insecure_tls is True
        assert c.user_agent == "MyBot/1.0"

    def test_validation(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            HttpConfig(timeout=0)
        with pytest.raises(ValidationError):
            HttpConfig(timeout=999)

    def test_config_model_set(self):
        assert HttpModule.CONFIG_MODEL is HttpConfig


# ═══════════════════════════════════════════════════════════════
# SECURITY HELPERS
# ═══════════════════════════════════════════════════════════════


class TestIsPrivateIP:
    def test_loopback(self):
        assert is_private_ip("127.0.0.1") is True

    def test_rfc1918_10(self):
        assert is_private_ip("10.0.0.1") is True

    def test_rfc1918_172(self):
        assert is_private_ip("172.16.0.1") is True

    def test_rfc1918_192(self):
        assert is_private_ip("192.168.1.1") is True

    def test_ipv6_loopback(self):
        assert is_private_ip("::1") is True

    def test_public_ip(self):
        assert is_private_ip("8.8.8.8") is False

    def test_invalid_ip(self):
        assert is_private_ip("not-an-ip") is True


class TestMaskHeaders:
    def test_masks_authorization(self):
        h = mask_headers({"Authorization": "Bearer secret123", "Content-Type": "application/json"})
        assert h["Authorization"] == "***masked***"
        assert h["Content-Type"] == "application/json"

    def test_masks_cookie(self):
        h = mask_headers({"Cookie": "session=abc123"})
        assert h["Cookie"] == "***masked***"

    def test_masks_api_key(self):
        h = mask_headers({"X-Api-Key": "my-key"})
        assert h["X-Api-Key"] == "***masked***"

    def test_case_insensitive(self):
        h = mask_headers({"authorization": "secret"})
        assert h["authorization"] == "***masked***"


class TestValidateUrl:
    def test_valid_https(self):
        err, url, _ = validate_url("https://example.com/api")
        # May fail due to DNS resolution in CI, but scheme/format should pass
        assert url == "https://example.com/api"

    def test_blocks_ftp(self):
        err, _, _ = validate_url("ftp://example.com")
        assert err is not None
        assert "Scheme" in err

    def test_blocks_no_hostname(self):
        err, _, _ = validate_url("http://")
        assert err is not None

    def test_blocked_hosts(self):
        err, _, _ = validate_url("https://evil.com/api", blocked_hosts=["evil.com"])
        assert err is not None
        assert "blocked" in err.lower()

    def test_allowed_hosts_filters(self):
        err, _, _ = validate_url("https://other.com/api", allowed_hosts=["api.example.com"])
        assert err is not None
        assert "not in the allowed" in err.lower()


class TestFormatBytes:
    def test_bytes(self):
        assert format_bytes(500) == "500 B"

    def test_kilobytes(self):
        result = format_bytes(1536)
        assert "KB" in result

    def test_megabytes(self):
        result = format_bytes(5_000_000)
        assert "MB" in result

    def test_gigabytes(self):
        result = format_bytes(2_000_000_000)
        assert "GB" in result


# ═══════════════════════════════════════════════════════════════
# HTML EXTRACTION
# ═══════════════════════════════════════════════════════════════


class TestExtractText:
    def test_basic(self):
        html = "<html><body><h1>Title</h1><p>Hello world</p></body></html>"
        text = _extract_text(html)
        assert "Title" in text
        assert "Hello world" in text

    def test_strips_scripts(self):
        html = "<html><body><script>alert('xss')</script><p>Content</p></body></html>"
        text = _extract_text(html)
        assert "alert" not in text
        assert "Content" in text

    def test_strips_styles(self):
        html = "<html><body><style>body{color:red}</style><p>Text</p></body></html>"
        text = _extract_text(html)
        assert "color:red" not in text
        assert "Text" in text

    def test_max_length(self):
        html = "<p>" + "x" * 10000 + "</p>"
        text = _extract_text(html, max_length=100)
        assert len(text) <= 200  # truncation message included

    def test_entities(self):
        html = "<p>&amp; &lt; &gt;</p>"
        text = _extract_text(html)
        assert "&" in text
        assert "<" in text


class TestExtractTitle:
    def test_found(self):
        assert _extract_title("<html><head><title>My Page</title></head></html>") == "My Page"

    def test_missing(self):
        assert _extract_title("<html><body>No title</body></html>") is None


class TestExtractLinks:
    def test_basic(self):
        html = '<a href="https://example.com">Link</a>'
        links = _extract_links(html)
        assert len(links) == 1
        assert links[0]["href"] == "https://example.com"
        assert links[0]["text"] == "Link"

    def test_skips_javascript(self):
        html = '<a href="javascript:void(0)">Bad</a><a href="https://ok.com">OK</a>'
        links = _extract_links(html)
        assert len(links) == 1
        assert links[0]["href"] == "https://ok.com"


# ═══════════════════════════════════════════════════════════════
# STATUS HINTS
# ═══════════════════════════════════════════════════════════════


class TestStatusHint:
    def test_200(self):
        assert "Success" in _status_hint(200)

    def test_404(self):
        assert "Not found" in _status_hint(404)

    def test_500(self):
        assert "server error" in _status_hint(500).lower()

    def test_unknown(self):
        result = _status_hint(999)
        assert "999" in result


# ═══════════════════════════════════════════════════════════════
# DOWNLOAD TASK DATA STRUCTURE
# ═══════════════════════════════════════════════════════════════


class TestDownloadTask:
    def test_to_summary_downloading(self):
        from datetime import datetime, timezone

        dt = DownloadTask(
            download_id="dl-1",
            url="https://example.com/file.zip",
            destination="/tmp/file.zip",
            started_at=datetime.now(timezone.utc).isoformat(),
            total_bytes=1000,
            downloaded_bytes=500,
            speed_bytes_per_sec=100,
            status="downloading",
        )
        s = dt.to_summary()
        assert s["percent"] == 50.0
        assert s["status"] == "downloading"
        assert s["eta_seconds"] is not None

    def test_to_summary_completed(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        dt = DownloadTask(
            download_id="dl-2",
            url="https://example.com/file.zip",
            destination="/tmp/file.zip",
            started_at=now,
            finished_at=now,
            total_bytes=1000,
            downloaded_bytes=1000,
            status="completed",
        )
        s = dt.to_summary()
        assert s["status"] == "completed"
        assert "completed" in s["hint"].lower()

    def test_percent_none_when_no_total(self):
        from datetime import datetime, timezone

        dt = DownloadTask(
            download_id="dl-3",
            url="https://example.com/file.zip",
            destination="/tmp/file.zip",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        assert dt.percent is None


# ═══════════════════════════════════════════════════════════════
# MODULE MANIFEST
# ═══════════════════════════════════════════════════════════════


class TestHttpModuleManifest:
    def test_constraints(self):
        m = HttpModule()
        manifest = m.get_manifest()
        names = [c.name for c in (manifest.supported_constraints or [])]
        assert "allowed_hosts" in names
        assert "blocked_hosts" in names

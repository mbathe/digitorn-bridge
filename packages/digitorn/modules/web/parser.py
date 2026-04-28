"""HTML to text parser - clean, fast content extraction.

Uses html2text for markdown-like output, bs4 for targeted extraction.
Strips scripts, styles, nav, footer, ads - keeps only useful content.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_STRIP_TAGS = {"script", "style", "nav", "footer", "header", "noscript", "svg", "iframe", "form"}

_NOISE_SELECTORS = [
    ".cookie-banner", ".cookie-consent", ".ad", ".ads", ".advertisement",
    ".sidebar", ".nav", ".navigation", ".menu", ".footer",
    "#cookie-banner", "#cookie-consent", "#ads", "#sidebar", "#nav",
]


_INJECTION_MARKERS = [
    "ignore previous instructions",
    "ignore all instructions",
    "disregard your instructions",
    "you are now",
    "new instructions:",
    "system prompt:",
    "forget everything",
    "<|im_start|>",
    "[INST]",
    "<<SYS>>",
]


def scan_for_injection(text: str) -> str | None:
    """Return a warning if text contains likely prompt injection patterns."""
    lower = text.lower()
    for marker in _INJECTION_MARKERS:
        if marker.lower() in lower:
            return f"Potential prompt injection detected: content contains '{marker}'"
    return None


def html_to_text(html: str, max_length: int = 50000) -> str:
    """Convert HTML to clean readable text (markdown-like).

    Fast path: html2text with noise removal.
    """
    try:
        import html2text

        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.ignore_emphasis = False
        h.body_width = 0
        h.skip_internal_links = True
        h.inline_links = True

        cleaned = _strip_noise_tags(html)

        text = h.handle(cleaned)

        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()

        if len(text) > max_length:
            text = text[:max_length] + f"\n\n... (truncated at {max_length} chars)"

        return text

    except ImportError:
        return _regex_strip(html, max_length)


def html_to_markdown(html: str, max_length: int = 50000) -> str:
    """Convert HTML to proper Markdown preserving links, images, headers, code blocks.

    Richer than html_to_text: keeps images, uses reference-style links for readability.
    """
    try:
        import html2text

        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = False
        h.ignore_emphasis = False
        h.body_width = 0
        h.skip_internal_links = False
        h.inline_links = True
        h.protect_links = True
        h.wrap_links = False
        h.mark_code = True

        cleaned = _strip_noise_tags(html)
        md = h.handle(cleaned)
        md = re.sub(r"\n{3,}", "\n\n", md).strip()

        if len(md) > max_length:
            md = md[:max_length] + f"\n\n... (truncated at {max_length} chars)"

        return md

    except ImportError:
        return html_to_text(html, max_length)


def extract_with_selector(html: str, selector: str, max_length: int = 30000) -> str:
    """Extract content using CSS selector via BeautifulSoup."""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        for tag in soup.find_all(_STRIP_TAGS):
            tag.decompose()
        for sel in _NOISE_SELECTORS:
            for el in soup.select(sel):
                el.decompose()

        for sel in selector.split(","):
            sel = sel.strip()
            elements = soup.select(sel)
            if elements:
                text = "\n\n".join(el.get_text(separator="\n", strip=True) for el in elements)
                if len(text) > max_length:
                    text = text[:max_length] + f"\n\n... (truncated at {max_length} chars)"
                return text

        body = soup.find("body")
        if body:
            text = body.get_text(separator="\n", strip=True)
            if len(text) > max_length:
                text = text[:max_length]
            return text

        return soup.get_text(separator="\n", strip=True)[:max_length]

    except ImportError:
        return html_to_text(html, max_length)


def extract_title(html: str) -> str:
    """Extract page title from HTML."""
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else ""


def extract_meta_description(html: str) -> str:
    """Extract meta description from HTML."""
    match = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']',
        html, re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']description["\']',
            html, re.IGNORECASE,
        )
    return match.group(1).strip() if match else ""


def _strip_noise_tags(html: str) -> str:
    """Remove noise tags via regex (fast, no bs4 needed)."""
    for tag in _STRIP_TAGS:
        html = re.sub(
            rf"<{tag}[\s>].*?</{tag}>",
            "",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    return html


def _regex_strip(html: str, max_length: int) -> str:
    """Basic HTML to text via regex (fallback when html2text unavailable)."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    text = text.strip()
    return text[:max_length]

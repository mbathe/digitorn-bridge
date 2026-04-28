"""HTTP security - SSRF protection, URL validation, header masking.

All outbound requests MUST pass through ``validate_url()`` before execution.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from fnmatch import fnmatch
from typing import Any
from urllib.parse import urlparse


_SENSITIVE_HEADERS: set[str] = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "x-secret",
    "x-csrf-token",
}

_MASKED = "***masked***"


def mask_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy with sensitive header values replaced."""
    result: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in _SENSITIVE_HEADERS:
            result[k] = _MASKED
        else:
            result[k] = v
    return result


_PRIVATE_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
]


def is_private_ip(ip_str: str) -> bool:
    """Check if an IP address is private/reserved."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return any(addr in net for net in _PRIVATE_NETWORKS)


def _host_matches(host: str, patterns: list[str]) -> bool:
    """Check if host matches any pattern (exact or glob)."""
    host_lower = host.lower()
    for pattern in patterns:
        pattern_lower = pattern.lower()
        if fnmatch(host_lower, pattern_lower):
            return True
        if ":" in host_lower:
            hostname_only = host_lower.split(":")[0]
            if fnmatch(hostname_only, pattern_lower):
                return True
    return False


_ALLOWED_SCHEMES = {"http", "https"}


class ValidatedURL:
    """Result of URL validation with pinned IP to prevent DNS rebinding.

    The ``pinned_url`` replaces the hostname with the resolved IP so that
    httpx/aiohttp connects to the validated address - not a re-resolved one.
    The original ``Host`` header must be sent for TLS SNI and vhost routing.
    """

    __slots__ = ("original_url", "pinned_url", "hostname", "resolved_ip")

    def __init__(self, original: str, pinned: str, hostname: str, resolved_ip: str):
        self.original_url = original
        self.pinned_url = pinned
        self.hostname = hostname
        self.resolved_ip = resolved_ip


def validate_url(
    url: str,
    *,
    allowed_hosts: list[str] | None = None,
    blocked_hosts: list[str] | None = None,
) -> tuple[str | None, str, ValidatedURL | None]:
    """Validate a URL for safe outbound requests.

    Returns ``(error_message, sanitized_url, validated)``.
    If error_message is not None, the request MUST be rejected.
    ``validated.pinned_url`` should be used for the actual request
    to prevent DNS rebinding attacks.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL format.", url, None

    if parsed.scheme not in _ALLOWED_SCHEMES:
        return (
            f"Scheme '{parsed.scheme}' is not allowed. Only HTTP and HTTPS are supported.",
            url,
            None,
        )

    hostname = parsed.hostname
    if not hostname:
        return "URL has no hostname.", url, None

    if blocked_hosts and _host_matches(hostname, blocked_hosts):
        return f"Host '{hostname}' is blocked by policy.", url, None

    if allowed_hosts:
        if not _host_matches(hostname, allowed_hosts):
            return (
                f"Host '{hostname}' is not in the allowed hosts list.",
                url,
                None,
            )

    # Resolve DNS ONCE and pin the IP for the actual request.
    try:
        addr_infos = socket.getaddrinfo(hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return f"Cannot resolve hostname '{hostname}'.", url, None

    # If host is explicitly allowed, skip private IP check (trusted by admin).
    host_is_trusted = allowed_hosts and _host_matches(hostname, allowed_hosts)

    resolved_ip: str | None = None
    for family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        if is_private_ip(ip_str) and not host_is_trusted:
            return (
                f"Host '{hostname}' resolves to private/reserved IP {ip_str}. "
                "Requests to internal networks are blocked for security (SSRF protection). "
                "Add the host to constraints.allowed_hosts to allow it.",
                url,
                None,
            )
        if resolved_ip is None:
            resolved_ip = ip_str

    if resolved_ip is None:
        return f"Cannot resolve hostname '{hostname}'.", url, None

    # Build pinned URL: replace hostname with resolved IP.
    # httpx will connect to this IP directly - no second DNS lookup.
    pinned = _pin_url(parsed, resolved_ip)
    validated = ValidatedURL(
        original=url,
        pinned=pinned,
        hostname=hostname,
        resolved_ip=resolved_ip,
    )

    return None, url, validated


def _pin_url(parsed: Any, ip: str) -> str:
    """Replace hostname in a parsed URL with a resolved IP."""
    # For IPv6, wrap in brackets
    host_part = f"[{ip}]" if ":" in ip else ip
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or ""
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return f"{parsed.scheme}://{host_part}{port}{path}{query}{fragment}"


def format_bytes(n: int) -> str:
    """Human-readable byte size."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"

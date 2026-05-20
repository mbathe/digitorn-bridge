"""Channel security primitives."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from typing import Any

logger = logging.getLogger(__name__)

# Maximum nesting depth for inbound payloads
MAX_PAYLOAD_DEPTH = 10

# Maximum string value length in inbound payloads
MAX_STRING_LENGTH = 10_000

# Maximum number of keys in a single dict level
MAX_DICT_KEYS = 200

# Maximum number of items in a single list level
MAX_LIST_ITEMS = 500

# Patterns that look like secrets in outbound messages
_SECRET_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9\-]{20,}", re.ASCII),            # OpenAI (sk-proj-...)
    re.compile(r"sk-ant-[a-zA-Z0-9\-]{20,}", re.ASCII),       # Anthropic
    re.compile(r"xoxb-[0-9]{10,}-[a-zA-Z0-9]{20,}", re.ASCII),  # Slack bot
    re.compile(r"ghp_[a-zA-Z0-9]{36}", re.ASCII),             # GitHub PAT
    re.compile(r"glpat-[a-zA-Z0-9\-_]{20}", re.ASCII),        # GitLab PAT
    re.compile(r"AKIA[0-9A-Z]{16}", re.ASCII),                # AWS access key
    re.compile(r"eyJ[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{20,}", re.ASCII),  # JWT
    re.compile(r"Bearer\s+[a-zA-Z0-9_\-.]{20,}", re.ASCII),   # Bearer token
    re.compile(r"Basic\s+[a-zA-Z0-9+/=]{20,}", re.ASCII),     # Basic auth
    re.compile(r"dk_[a-zA-Z0-9]{4}_[a-zA-Z0-9]{20,}", re.ASCII),  # Digitorn API key
]

# Dangerous keys to strip from inbound payloads
_DANGEROUS_KEY_PREFIXES = ("__", "$$")
_DANGEROUS_KEYS = frozenset({
    "__proto__", "__class__", "__import__", "constructor",
    "__globals__", "__builtins__", "__subclasses__",
})

def generate_webhook_token() -> str:
    """Generate a cryptographically secure webhook path token."""
    return secrets.token_hex(32)

def verify_hmac_signature(
    payload_bytes: bytes,
    signature: str,
    secret: str,
    algorithm: str = "sha256",
) -> bool:
    """Verify HMAC signature with constant-time comparison."""
    if not signature or not secret:
        return False

    # Strip common prefixes (GitHub: "sha256=...", Stripe: "v1=...")
    if "=" in signature:
        signature = signature.split("=", 1)[-1]

    hash_func = getattr(hashlib, algorithm, None)
    if hash_func is None:
        logger.warning("channel_hmac_unknown_algorithm algo=%s", algorithm)
        return False

    expected = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hash_func,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)

def verify_api_key(provided: str, expected: str) -> bool:
    """Verify API key with constant-time comparison."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)

def sanitize_payload(data: Any, *, _depth: int = 0) -> Any:
    """Sanitize an inbound payload to prevent injection and DoS."""
    if _depth > MAX_PAYLOAD_DEPTH:
        return "[depth_limit_exceeded]"

    if isinstance(data, dict):
        result = {}
        count = 0
        for key, value in data.items():
            if count >= MAX_DICT_KEYS:
                break
            if not isinstance(key, str):
                key = str(key)
            # Strip dangerous keys
            if key in _DANGEROUS_KEYS:
                continue
            if any(key.startswith(p) for p in _DANGEROUS_KEY_PREFIXES):
                continue
            result[key] = sanitize_payload(value, _depth=_depth + 1)
            count += 1
        return result

    if isinstance(data, list):
        return [
            sanitize_payload(item, _depth=_depth + 1)
            for item in data[:MAX_LIST_ITEMS]
        ]

    if isinstance(data, str):
        if len(data) > MAX_STRING_LENGTH:
            return data[:MAX_STRING_LENGTH] + f"... [truncated, {len(data)} chars total]"
        return data

    if isinstance(data, bytes):
        try:
            decoded = data.decode("utf-8")
            return decoded[:MAX_STRING_LENGTH]
        except UnicodeDecodeError:
            return ""

    if isinstance(data, (int, float, bool)) or data is None:
        return data

    # Unknown type - convert to string safely
    return str(data)[:MAX_STRING_LENGTH]

def check_payload_size(raw_bytes: bytes, max_bytes: int) -> tuple[bool, str]:
    """Check raw payload size BEFORE parsing."""
    size = len(raw_bytes)
    if size > max_bytes:
        return False, (
            f"Payload too large: {size:,} bytes "
            f"(limit: {max_bytes:,} bytes)"
        )
    return True, ""

def check_content_type(
    content_type: str,
    allowed: frozenset[str] | None = None,
) -> tuple[bool, str]:
    """Validate Content-Type against whitelist."""
    if allowed is None:
        allowed = frozenset({
            "application/json",
            "application/x-www-form-urlencoded",
            "text/plain",
        })

    # Extract base type (ignore charset, boundary, etc.)
    base_type = content_type.split(";")[0].strip().lower() if content_type else ""

    if base_type not in allowed:
        return False, (
            f"Unsupported Content-Type: '{base_type}'. "
            f"Allowed: {', '.join(sorted(allowed))}"
        )
    return True, ""

def filter_secrets(text: str) -> str:
    """Scan outbound text for secret-like patterns and redact them."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text

def filter_secrets_in_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively filter secrets from a dict (e.g., structured_data)."""
    result = {}
    for key, value in data.items():
        if isinstance(value, str):
            result[key] = filter_secrets(value)
        elif isinstance(value, dict):
            result[key] = filter_secrets_in_dict(value)
        elif isinstance(value, list):
            result[key] = [
                filter_secrets(v) if isinstance(v, str)
                else filter_secrets_in_dict(v) if isinstance(v, dict)
                else v
                for v in value
            ]
        else:
            result[key] = value
    return result

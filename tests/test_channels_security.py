"""Security tests for the channels module.

Covers:
- HMAC signature verification (constant-time, prefix handling)
- API key verification (constant-time)
- Payload size limits (before JSON parsing)
- Content-Type validation
- Payload sanitization (dunder keys, depth, string length, binary)
- Outbound secret filtering (API keys, JWTs, tokens)
- Webhook token generation
- Template safety (no secret/env runtime access, no recursion, max length)
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod

import pytest

from digitorn.modules.channels.security import (
    MAX_DICT_KEYS,
    MAX_LIST_ITEMS,
    MAX_PAYLOAD_DEPTH,
    MAX_STRING_LENGTH,
    check_content_type,
    check_payload_size,
    filter_secrets,
    filter_secrets_in_dict,
    generate_webhook_token,
    sanitize_payload,
    verify_api_key,
    verify_hmac_signature,
)
from digitorn.modules.channels.template import render, resolve_dotpath


# ── HMAC Signature Verification ──────────────────────────────────────


class TestHmacVerification:
    def test_valid_signature(self):
        secret = "webhook_secret_123"
        body = b'{"event": "push"}'
        sig = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert verify_hmac_signature(body, sig, secret) is True

    def test_valid_signature_with_prefix(self):
        secret = "secret"
        body = b"test"
        sig = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert verify_hmac_signature(body, f"sha256={sig}", secret) is True

    def test_invalid_signature(self):
        assert verify_hmac_signature(b"test", "invalid_hex", "secret") is False

    def test_empty_signature(self):
        assert verify_hmac_signature(b"test", "", "secret") is False

    def test_empty_secret(self):
        assert verify_hmac_signature(b"test", "some_sig", "") is False

    def test_wrong_body(self):
        secret = "secret"
        body = b"original"
        sig = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert verify_hmac_signature(b"tampered", sig, secret) is False

    def test_sha1_algorithm(self):
        secret = "secret"
        body = b"test"
        sig = hmac_mod.new(secret.encode(), body, hashlib.sha1).hexdigest()
        assert verify_hmac_signature(body, sig, secret, algorithm="sha1") is True

    def test_unknown_algorithm(self):
        assert verify_hmac_signature(b"test", "sig", "secret", algorithm="md4") is False


# ── API Key Verification ─────────────────────────────────────────────


class TestApiKeyVerification:
    def test_matching_keys(self):
        assert verify_api_key("dk_abcd_1234567890abcdef", "dk_abcd_1234567890abcdef") is True

    def test_mismatched_keys(self):
        assert verify_api_key("key_a", "key_b") is False

    def test_empty_provided(self):
        assert verify_api_key("", "expected") is False

    def test_empty_expected(self):
        assert verify_api_key("provided", "") is False

    def test_both_empty(self):
        assert verify_api_key("", "") is False


# ── Payload Size ─────────────────────────────────────────────────────


class TestPayloadSize:
    def test_within_limit(self):
        ok, err = check_payload_size(b"small", 1024)
        assert ok is True
        assert err == ""

    def test_exceeds_limit(self):
        ok, err = check_payload_size(b"x" * 1000, 100)
        assert ok is False
        assert "1,000 bytes" in err
        assert "100 bytes" in err

    def test_exact_limit(self):
        ok, _ = check_payload_size(b"x" * 100, 100)
        assert ok is True

    def test_empty_payload(self):
        ok, _ = check_payload_size(b"", 100)
        assert ok is True


# ── Content-Type ─────────────────────────────────────────────────────


class TestContentType:
    def test_json(self):
        ok, _ = check_content_type("application/json")
        assert ok is True

    def test_json_with_charset(self):
        ok, _ = check_content_type("application/json; charset=utf-8")
        assert ok is True

    def test_form_urlencoded(self):
        ok, _ = check_content_type("application/x-www-form-urlencoded")
        assert ok is True

    def test_text_plain(self):
        ok, _ = check_content_type("text/plain")
        assert ok is True

    def test_xml_rejected(self):
        ok, err = check_content_type("text/xml")
        assert ok is False
        assert "Unsupported" in err

    def test_html_rejected(self):
        ok, _ = check_content_type("text/html")
        assert ok is False

    def test_empty_rejected(self):
        ok, _ = check_content_type("")
        assert ok is False

    def test_custom_whitelist(self):
        ok, _ = check_content_type("text/xml", allowed=frozenset({"text/xml"}))
        assert ok is True


# ── Payload Sanitization ─────────────────────────────────────────────


class TestPayloadSanitization:
    def test_strips_dunder_keys(self):
        result = sanitize_payload({
            "__proto__": "bad",
            "__class__": "bad",
            "__import__": "bad",
            "name": "good",
        })
        assert "__proto__" not in result
        assert "__class__" not in result
        assert "__import__" not in result
        assert result["name"] == "good"

    def test_strips_dollar_keys(self):
        result = sanitize_payload({"$$ref": "bad", "ok": 1})
        assert "$$ref" not in result
        assert result["ok"] == 1

    def test_truncates_long_strings(self):
        long_str = "a" * (MAX_STRING_LENGTH + 100)
        result = sanitize_payload({"text": long_str})
        assert len(result["text"]) < len(long_str)
        assert "truncated" in result["text"]

    def test_limits_nesting_depth(self):
        deep = {"level": 0}
        current = deep
        for i in range(1, MAX_PAYLOAD_DEPTH + 5):
            current["child"] = {"level": i}
            current = current["child"]
        current["value"] = "deep"

        result = sanitize_payload(deep)
        # Navigate to the depth limit
        node = result
        depth = 0
        while isinstance(node, dict) and "child" in node:
            node = node["child"]
            depth += 1
        # Should hit the limit string
        assert depth <= MAX_PAYLOAD_DEPTH + 1

    def test_limits_dict_keys(self):
        big_dict = {f"key_{i}": i for i in range(MAX_DICT_KEYS + 50)}
        result = sanitize_payload(big_dict)
        assert len(result) <= MAX_DICT_KEYS

    def test_limits_list_items(self):
        big_list = list(range(MAX_LIST_ITEMS + 50))
        result = sanitize_payload(big_list)
        assert len(result) <= MAX_LIST_ITEMS

    def test_handles_bytes(self):
        result = sanitize_payload({"data": b"hello"})
        assert result["data"] == "hello"

    def test_handles_invalid_bytes(self):
        result = sanitize_payload({"data": b"\xff\xfe\xfd"})
        assert result["data"] == ""  # Non-UTF8 → empty

    def test_preserves_primitives(self):
        result = sanitize_payload({
            "int": 42,
            "float": 3.14,
            "bool": True,
            "none": None,
            "str": "hello",
        })
        assert result == {"int": 42, "float": 3.14, "bool": True, "none": None, "str": "hello"}

    def test_nested_sanitization(self):
        result = sanitize_payload({
            "ok": {"__proto__": "bad", "name": "good"},
            "list": [{"__class__": "bad", "val": 1}],
        })
        assert "__proto__" not in result["ok"]
        assert result["ok"]["name"] == "good"
        assert "__class__" not in result["list"][0]
        assert result["list"][0]["val"] == 1


# ── Secret Filtering ─────────────────────────────────────────────────


class TestSecretFiltering:
    def test_openai_key(self):
        msg = "key: sk-proj-abc123def456ghi789jkl012mno345pqr678"
        assert "sk-proj-" not in filter_secrets(msg)
        assert "[REDACTED]" in filter_secrets(msg)

    def test_anthropic_key(self):
        msg = "key: sk-ant-api03-abc123def456ghi789jkl012mno345pqr"
        assert "sk-ant-" not in filter_secrets(msg)

    def test_github_pat(self):
        msg = "token: ghp_1234567890abcdefghijklmnopqrstuvwxyz"
        assert "ghp_" not in filter_secrets(msg)

    def test_aws_access_key(self):
        msg = "key: AKIAIOSFODNN7EXAMPLE"
        assert "AKIA" not in filter_secrets(msg)

    def test_jwt_token(self):
        msg = "auth: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        assert "eyJ" not in filter_secrets(msg)

    def test_bearer_token(self):
        msg = "Authorization: Bearer abc123def456ghi789jkl012mno345pqr"
        assert "Bearer" not in filter_secrets(msg)

    def test_digitorn_api_key(self):
        msg = "key: dk_abcd_1234567890abcdefghij"
        assert "dk_" not in filter_secrets(msg)

    def test_preserves_normal_text(self):
        msg = "Hello, this is a normal message with no secrets."
        assert filter_secrets(msg) == msg

    def test_multiple_secrets(self):
        msg = "key1: sk-abc12345678901234567890 key2: ghp_1234567890abcdefghijklmnopqrstuvwxyz"
        filtered = filter_secrets(msg)
        assert filtered.count("[REDACTED]") == 2

    def test_filter_in_dict(self):
        data = {
            "message": "Token is sk-abc12345678901234567890",
            "nested": {"key": "ghp_1234567890abcdefghijklmnopqrstuvwxyz"},
        }
        result = filter_secrets_in_dict(data)
        assert "[REDACTED]" in result["message"]
        assert "[REDACTED]" in result["nested"]["key"]


# ── Webhook Token Generation ─────────────────────────────────────────


class TestWebhookToken:
    def test_length(self):
        token = generate_webhook_token()
        assert len(token) == 64  # 32 bytes = 64 hex chars

    def test_uniqueness(self):
        tokens = {generate_webhook_token() for _ in range(100)}
        assert len(tokens) == 100  # All unique

    def test_hex_chars_only(self):
        token = generate_webhook_token()
        assert all(c in "0123456789abcdef" for c in token)


# ── Template Safety ──────────────────────────────────────────────────


class TestTemplateSafety:
    def test_blocks_secret_references(self):
        result = render("key={{secret.API_KEY}}", {})
        assert "{{secret.API_KEY}}" in result  # Left as-is

    def test_blocks_env_references(self):
        result = render("val={{env.DATABASE_URL}}", {})
        assert "{{env.DATABASE_URL}}" in result

    def test_no_recursive_expansion(self):
        result = render("{{val}}", {"val": "{{other}}", "other": "deep"})
        assert result == "{{other}}"  # Single pass, no recursion

    def test_max_output_length(self):
        from digitorn.modules.channels.template import MAX_OUTPUT_LENGTH
        big_var = "x" * (MAX_OUTPUT_LENGTH + 1000)
        result = render("{{data}}", {"data": big_var})
        assert len(result) <= MAX_OUTPUT_LENGTH + 20  # +20 for "... [truncated]"

    def test_unresolved_placeholders_preserved(self):
        result = render("Hello {{unknown}}", {})
        assert result == "Hello {{unknown}}"

    def test_safe_dict_rendering(self):
        result = render("data={{obj}}", {"obj": {"a": 1, "b": "x"}})
        assert '"a": 1' in result or '"a":1' in result

    def test_dotpath_resolution(self):
        data = {"event": {"data": {"from": "+33612345678"}}}
        assert resolve_dotpath("event.data.from", data) == "+33612345678"

    def test_dotpath_list_index(self):
        data = {"items": [{"name": "first"}, {"name": "second"}]}
        assert resolve_dotpath("items.0.name", data) == "first"
        assert resolve_dotpath("items.1.name", data) == "second"

    def test_dotpath_out_of_bounds(self):
        data = {"items": [{"name": "only"}]}
        assert resolve_dotpath("items.5.name", data) is None

    def test_dotpath_missing_key(self):
        data = {"a": {"b": 1}}
        assert resolve_dotpath("a.c", data) is None

    def test_empty_template(self):
        assert render("", {"x": 1}) == ""

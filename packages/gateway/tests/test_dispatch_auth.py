"""Real tests for ``auth_dispatchers``.

These functions decide what HTTP-level credential bytes the dispatch
path puts on the wire. A bug here = a leaked or wrong credential at
the boundary. Every dispatcher path is tested for correct outputs.
"""
from __future__ import annotations

import base64

import pytest


@pytest.fixture
def disp():
    from digitorn_gateway.auth_dispatchers import dispatch_auth
    return dispatch_auth


# ── api_key family ──────────────────────────────────────────────────


def test_api_key_basic(disp):
    inj = disp("api_key", {"value": "sk-test-1234"})
    assert inj.api_key == "sk-test-1234"
    assert inj.api_base is None
    assert inj.extra_headers == {}
    assert inj.extra_body == {}


def test_api_key_empty(disp):
    inj = disp("api_key", {})
    assert inj.api_key == ""


def test_api_key_header_default(disp):
    inj = disp("api_key_header", {"value": "k"})
    assert inj.extra_headers == {"x-api-key": "k"}


def test_api_key_header_custom(disp):
    inj = disp("api_key_header", {"header_name": "X-Foo-Auth", "value": "k"})
    assert inj.extra_headers == {"X-Foo-Auth": "k"}


# ── basic_auth ──────────────────────────────────────────────────────


def test_basic_auth(disp):
    inj = disp("basic_auth", {"username": "user", "password": "pass"})
    expected = base64.b64encode(b"user:pass").decode()
    assert inj.extra_headers == {"Authorization": f"Basic {expected}"}


def test_basic_auth_handles_unicode(disp):
    inj = disp("basic_auth", {"username": "café", "password": "naïve"})
    # Just make sure we don't raise on non-ASCII; the decoded form
    # round-trips to the original UTF-8 bytes.
    raw = base64.b64decode(
        inj.extra_headers["Authorization"].split(" ", 1)[1].encode(),
    )
    assert raw == "café:naïve".encode("utf-8")


# ── oauth2 + claude_code + github_copilot ──────────────────────────


def test_oauth2(disp):
    inj = disp("oauth2", {"access_token": "tok-abc"})
    assert inj.api_key == "tok-abc"


def test_claude_code_includes_oauth_betas(disp):
    inj = disp("claude_code", {"access_token": "claude-tok"})
    assert inj.api_key == "claude-tok"
    assert inj.extra_headers.get("x-app") == "cli"
    # The anthropic-beta header is required for the OAuth path.
    assert "oauth-2025-04-20" in inj.extra_headers.get("anthropic-beta", "")


def test_github_copilot_uses_short_lived_api_key(disp):
    """The bearer is the SHORT-LIVED ``api_key`` (minted by the
    refresher from the oauth_token), NOT the long-lived oauth_token."""
    inj = disp("github_copilot", {
        "oauth_token": "ghu_long",
        "api_key": "tid=short",
    })
    assert inj.api_key == "tid=short"
    # The dispatcher returns minimal Authinject; api_base + Editor
    # headers are layered by the cache from provider.metadata.
    assert inj.api_base is None
    assert inj.extra_headers == {}


def test_github_copilot_empty_api_key_yields_empty_bearer(disp):
    """Defensive: a refresher-not-yet-run cred yields empty bearer.
    The dispatch path detects this and falls through to the next route."""
    inj = disp("github_copilot", {"oauth_token": "ghu_x", "api_key": ""})
    assert inj.api_key == ""


# ── multi-field family ──────────────────────────────────────────────


def test_aws_bedrock_lifts_into_extra_body(disp):
    inj = disp("aws_bedrock", {
        "aws_access_key_id": "AKIA",
        "aws_secret_access_key": "secret",
        "aws_region_name": "eu-west-3",
    })
    aws = inj.extra_body["_aws_bedrock_kwargs"]
    assert aws["aws_access_key_id"] == "AKIA"
    assert aws["aws_secret_access_key"] == "secret"
    assert aws["aws_region_name"] == "eu-west-3"
    assert "aws_session_token" not in aws  # absent when not provided


def test_aws_bedrock_optional_session_token(disp):
    inj = disp("aws_bedrock", {
        "aws_access_key_id": "AKIA",
        "aws_secret_access_key": "secret",
        "aws_region_name": "us-east-1",
        "aws_session_token": "STSToken",
    })
    assert inj.extra_body["_aws_bedrock_kwargs"]["aws_session_token"] == "STSToken"


def test_aws_bedrock_default_region(disp):
    inj = disp("aws_bedrock", {
        "aws_access_key_id": "k", "aws_secret_access_key": "s",
    })
    assert inj.extra_body["_aws_bedrock_kwargs"]["aws_region_name"] == "us-east-1"


def test_vertex_ai_lifts_into_extra_body(disp):
    inj = disp("vertex_ai", {
        "project_id": "my-project",
        "location": "us-east5",
        "service_account_json": '{"type":"service_account"}',
    })
    vk = inj.extra_body["_vertex_kwargs"]
    assert vk["vertex_project"] == "my-project"
    assert vk["vertex_location"] == "us-east5"
    assert "service_account" in vk["vertex_credentials"]


def test_vertex_ai_default_location(disp):
    inj = disp("vertex_ai", {
        "project_id": "p", "service_account_json": "{}",
    })
    assert inj.extra_body["_vertex_kwargs"]["vertex_location"] == "us-east5"


def test_azure_openai_lifts(disp):
    inj = disp("azure_openai", {
        "api_key": "azkey",
        "endpoint": "https://x.openai.azure.com",
        "api_version": "2024-08-01-preview",
        "deployment_name": "gpt-4o-prod",
    })
    assert inj.api_key == "azkey"
    assert inj.api_base == "https://x.openai.azure.com"
    az = inj.extra_body["_azure_kwargs"]
    assert az["api_version"] == "2024-08-01-preview"
    assert az["_default_deployment"] == "gpt-4o-prod"


def test_azure_openai_no_deployment_omits_sentinel(disp):
    """When deployment_name is missing, _default_deployment should NOT be set
    (so the dispatch path doesn't try to clobber the model)."""
    inj = disp("azure_openai", {
        "api_key": "k",
        "endpoint": "https://x.openai.azure.com",
        "api_version": "v1",
    })
    az = inj.extra_body.get("_azure_kwargs", {})
    assert "_default_deployment" not in az


# ── multi_field generic ──────────────────────────────────────────────


def test_multi_field_passes_through_extra_body(disp):
    inj = disp("multi_field", {"a": "1", "b": "2"})
    assert inj.extra_body == {"_multi_field_auth": {"a": "1", "b": "2"}}


# ── unknown type ─────────────────────────────────────────────────────


def test_unknown_auth_type_returns_empty_inject(disp):
    """Unknown auth types must NOT crash the dispatch path. Empty
    AuthInject lets the cache return None which surfaces a clean 404."""
    inj = disp("does_not_exist", {"value": "x"})
    assert not inj.api_key
    assert inj.api_base is None
    assert inj.extra_headers == {}
    assert inj.extra_body == {}


def test_unknown_auth_type_swallows_exceptions(disp):
    """The dispatcher MUST swallow internal crashes - a buggy custom
    handler must never propagate to the chat completion handler."""
    from digitorn_gateway import auth_dispatchers as ad

    def boom(s):
        raise RuntimeError("oops")

    ad.DISPATCHERS["explosive"] = boom
    try:
        inj = disp("explosive", {"x": "y"})
        # Should return AuthInject() default, not raise.
        assert inj.api_key in (None, "")
    finally:
        del ad.DISPATCHERS["explosive"]


# ── validate_secret_data ────────────────────────────────────────────


def test_validate_required_fields_present():
    from digitorn_gateway.auth_dispatchers import validate_secret_data
    ok, missing = validate_secret_data("vertex_ai", {
        "project_id": "p",
        "location": "us-east5",
        "service_account_json": "{}",
    })
    assert ok is True
    assert missing == []


def test_validate_missing_required_field():
    from digitorn_gateway.auth_dispatchers import validate_secret_data
    ok, missing = validate_secret_data("vertex_ai", {
        "project_id": "p",
        "location": "us-east5",
        # service_account_json missing
    })
    assert ok is False
    assert missing == ["service_account_json"]


def test_validate_empty_string_counts_as_missing():
    """Empty strings should NOT pass validation - that's how malformed
    JSON parses come through. We treat them like missing fields."""
    from digitorn_gateway.auth_dispatchers import validate_secret_data
    ok, missing = validate_secret_data("api_key", {"value": ""})
    assert ok is False
    assert "value" in missing


def test_validate_optional_fields_can_be_absent():
    from digitorn_gateway.auth_dispatchers import validate_secret_data
    ok, missing = validate_secret_data("aws_bedrock", {
        "aws_access_key_id": "k",
        "aws_secret_access_key": "s",
        "aws_region_name": "us-east-1",
        # aws_session_token is optional
    })
    assert ok is True
    assert missing == []


def test_validate_unknown_type_passes():
    """Unknown auth types pass validation by default (custom routers
    accept whatever the operator hands them)."""
    from digitorn_gateway.auth_dispatchers import validate_secret_data
    ok, missing = validate_secret_data("custom", {})
    assert ok is True

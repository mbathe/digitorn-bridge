"""Per-auth-type credential injectors for the LLM dispatch path.

Each provider declares an ``auth_type`` (e.g. ``api_key``,
``api_key_header``, ``basic_auth``, ``multi_field``, ``oauth2``,
``claude_code``, ``custom``) and an ``auth_schema`` listing the named
fields its credentials carry. The dispatcher is the function that, at
call time, takes:

    secret_data: dict[str, str]   # decrypted from the cache
    extra_headers: dict[str, str] # we may add to it
    litellm_kwargs: dict           # api_key / api_base / extra_body etc.

and mutates them in-place so LiteLLM (or our custom router) sends the
correct credentials upstream.

Adding a new provider type = adding one entry to ``DISPATCHERS``. The
runtime never branches on a provider slug; it dispatches on auth_type.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class AuthInject:
    """Container the dispatcher mutates. Decoupled from LiteLLM so we
    can unit-test the injector without spinning the full library."""

    api_key: str | None = None
    api_base: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuthFieldSpec:
    """One row of a provider's ``auth_schema``."""

    name: str
    label: str
    kind: str  # "secret" | "string" | "url" | "datetime"
    secret: bool = False
    required: bool = True


# Schemas declared in code so the dashboard can render forms even
# before any provider row exists in the DB. The seed walks this map
# to populate ``gateway_providers.auth_schema`` on first boot.
SCHEMAS: dict[str, list[AuthFieldSpec]] = {
    "api_key": [
        AuthFieldSpec("value", "API key", "secret", secret=True),
    ],
    "api_key_header": [
        AuthFieldSpec("header_name", "Header name", "string"),
        AuthFieldSpec("value", "API key", "secret", secret=True),
    ],
    "basic_auth": [
        AuthFieldSpec("username", "Username", "string"),
        AuthFieldSpec("password", "Password", "secret", secret=True),
    ],
    "multi_field": [
        AuthFieldSpec("access_key_id", "Access Key ID", "string"),
        AuthFieldSpec("secret_access_key", "Secret Access Key", "secret", secret=True),
        AuthFieldSpec("region", "Region", "string", required=False),
    ],
    "aws_bedrock": [
        AuthFieldSpec("aws_access_key_id", "AWS Access Key ID", "string"),
        AuthFieldSpec("aws_secret_access_key", "AWS Secret Access Key", "secret", secret=True),
        AuthFieldSpec("aws_region_name", "AWS Region", "string"),
        AuthFieldSpec("aws_session_token", "AWS Session Token (STS)", "secret", secret=True, required=False),
    ],
    "oauth2": [
        AuthFieldSpec("access_token", "Access token", "secret", secret=True),
        AuthFieldSpec("refresh_token", "Refresh token", "secret", secret=True, required=False),
        AuthFieldSpec("expires_at", "Expires at (unix seconds)", "datetime", required=False),
        AuthFieldSpec("token_endpoint", "Token endpoint URL", "url", required=False),
        AuthFieldSpec("client_id", "Client ID", "string", required=False),
    ],
    "claude_code": [
        AuthFieldSpec("access_token", "Access token", "secret", secret=True),
        AuthFieldSpec("refresh_token", "Refresh token", "secret", secret=True, required=False),
        AuthFieldSpec("expires_at", "Expires at (unix milliseconds)", "datetime", required=False),
    ],
    "github_copilot": [
        # GitHub Copilot exchanges a long-lived OAuth refresh token
        # ("device flow") for a short-lived API key. We let operators
        # paste both - the runtime refreshes the API key when needed.
        AuthFieldSpec("oauth_token", "GitHub OAuth token (gho_...)", "secret", secret=True),
        AuthFieldSpec("api_key", "Copilot API key (cached)", "secret", secret=True, required=False),
        AuthFieldSpec("api_key_expires_at", "API key expires at", "datetime", required=False),
    ],
    "custom": [
        # Free-form: operator supplies whatever fields the custom
        # router knows how to consume.
    ],
}


def schema_for(auth_type: str) -> list[dict[str, Any]]:
    """Return the JSON-serialisable shape stored in ``auth_schema``."""
    fields = SCHEMAS.get(auth_type, [])
    return [
        {
            "name": f.name,
            "label": f.label,
            "kind": f.kind,
            "secret": f.secret,
            "required": f.required,
        }
        for f in fields
    ]


# ── Dispatchers ────────────────────────────────────────────────────


Dispatcher = Callable[[dict[str, str]], AuthInject]


def _dispatch_api_key(s: dict[str, str]) -> AuthInject:
    return AuthInject(api_key=s.get("value", ""))


def _dispatch_api_key_header(s: dict[str, str]) -> AuthInject:
    header = s.get("header_name") or "x-api-key"
    return AuthInject(extra_headers={header: s.get("value", "")})


def _dispatch_basic_auth(s: dict[str, str]) -> AuthInject:
    raw = f"{s.get('username','')}:{s.get('password','')}".encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    return AuthInject(extra_headers={"Authorization": f"Basic {encoded}"})


def _dispatch_multi_field(s: dict[str, str]) -> AuthInject:
    """Generic multi-field: no built-in auth handling. The CustomRouter
    or a per-provider extension is expected to read ``extra_body``."""
    return AuthInject(extra_body={"_multi_field_auth": dict(s)})


def _dispatch_oauth2(s: dict[str, str]) -> AuthInject:
    """OAuth2: we use the access_token as a Bearer. Refresh is handled
    out-of-band by ``oauth_refresher.py`` BEFORE we hit dispatch -
    by the time we read the cache, ``access_token`` is fresh."""
    return AuthInject(api_key=s.get("access_token", ""))


def _dispatch_claude_code(s: dict[str, str]) -> AuthInject:
    """The Claude Code OAuth flow: bearer token + a couple of headers
    Anthropic checks for to allow the OAuth path. Mirrors the daemon's
    anthropic provider when ``api_key == 'claude-code'``."""
    inj = AuthInject(api_key=s.get("access_token", ""))
    inj.extra_headers["x-app"] = "cli"
    inj.extra_headers["anthropic-beta"] = (
        "oauth-2025-04-20,claude-code-20250219"
    )
    return inj


def _dispatch_github_copilot(s: dict[str, str]) -> AuthInject:
    """GitHub Copilot: we use the SHORT-LIVED ``api_key`` as the bearer.
    The refresher swaps it via the long-lived ``oauth_token`` when it's
    near expiry - same idea as the daemon's oauth refresh loop."""
    return AuthInject(api_key=s.get("api_key", ""))


def _dispatch_custom(s: dict[str, str]) -> AuthInject:
    """Custom: we hand the whole dict to the CustomRouter via extra_body."""
    return AuthInject(extra_body={"_custom_auth": dict(s)})


def _dispatch_aws_bedrock(s: dict[str, str]) -> AuthInject:
    """AWS Bedrock uses SigV4 signing, not Bearer auth. LiteLLM handles
    the signing internally when we pass ``aws_access_key_id`` /
    ``aws_secret_access_key`` / ``aws_region_name`` as kwargs to
    ``litellm.acompletion``. We stash those in ``extra_body`` under a
    reserved sentinel key; the dispatch layer in ``llm_call.py``
    detects it and lifts them out into kwargs."""
    aws_kwargs = {
        "aws_access_key_id": s.get("aws_access_key_id", ""),
        "aws_secret_access_key": s.get("aws_secret_access_key", ""),
        "aws_region_name": s.get("aws_region_name", "us-east-1"),
    }
    if s.get("aws_session_token"):
        aws_kwargs["aws_session_token"] = s["aws_session_token"]
    return AuthInject(extra_body={"_aws_bedrock_kwargs": aws_kwargs})


DISPATCHERS: dict[str, Dispatcher] = {
    "api_key": _dispatch_api_key,
    "api_key_header": _dispatch_api_key_header,
    "basic_auth": _dispatch_basic_auth,
    "multi_field": _dispatch_multi_field,
    "oauth2": _dispatch_oauth2,
    "claude_code": _dispatch_claude_code,
    "github_copilot": _dispatch_github_copilot,
    "aws_bedrock": _dispatch_aws_bedrock,
    "custom": _dispatch_custom,
}


def dispatch_auth(auth_type: str, secret_data: dict[str, str]) -> AuthInject:
    """Resolve a credential for the dispatch path. Returns an empty
    ``AuthInject`` (no key, no headers) when the type is unknown - the
    caller will surface a clean ``model_not_provided_by_digitorn`` 404.
    """
    fn = DISPATCHERS.get(auth_type)
    if fn is None:
        logger.warning("dispatch_auth: unknown auth_type %s", auth_type)
        return AuthInject()
    try:
        return fn(secret_data or {})
    except Exception as exc:  # pragma: no cover
        logger.error(
            "dispatch_auth crashed (auth_type=%s): %s", auth_type, exc,
        )
        return AuthInject()


def validate_secret_data(
    auth_type: str, secret: dict[str, str],
) -> tuple[bool, list[str]]:
    """Check ``secret`` against the schema for ``auth_type``. Returns
    ``(ok, missing_required_field_names)``. Used by the CRUD endpoints
    to reject malformed credential payloads with a 400 instead of
    storing a half-broken row."""
    spec = SCHEMAS.get(auth_type)
    if spec is None:
        return True, []
    missing = [f.name for f in spec if f.required and not (secret or {}).get(f.name)]
    return len(missing) == 0, missing

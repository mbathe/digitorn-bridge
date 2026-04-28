"""OAuth 2.0 provider registry.

Loads ``~/.digitorn/oauth_providers.toml`` at daemon startup and
caches the known providers. Each entry contains everything the
daemon needs to run a 3-legged OAuth flow on behalf of a user:

    - ``auth_url``      - provider's authorization endpoint
    - ``token_url``     - provider's token exchange + refresh endpoint
    - ``client_id``     - the daemon's registered OAuth app id
    - ``client_secret`` - the daemon's registered OAuth app secret
    - ``default_scopes``- scopes requested when the app doesn't specify
    - ``auth_style``    - "basic" | "post" - how client credentials
                          are sent during the token exchange
    - ``redirect_uri``  - where the provider redirects the browser
                          after the user consents (daemon callback)

Secrets are read from ``~/.digitorn/oauth_providers.toml`` directly,
OR from env vars referenced by ``client_id_env`` / ``client_secret_env``
for Docker / k8s deployments where config files are a bad fit.

On first boot, if the config file doesn't exist, the daemon writes
a **commented-out template** so the admin can see what's possible
and enable the providers they want.

Five providers ship with the daemon: Notion, Google, GitHub, Slack,
Discord. More can be added by the admin simply by editing the file.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


DEFAULT_PATH = Path.home() / ".digitorn" / "oauth_providers.toml"
DEFAULT_REDIRECT_URI = "http://localhost:8000/api/oauth/callback"


@dataclass
class OAuthProviderConfig:
    """In-memory representation of one OAuth provider entry."""

    name: str
    auth_url: str
    token_url: str
    client_id: str
    client_secret: str
    default_scopes: list[str] = field(default_factory=list)
    redirect_uri: str = DEFAULT_REDIRECT_URI
    # "basic": send client_id/client_secret as HTTP Basic Auth header
    # "post":  send them as form-urlencoded body fields
    auth_style: str = "post"
    # Any extra query params added to the auth URL (e.g. Notion's
    # ``owner=user`` flag)
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    # Revocation endpoint (optional - not every provider supports it)
    revoke_url: str = ""

    def is_configured(self) -> bool:
        """True iff client_id AND client_secret are both present."""
        return bool(self.client_id) and bool(self.client_secret)


# ────────────────────────────────────────────────────────────────────
# Built-in provider catalog (5 well-known OAuth2 services)
# ────────────────────────────────────────────────────────────────────
#
# These are the URLs + default scopes - the actual client_id and
# client_secret come from the file / env vars. The catalog is here
# so an admin doesn't need to look up URLs in provider docs.

BUILTIN_PROVIDERS: dict[str, dict[str, Any]] = {
    "notion": {
        "auth_url": "https://api.notion.com/v1/oauth/authorize",
        "token_url": "https://api.notion.com/v1/oauth/token",
        "default_scopes": [],  # Notion uses workspace-level permissions
        "auth_style": "basic",
        "extra_auth_params": {"owner": "user"},
    },
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "default_scopes": ["openid", "email", "profile"],
        "auth_style": "post",
        "revoke_url": "https://oauth2.googleapis.com/revoke",
        "extra_auth_params": {
            "access_type": "offline",  # required to get a refresh_token
            "prompt": "consent",
        },
    },
    "github": {
        "auth_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "default_scopes": ["repo", "user:email"],
        "auth_style": "post",
    },
    "slack": {
        "auth_url": "https://slack.com/oauth/v2/authorize",
        "token_url": "https://slack.com/api/oauth.v2.access",
        "default_scopes": ["chat:write", "channels:read"],
        "auth_style": "post",
    },
    "discord": {
        "auth_url": "https://discord.com/api/oauth2/authorize",
        "token_url": "https://discord.com/api/oauth2/token",
        "default_scopes": ["identify", "guilds"],
        "auth_style": "post",
        "revoke_url": "https://discord.com/api/oauth2/token/revoke",
    },
}


TEMPLATE_TOML = """# Digitorn OAuth providers
# ========================
#
# Configure the OAuth credentials the daemon uses to initiate
# authorization flows on behalf of its users.
#
# ONE section per provider. The daemon bundles URL defaults for 5
# well-known providers (notion, google, github, slack, discord) -
# you only need to provide your client_id and client_secret. For
# custom providers, declare every field explicitly.
#
# You can either inline the secrets here OR reference env vars:
#
#     [notion]
#     client_id = "your-client-id"
#     client_secret = "your-client-secret"
#
# …or, recommended for production:
#
#     [notion]
#     client_id_env = "DIGITORN_NOTION_CLIENT_ID"
#     client_secret_env = "DIGITORN_NOTION_CLIENT_SECRET"
#
# The redirect_uri MUST be registered with the provider as an
# allowed callback. Default is http://localhost:8000/api/oauth/callback
# - change it for production.
#
# Uncomment a section to activate that provider.

# [notion]
# client_id_env = "DIGITORN_NOTION_CLIENT_ID"
# client_secret_env = "DIGITORN_NOTION_CLIENT_SECRET"
# redirect_uri = "http://localhost:8000/api/oauth/callback"

# [google]
# client_id_env = "DIGITORN_GOOGLE_CLIENT_ID"
# client_secret_env = "DIGITORN_GOOGLE_CLIENT_SECRET"
# redirect_uri = "http://localhost:8000/api/oauth/callback"

# [github]
# client_id_env = "DIGITORN_GITHUB_CLIENT_ID"
# client_secret_env = "DIGITORN_GITHUB_CLIENT_SECRET"
# redirect_uri = "http://localhost:8000/api/oauth/callback"

# [slack]
# client_id_env = "DIGITORN_SLACK_CLIENT_ID"
# client_secret_env = "DIGITORN_SLACK_CLIENT_SECRET"
# redirect_uri = "http://localhost:8000/api/oauth/callback"

# [discord]
# client_id_env = "DIGITORN_DISCORD_CLIENT_ID"
# client_secret_env = "DIGITORN_DISCORD_CLIENT_SECRET"
# redirect_uri = "http://localhost:8000/api/oauth/callback"
"""


# ────────────────────────────────────────────────────────────────────
# Registry
# ────────────────────────────────────────────────────────────────────


class OAuthProviderRegistry:
    """Loads and serves OAuth provider configs from a TOML file.

    Uses ``tomllib`` (stdlib, Python 3.11+). The registry is a simple
    read-only map - the admin edits the file and restarts the daemon
    if they want new providers available (hot-reload is out of scope
    for now because OAuth is infrequent config).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_PATH
        self._providers: dict[str, OAuthProviderConfig] = {}

    def load(self) -> None:
        """Read the TOML file + merge with the built-in catalog.

        If the file doesn't exist, writes the commented template so
        the admin has a starting point.
        """
        if not self._path.is_file():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(TEMPLATE_TOML, encoding="utf-8")
            logger.info(
                "oauth_providers.toml not found - wrote a template at %s",
                self._path,
            )
            return

        try:
            import tomllib  # Python 3.11+
        except ImportError:
            import tomli as tomllib  # type: ignore

        try:
            with self._path.open("rb") as f:
                data = tomllib.load(f)
        except Exception as exc:
            logger.error(
                "failed to parse %s: %s - OAuth will be disabled",
                self._path, exc,
            )
            return

        for name, section in data.items():
            if not isinstance(section, dict):
                continue
            provider = self._build_provider(name, section)
            if provider is not None:
                self._providers[name] = provider
                logger.info(
                    "OAuth provider %r loaded (configured=%s)",
                    name, provider.is_configured(),
                )

    def _build_provider(
        self, name: str, section: dict[str, Any],
    ) -> OAuthProviderConfig | None:
        """Merge user config with built-in catalog defaults for ``name``."""
        defaults: dict[str, Any] = dict(BUILTIN_PROVIDERS.get(name, {}))

        # Resolve client_id / client_secret from TOML or env var
        client_id = (
            section.get("client_id")
            or os.environ.get(section.get("client_id_env", "") or "")
            or ""
        )
        client_secret = (
            section.get("client_secret")
            or os.environ.get(section.get("client_secret_env", "") or "")
            or ""
        )

        auth_url = section.get("auth_url") or defaults.get("auth_url", "")
        token_url = section.get("token_url") or defaults.get("token_url", "")

        if not auth_url or not token_url:
            logger.warning(
                "OAuth provider %r missing auth_url or token_url, skipping",
                name,
            )
            return None

        return OAuthProviderConfig(
            name=name,
            auth_url=auth_url,
            token_url=token_url,
            client_id=client_id,
            client_secret=client_secret,
            default_scopes=(
                section.get("default_scopes")
                or defaults.get("default_scopes")
                or []
            ),
            redirect_uri=section.get("redirect_uri", DEFAULT_REDIRECT_URI),
            auth_style=section.get("auth_style") or defaults.get("auth_style", "post"),
            extra_auth_params=dict(
                section.get("extra_auth_params")
                or defaults.get("extra_auth_params")
                or {}
            ),
            revoke_url=section.get("revoke_url") or defaults.get("revoke_url", ""),
        )

    def get(self, name: str) -> OAuthProviderConfig | None:
        return self._providers.get(name)

    def list_configured(self) -> list[str]:
        """Names of providers that are fully configured (have id + secret)."""
        return [n for n, p in self._providers.items() if p.is_configured()]

    def list_all(self) -> list[str]:
        return sorted(self._providers.keys())


# Module-level singleton. Populated by the daemon's lifespan.
default_registry: OAuthProviderRegistry | None = None


def get_default_registry() -> OAuthProviderRegistry:
    """Return the global registry, creating + loading it lazily."""
    global default_registry
    if default_registry is None:
        default_registry = OAuthProviderRegistry()
        default_registry.load()
    return default_registry

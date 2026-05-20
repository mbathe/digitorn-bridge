"""OAuth 2.0 provider registry."""

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
    # `owner=user` flag)
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    # Revocation endpoint (optional - not every provider supports it)
    revoke_url: str = ""

    def is_configured(self) -> bool:
        """True iff client_id AND client_secret are both present."""
        return bool(self.client_id) and bool(self.client_secret)


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


"""


# Registry


class OAuthProviderRegistry:
    """Loads and serves OAuth provider configs from a TOML file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_PATH
        self._providers: dict[str, OAuthProviderConfig] = {}

    def load(self) -> None:
        """Read the TOML file + merge with the built-in catalog."""
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

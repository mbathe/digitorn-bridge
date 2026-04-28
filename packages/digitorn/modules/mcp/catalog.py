"""MCP Server Catalog - plug-and-play server configurations.

Two-layer resolution:

1. **Static catalog** - built-in entries for popular servers (GitHub,
   Notion, Slack, etc.).  Fastest, zero network, includes env_mapping
   for shorthand credentials.

2. **Registry lookup** - queries the official MCP registry
   (registry.modelcontextprotocol.io) at runtime.  Any server published
   to the registry works automatically - no code changes needed.

Users write shorthand YAML::

    servers:
      github:
        token: "{{secret.GITHUB_TOKEN}}"

The catalog resolves this to a full config with command, args, env vars,
transport, and OAuth settings - no need to read each server's docs.

Explicit configs (with ``command``, ``url``, or ``transport``) bypass
both catalog and registry entirely for backward compatibility.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_ARG_APPEND = "__ARG_APPEND__"

_STANDARD_KEYS = frozenset({
    "transport", "command", "args", "env", "url", "headers",
    "timeout", "buffer_size", "auth", "examples", "rate_limit_rpm",
    "via", "smithery_key", "smithery_namespace", "smithery_slug",
})


_SMITHERY_CONNECT_BASE = "https://api.smithery.ai/connect"

_SMITHERY_PROXY_BASE = "https://server.smithery.ai"

_SMITHERY_SLUGS: dict[str, str] = {
    "github": "@smithery-ai/github",
    "slack": "@smithery-ai/slack",
    "fetch": "@smithery-ai/fetch",
    "brave_search": "@anthropics/brave-web-search",
    "filesystem": "@anthropics/filesystem",
    "memory": "@anthropics/memory",
    "puppeteer": "@anthropics/puppeteer",
    "sequential_thinking": "@anthropics/sequential-thinking",
    "everart": "@anthropics/everart",
    "google_maps": "@anthropics/google-maps",
    "postgres": "@anthropics/postgres",
    "brave": "@anthropics/brave-web-search",
}


@dataclass(frozen=True)
class CatalogEntry:
    """Pre-configured defaults for a well-known MCP server."""

    server_id: str
    display_name: str
    description: str

    transport: str = "stdio"
    command: str = ""
    args: tuple[str, ...] = ()

    runtime: str = "npm"
    package: str = ""

    env_mapping: dict[str, str] = field(default_factory=dict)

    key_descriptions: dict[str, str] = field(default_factory=dict)

    default_env: dict[str, str] = field(default_factory=dict)

    oauth_provider: str | None = None
    oauth_env_token_var: str = ""
    oauth_scopes: tuple[str, ...] = ()

    oauth_style: str = ""

    oauth_keyfile_env: str = ""
    oauth_credentials_env: str = ""
    oauth_credentials_filename: str = ""

    binary_name: str = ""

    smithery_slug: str = ""

    timeout: float = 30.0

    # UI metadata - consumed by the Flutter Hub > MCP tab. Icon is
    # an emoji (works everywhere without asset shipping); category
    # is a string keyed to the Flutter client's category pills.
    # Both left empty here so the route-level substring fallback
    # map populates sensible values based on ``server_id``. Each
    # catalog entry can override these by setting them explicitly.
    icon: str = ""
    category: str = ""


CATALOG: dict[str, CatalogEntry] = {


    "github": CatalogEntry(
        server_id="github",
        display_name="GitHub",
        description="GitHub API (repos, issues, PRs, search, code)",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-github"),
        package="@modelcontextprotocol/server-github",
        env_mapping={"token": "GITHUB_PERSONAL_ACCESS_TOKEN"},
        key_descriptions={"token": "Personal access token (Settings → Developer settings → Tokens)"},
    ),
    "notion": CatalogEntry(
        server_id="notion",
        display_name="Notion",
        description="Notion workspace (pages, databases, search)",
        command="mcp-notion",
        runtime="pip",
        package="mcp-notion",
        env_mapping={"token": "NOTION_API_KEY"},
        key_descriptions={"token": "API key or OAuth token (Settings → Connections → Develop)"},
        oauth_provider="notion",
        oauth_style="env_token",
        oauth_env_token_var="NOTION_API_KEY",
    ),
    "slack": CatalogEntry(
        server_id="slack",
        display_name="Slack",
        description="Slack workspace (channels, messages, users)",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-slack"),
        package="@modelcontextprotocol/server-slack",
        env_mapping={
            "bot_token": "SLACK_BOT_TOKEN",
            "token": "SLACK_BOT_TOKEN",
            "team_id": "SLACK_TEAM_ID",
        },
        key_descriptions={
            "bot_token": "Bot User OAuth Token (api.slack.com → OAuth & Permissions)",
            "token": "Bot User OAuth Token (alias for bot_token)",
            "team_id": "Workspace ID (visible in the URL or workspace settings)",
        },
    ),
    "linear": CatalogEntry(
        server_id="linear",
        display_name="Linear",
        description="Linear issue tracker (issues, projects, teams)",
        command="npx",
        args=("-y", "mcp-linear"),
        package="mcp-linear",
        env_mapping={"api_key": "LINEAR_API_KEY"},
        key_descriptions={"api_key": "Personal API key (Settings → API)"},
    ),
    "clickup": CatalogEntry(
        server_id="clickup",
        display_name="ClickUp",
        description="ClickUp project management (tasks, lists, spaces)",
        command="npx",
        args=("-y", "mcp-clickup"),
        package="mcp-clickup",
        env_mapping={"api_key": "CLICKUP_API_KEY"},
        key_descriptions={"api_key": "Personal API token (Settings → Apps)"},
    ),
    "atlassian": CatalogEntry(
        server_id="atlassian",
        display_name="Atlassian (Jira/Confluence)",
        description="Jira issues and Confluence pages",
        command="npx",
        args=("-y", "mcp-atlassian"),
        package="mcp-atlassian",
        env_mapping={
            "site_url": "ATLASSIAN_URL",
            "email": "ATLASSIAN_EMAIL",
            "api_token": "ATLASSIAN_API_TOKEN",
        },
        key_descriptions={
            "site_url": "Your Atlassian site URL (e.g. https://myteam.atlassian.net)",
            "email": "Account email address",
            "api_token": "API token (id.atlassian.com → Security → API tokens)",
        },
    ),


    "google_drive": CatalogEntry(
        server_id="google_drive",
        display_name="Google Drive",
        description="Google Drive file access",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-gdrive"),
        package="@modelcontextprotocol/server-gdrive",
        oauth_provider="google",
        oauth_style="google_keyfile",
        oauth_keyfile_env="GDRIVE_OAUTH_PATH",
        oauth_credentials_env="GDRIVE_CREDENTIALS_PATH",
        oauth_credentials_filename=".gdrive-server-credentials.json",
        oauth_scopes=(
            "https://www.googleapis.com/auth/drive.readonly",
        ),
    ),
    "google_calendar": CatalogEntry(
        server_id="google_calendar",
        display_name="Google Calendar",
        description="Google Calendar events and scheduling",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-google-calendar"),
        package="@modelcontextprotocol/server-google-calendar",
        oauth_provider="google",
        oauth_style="google_keyfile",
        oauth_keyfile_env="GDRIVE_OAUTH_PATH",
        oauth_credentials_env="GDRIVE_CREDENTIALS_PATH",
        oauth_credentials_filename=".gdrive-server-credentials.json",
        oauth_scopes=(
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.events",
        ),
    ),
    "gmail": CatalogEntry(
        server_id="gmail",
        display_name="Gmail",
        description="Gmail email access (read, send, search) with auto OAuth",
        command="npx",
        args=("-y", "@gongrzhe/server-gmail-autoauth-mcp"),
        package="@gongrzhe/server-gmail-autoauth-mcp",
        binary_name="gmail-mcp",
        oauth_provider="google",
        oauth_style="google_keyfile",
        oauth_keyfile_env="GMAIL_OAUTH_PATH",
        oauth_credentials_env="GMAIL_CREDENTIALS_PATH",
        oauth_credentials_filename="credentials.json",
        oauth_scopes=(
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
        ),
    ),
    "google_maps": CatalogEntry(
        server_id="google_maps",
        display_name="Google Maps",
        description="Google Maps geocoding, directions, places",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-google-maps"),
        package="@modelcontextprotocol/server-google-maps",
        env_mapping={"api_key": "GOOGLE_MAPS_API_KEY"},
        key_descriptions={"api_key": "Google Maps API key (console.cloud.google.com → Credentials)"},
    ),


    "stripe": CatalogEntry(
        server_id="stripe",
        display_name="Stripe",
        description="Stripe payments (customers, invoices, subscriptions)",
        command="npx",
        args=("-y", "@stripe/mcp-server-stripe"),
        package="@stripe/mcp-server-stripe",
        env_mapping={"api_key": "STRIPE_API_KEY"},
        key_descriptions={"api_key": "Secret API key (dashboard.stripe.com → Developers → API keys)"},
    ),
    "shopify": CatalogEntry(
        server_id="shopify",
        display_name="Shopify",
        description="Shopify store management (products, orders)",
        command="npx",
        args=("-y", "@anthropic/mcp-server-shopify"),
        package="@anthropic/mcp-server-shopify",
        env_mapping={"token": "SHOPIFY_ACCESS_TOKEN"},
        key_descriptions={"token": "Admin API access token (Partners → Apps → API credentials)"},
    ),
    "paypal": CatalogEntry(
        server_id="paypal",
        display_name="PayPal",
        description="PayPal payments and transactions",
        command="npx",
        args=("-y", "@anthropic/mcp-server-paypal"),
        package="@anthropic/mcp-server-paypal",
        env_mapping={
            "client_id": "PAYPAL_CLIENT_ID",
            "client_secret": "PAYPAL_CLIENT_SECRET",
        },
        key_descriptions={
            "client_id": "REST API app Client ID (developer.paypal.com → My Apps)",
            "client_secret": "REST API app Secret (developer.paypal.com → My Apps)",
        },
    ),


    "mailgun": CatalogEntry(
        server_id="mailgun",
        display_name="Mailgun",
        description="Mailgun email sending and tracking",
        command="npx",
        args=("-y", "mcp-mailgun"),
        package="mcp-mailgun",
        env_mapping={
            "api_key": "MAILGUN_API_KEY",
            "domain": "MAILGUN_DOMAIN",
        },
        key_descriptions={
            "api_key": "Private API key (app.mailgun.com → API Keys)",
            "domain": "Sending domain (e.g. mg.example.com)",
        },
    ),


    "brave_search": CatalogEntry(
        server_id="brave_search",
        display_name="Brave Search",
        description="Web search via Brave Search API",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-brave-search"),
        package="@modelcontextprotocol/server-brave-search",
        env_mapping={"api_key": "BRAVE_API_KEY"},
        key_descriptions={"api_key": "Brave Search API key (brave.com/search/api → Get API Key)"},
    ),
    "fetch": CatalogEntry(
        server_id="fetch",
        display_name="Fetch",
        description="Fetch and extract content from web URLs",
        command="mcp-server-fetch",
        runtime="pip",
        package="mcp-server-fetch",
    ),
    "puppeteer": CatalogEntry(
        server_id="puppeteer",
        display_name="Puppeteer",
        description="Browser automation via Puppeteer",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-puppeteer"),
        package="@modelcontextprotocol/server-puppeteer",
    ),


    "postgres": CatalogEntry(
        server_id="postgres",
        display_name="PostgreSQL",
        description="PostgreSQL database access",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-postgres"),
        package="@modelcontextprotocol/server-postgres",
        env_mapping={"connection_string": _ARG_APPEND},
        key_descriptions={"connection_string": "PostgreSQL connection URI (e.g. postgresql://user:pass@host:5432/db)"},
    ),
    "sqlite": CatalogEntry(
        server_id="sqlite",
        display_name="SQLite",
        description="SQLite database access",
        command="mcp-server-sqlite",
        args=("--db-path",),
        runtime="pip",
        package="mcp-server-sqlite",
        env_mapping={"database": _ARG_APPEND},
        key_descriptions={"database": "Path to SQLite database file"},
    ),
    "qdrant": CatalogEntry(
        server_id="qdrant",
        display_name="Qdrant",
        description="Qdrant vector database",
        command="npx",
        args=("-y", "mcp-qdrant"),
        package="mcp-qdrant",
        env_mapping={
            "url": "QDRANT_URL",
            "api_key": "QDRANT_API_KEY",
        },
        key_descriptions={
            "url": "Qdrant server URL (e.g. http://localhost:6333)",
            "api_key": "Qdrant API key (optional for local instances)",
        },
    ),


    "filesystem": CatalogEntry(
        server_id="filesystem",
        display_name="Filesystem",
        description="Local filesystem read/write access",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-filesystem"),
        package="@modelcontextprotocol/server-filesystem",
        env_mapping={"path": _ARG_APPEND},
        key_descriptions={"path": "Directory path to expose (e.g. /home/user/project)"},
    ),
    "memory": CatalogEntry(
        server_id="memory",
        display_name="Memory (Knowledge Graph)",
        description="Persistent knowledge graph memory",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-memory"),
        package="@modelcontextprotocol/server-memory",
    ),
    "git": CatalogEntry(
        server_id="git",
        display_name="Git",
        description="Git repository operations (log, diff, branch, commit)",
        command="mcp-server-git",
        args=("--repository",),
        runtime="pip",
        package="mcp-server-git",
        env_mapping={"repository": _ARG_APPEND},
        key_descriptions={"repository": "Path to the git repository"},
    ),
    "docker": CatalogEntry(
        server_id="docker",
        display_name="Docker",
        description="Docker container management",
        command="npx",
        args=("-y", "mcp-docker"),
        package="mcp-docker",
    ),
    "sequential_thinking": CatalogEntry(
        server_id="sequential_thinking",
        display_name="Sequential Thinking",
        description="Step-by-step reasoning and problem decomposition",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-sequential-thinking"),
        package="@modelcontextprotocol/server-sequential-thinking",
    ),


    "vercel": CatalogEntry(
        server_id="vercel",
        display_name="Vercel",
        description="Vercel deployments and project management",
        command="npx",
        args=("-y", "mcp-vercel"),
        package="mcp-vercel",
        env_mapping={"token": "VERCEL_TOKEN"},
        key_descriptions={"token": "Vercel access token (vercel.com → Settings → Tokens)"},
    ),
    "cloudflare": CatalogEntry(
        server_id="cloudflare",
        display_name="Cloudflare",
        description="Cloudflare Workers, DNS, and CDN management",
        command="npx",
        args=("-y", "mcp-cloudflare"),
        package="mcp-cloudflare",
        env_mapping={"api_token": "CLOUDFLARE_API_TOKEN"},
        key_descriptions={"api_token": "API token (dash.cloudflare.com → My Profile → API Tokens)"},
    ),
    "aws": CatalogEntry(
        server_id="aws",
        display_name="AWS",
        description="AWS services (S3, Lambda, EC2, etc.)",
        command="npx",
        args=("-y", "mcp-aws"),
        package="mcp-aws",
        env_mapping={
            "access_key_id": "AWS_ACCESS_KEY_ID",
            "secret_access_key": "AWS_SECRET_ACCESS_KEY",
            "region": "AWS_DEFAULT_REGION",
        },
        key_descriptions={
            "access_key_id": "IAM access key ID",
            "secret_access_key": "IAM secret access key",
            "region": "AWS region (e.g. us-east-1, eu-west-1)",
        },
    ),
    "kubernetes": CatalogEntry(
        server_id="kubernetes",
        display_name="Kubernetes",
        description="Kubernetes cluster management",
        command="npx",
        args=("-y", "mcp-kubernetes"),
        package="mcp-kubernetes",
        env_mapping={"kubeconfig": "KUBECONFIG"},
        key_descriptions={"kubeconfig": "Path to kubeconfig file (default: ~/.kube/config)"},
    ),


    "everart": CatalogEntry(
        server_id="everart",
        display_name="EverArt",
        description="AI image generation via EverArt",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-everart"),
        package="@modelcontextprotocol/server-everart",
        env_mapping={"api_key": "EVERART_API_KEY"},
        key_descriptions={"api_key": "EverArt API key"},
    ),
    "apify": CatalogEntry(
        server_id="apify",
        display_name="Apify",
        description="Apify web scraping and automation actors",
        command="npx",
        args=("-y", "mcp-apify"),
        package="mcp-apify",
        env_mapping={"token": "APIFY_TOKEN"},
        key_descriptions={"token": "Apify API token (console.apify.com → Settings → Integrations)"},
    ),
}


def get_catalog_entry(server_id: str) -> CatalogEntry | None:
    """Look up a server in the catalog. Returns None if not found."""
    return CATALOG.get(server_id)


def list_catalog() -> list[str]:
    """List available catalog server IDs."""
    return sorted(CATALOG.keys())


@dataclass
class ServerRequirement:
    """A single configuration requirement for an MCP server."""
    key: str
    env_var: str
    description: str
    required: bool
    is_arg: bool = False


@dataclass
class ServerRequirements:
    """All requirements for configuring an MCP server."""
    server_id: str
    display_name: str
    description: str
    source: str
    transport: str
    runtime: str
    package: str
    credentials: list[ServerRequirement]
    oauth: bool = False
    oauth_provider: str = ""
    install_hint: str = ""
    yaml_example: str = ""


def get_server_requirements(server_id: str) -> ServerRequirements:
    """Get configuration requirements for a catalog server.

    Works BEFORE installation. For registry servers, use the async version.
    """
    entry = CATALOG.get(server_id)
    if entry is None:
        raise ValueError(
            f"Server '{server_id}' not found in catalog. "
            f"Use get_server_requirements_async() to check the registry, "
            f"or run: digitorn mcp search {server_id}"
        )
    return _requirements_from_catalog(entry)


async def get_server_requirements_async(server_id: str) -> ServerRequirements:
    """Get configuration requirements - catalog + registry fallback."""
    entry = CATALOG.get(server_id)
    if entry is not None:
        return _requirements_from_catalog(entry)

    registry_entry = await _search_registry(server_id)
    if registry_entry is not None:
        return _requirements_from_registry(server_id, registry_entry)

    raise ValueError(
        f"Server '{server_id}' not found in catalog or registry. "
        f"Run: digitorn mcp search {server_id}"
    )


def _requirements_from_catalog(entry: CatalogEntry) -> ServerRequirements:
    """Build requirements from a catalog entry."""
    creds = []
    for key, env_var in entry.env_mapping.items():
        is_arg = env_var == _ARG_APPEND
        desc = entry.key_descriptions.get(key, "")
        creds.append(ServerRequirement(
            key=key,
            env_var=env_var if not is_arg else "(positional argument)",
            description=desc,
            required=True,
            is_arg=is_arg,
        ))

    yaml_lines = [f"  {entry.server_id}:"]
    for c in creds:
        yaml_lines.append(f'    {c.key}: "{{{{secret.{c.key.upper()}}}}}"')
    if not creds:
        yaml_lines = [f"  {entry.server_id}: {{}}"]
    yaml_example = "\n".join(yaml_lines)

    if entry.runtime == "npm":
        install_hint = f"Requires Node.js (npx)"
    elif entry.runtime == "pip":
        install_hint = f"Requires Python (pip install {entry.package})"
    else:
        install_hint = ""

    return ServerRequirements(
        server_id=entry.server_id,
        display_name=entry.display_name,
        description=entry.description,
        source="catalog",
        transport=entry.transport,
        runtime=entry.runtime,
        package=entry.package,
        credentials=creds,
        oauth=entry.oauth_provider is not None,
        oauth_provider=entry.oauth_provider or "",
        install_hint=install_hint,
        yaml_example=yaml_example,
    )


def _requirements_from_registry(
    server_id: str,
    registry_entry: dict[str, Any],
) -> ServerRequirements:
    """Build requirements from a registry entry."""
    name = registry_entry.get("name", server_id)
    description = registry_entry.get("description", "")
    packages = registry_entry.get("packages", [])
    remotes = registry_entry.get("remotes", [])

    creds = []
    runtime = "npm"
    package = ""
    transport = "stdio"

    for pkg in packages:
        rt = pkg.get("registryType", "")
        if rt == "npm":
            runtime = "npm"
            package = pkg.get("identifier", "")
        elif rt in ("pip", "pypi"):
            runtime = "pip"
            package = pkg.get("identifier", "")

        for env_var in pkg.get("environmentVariables", []):
            var_name = env_var.get("name", "")
            if not var_name:
                continue
            shorthand = _env_var_to_shorthand(var_name)
            creds.append(ServerRequirement(
                key=shorthand,
                env_var=var_name,
                description=env_var.get("description", ""),
                required=env_var.get("required", True),
            ))

    if not packages and remotes:
        transport = "streamable_http"
        for remote in remotes:
            for header in remote.get("headers", []):
                hdr_name = header.get("name", "")
                hdr_value = header.get("value", "")
                if hdr_name and "{" in hdr_value:
                    import re
                    placeholders = re.findall(r"\{(\w+)\}", hdr_value)
                    for ph in placeholders:
                        creds.append(ServerRequirement(
                            key=ph,
                            env_var=f"(header: {hdr_name})",
                            description=f"Value for {hdr_name} header",
                            required=True,
                        ))
            break

    yaml_lines = [f"  {server_id}:"]
    for c in creds:
        yaml_lines.append(f'    {c.key}: "{{{{secret.{c.key.upper()}}}}}"')
    if not creds:
        yaml_lines = [f"  {server_id}: {{}}"]
    yaml_example = "\n".join(yaml_lines)

    return ServerRequirements(
        server_id=server_id,
        display_name=name,
        description=description,
        source="registry",
        transport=transport,
        runtime=runtime,
        package=package,
        credentials=creds,
        yaml_example=yaml_example,
    )


_REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0.1/servers"
_REGISTRY_TIMEOUT = 10

_registry_cache: dict[str, dict[str, Any] | None] = {}


async def _search_registry(query: str) -> dict[str, Any] | None:
    """Search the official MCP registry for a server.

    Returns the first matching server entry, or None.
    Results are cached per query for the process lifetime.
    """
    if query in _registry_cache:
        return _registry_cache[query]

    try:
        import httpx
    except ImportError:
        logger.debug("registry_lookup: httpx not available, skipping")
        _registry_cache[query] = None
        return None

    try:
        async with httpx.AsyncClient(timeout=_REGISTRY_TIMEOUT) as client:
            resp = await client.get(
                _REGISTRY_URL,
                params={"search": query, "limit": 5, "version": "latest"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.debug("registry_lookup: query=%s failed: %s", query, exc)
        _registry_cache[query] = None
        return None

    servers = data.get("servers", [])
    if not servers:
        _registry_cache[query] = None
        return None

    for entry in servers:
        server = entry.get("server", entry)
        name = server.get("name", "")
        short_name = name.rsplit("/", 1)[-1] if "/" in name else name
        if short_name == query or short_name == query.replace("_", "-"):
            _registry_cache[query] = server
            return server

    first = servers[0].get("server", servers[0])
    _registry_cache[query] = first
    return first


def _map_registry_env_vars(
    env_vars: list[dict[str, Any]],
    user_config: dict[str, Any],
    resolved: dict[str, Any],
) -> None:
    """Map registry env vars to user-provided shorthand values.

    Tries multiple candidate shorthands for each env var name,
    so users don't need to know the exact env var name.
    """
    for env_var in env_vars:
        var_name = env_var.get("name", "")
        if not var_name:
            continue
        if var_name in user_config.get("env", {}):
            resolved["env"][var_name] = str(user_config["env"][var_name])
            continue
        for candidate in _env_var_to_shorthands(var_name):
            if candidate in user_config and candidate not in _STANDARD_KEYS:
                resolved["env"][var_name] = str(user_config[candidate])
                break


def _registry_entry_to_config(
    server: dict[str, Any],
    user_config: dict[str, Any],
) -> dict[str, Any]:
    """Convert a registry server entry to a Digitorn MCP config dict.

    Handles both ``packages`` (local install) and ``remotes`` (hosted).
    Prefers ``packages`` (stdio) over ``remotes`` (HTTP).
    """
    resolved: dict[str, Any] = {"timeout": 30.0}

    packages = server.get("packages", [])
    remotes = server.get("remotes", [])

    npm_pkg = None
    pip_pkg = None
    for pkg in packages:
        reg_type = pkg.get("registryType", "")
        if reg_type == "npm" and npm_pkg is None:
            npm_pkg = pkg
        elif reg_type in ("pip", "pypi") and pip_pkg is None:
            pip_pkg = pkg

    if npm_pkg is not None:
        identifier = npm_pkg.get("identifier", "")
        resolved["transport"] = "stdio"
        resolved["command"] = "npx"
        resolved["args"] = ["-y", identifier]
        resolved["env"] = {}

        _map_registry_env_vars(
            npm_pkg.get("environmentVariables", []),
            user_config, resolved,
        )

    elif pip_pkg is not None:
        identifier = pip_pkg.get("identifier", "")
        resolved["transport"] = "stdio"
        resolved["command"] = "uvx"
        resolved["args"] = [identifier]
        resolved["env"] = {}

        _map_registry_env_vars(
            pip_pkg.get("environmentVariables", []),
            user_config, resolved,
        )

    elif remotes:
        remote = remotes[0]
        for r in remotes:
            if r.get("type") == "streamable-http":
                remote = r
                break

        rtype = remote.get("type", "streamable-http")
        transport_map = {
            "streamable-http": "streamable_http",
            "sse": "sse",
        }
        resolved["transport"] = transport_map.get(rtype, "streamable_http")
        resolved["url"] = remote.get("url", "")
        resolved["headers"] = {}

        for header in remote.get("headers", []):
            hdr_name = header.get("name", "")
            hdr_value = header.get("value", "")
            if hdr_name and hdr_value:
                for key, val in user_config.items():
                    if key in _STANDARD_KEYS:
                        continue
                    hdr_value = hdr_value.replace(
                        "{" + key + "}", str(val),
                    )
                resolved["headers"][hdr_name] = hdr_value
    else:
        raise ValueError(
            f"Registry server '{server.get('name', '?')}' has no "
            f"installable packages or remote endpoints."
        )

    for key in _STANDARD_KEYS:
        if key in user_config and key not in ("auth",):
            if key == "env" and "env" in resolved:
                resolved["env"].update(user_config["env"])
            else:
                resolved[key] = user_config[key]

    if "auth" in user_config:
        resolved["auth"] = user_config["auth"]

    if "auth" not in resolved:
        oauth_info = _detect_oauth_from_env_vars(server)
        if oauth_info:
            resolved["auth"] = oauth_info

    return resolved


_OAUTH_ENV_PATTERNS: dict[str, str] = {
    "google": "google",
    "github": "github",
    "notion": "notion",
    "slack": "slack",
    "microsoft": "microsoft",
    "azure": "microsoft",
    "spotify": "custom",
    "dropbox": "custom",
    "discord": "custom",
    "twitter": "custom",
    "facebook": "custom",
    "linkedin": "custom",
}


def _detect_oauth_from_env_vars(server: dict[str, Any]) -> dict[str, Any] | None:
    """Detect OAuth provider and env var names from registry environment variables.

    Scans env var names for patterns like ``*_CLIENT_ID``, ``*_CLIENT_SECRET``,
    ``*_OAUTH_*`` to auto-detect OAuth requirements.

    Returns an ``auth`` dict to store in config, or None if no OAuth detected.
    """
    all_env_vars: list[dict[str, Any]] = []
    for pkg in server.get("packages", []):
        all_env_vars.extend(pkg.get("environmentVariables", []))
    for remote in server.get("remotes", []):
        all_env_vars.extend(remote.get("environmentVariables", []))

    env_names = [ev.get("name", "").upper() for ev in all_env_vars]
    if not env_names:
        return None

    client_id_var = None
    client_secret_var = None
    token_var = None
    provider = ""

    for name in env_names:
        if "CLIENT_ID" in name:
            client_id_var = name
        elif "CLIENT_SECRET" in name:
            client_secret_var = name
        elif "ACCESS_TOKEN" in name or "API_TOKEN" in name:
            token_var = name
        elif name.endswith("_TOKEN") and "BOT" not in name:
            token_var = token_var or name
        elif name.endswith("_API_KEY"):
            token_var = token_var or name

    if not client_id_var or not client_secret_var:
        return None

    prefix = client_id_var.split("CLIENT_ID")[0].rstrip("_").lower()
    for pattern, prov in _OAUTH_ENV_PATTERNS.items():
        if pattern in prefix:
            provider = prov
            break

    if not provider:
        provider = "custom"

    env_token_var = token_var or ""

    auth_info: dict[str, Any] = {
        "provider": provider,
        "_detected": True,
        "_client_id_env": client_id_var,
        "_client_secret_env": client_secret_var,
    }
    if env_token_var:
        auth_info["env_token_var"] = env_token_var

    return auth_info


def _env_var_to_shorthand(env_name: str) -> str:
    """Convert an env var name to a user-friendly shorthand key.

    Examples:
        GITHUB_PERSONAL_ACCESS_TOKEN → token
        STRIPE_API_KEY → api_key
        NOTION_API_KEY → api_key
        SLACK_BOT_TOKEN → bot_token
        BRAVE_API_KEY → api_key
    """
    name = env_name.lower()
    for prefix in (
        "github_personal_", "github_", "notion_", "slack_",
        "stripe_", "brave_", "linear_", "clickup_", "vercel_",
        "cloudflare_", "mailgun_", "apify_", "everart_",
        "shopify_", "paypal_", "google_maps_", "google_",
        "qdrant_", "atlassian_", "aws_",
    ):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    if name == "access_token":
        return "token"
    return name


def _env_var_to_shorthands(env_name: str) -> list[str]:
    """Generate multiple candidate shorthand keys for an env var.

    For registry servers with unknown prefixes, we need a generic
    approach. Returns candidates in priority order.

    Examples:
        WEATHER_API_KEY → ["api_key", "weather_api_key"]
        MY_SERVICE_TOKEN → ["token", "service_token", "my_service_token"]
        API_KEY → ["api_key"]
    """
    known = _env_var_to_shorthand(env_name)
    name = env_name.lower()

    candidates = [known]

    if "_" in name:
        rest = name.split("_", 1)[1]
        if rest and rest not in candidates:
            candidates.append(rest)

    if name not in candidates:
        candidates.append(name)

    return candidates


def resolve_server_config(
    server_id: str,
    user_config: dict[str, Any],
) -> dict[str, Any]:
    """Resolve shorthand YAML config into a full server config.

    Resolution order:
    0. Smithery proxy (``via: smithery``) → streamable_http via Smithery
    1. Explicit config (has ``command``/``url``/``transport``) → as-is
    2. Static catalog lookup (built-in entries) → merge with user config
    3. Registry lookup marker → handled async in ``resolve_server_config_async``

    For sync callers, raises ValueError if server is unknown.
    For async callers, use ``resolve_server_config_async`` instead.
    """
    if user_config.get("via") == "smithery":
        return _resolve_via_smithery(server_id, user_config)

    if any(k in user_config for k in ("command", "url", "transport")):
        return dict(user_config)

    entry = CATALOG.get(server_id)
    if entry is not None:
        return _resolve_from_catalog(entry, user_config)

    raise ValueError(
        f"Unknown MCP server '{server_id}'. "
        f"Use resolve_server_config_async() to check the online registry, "
        f"or provide an explicit config (command/url/transport)."
    )


async def resolve_server_config_async(
    server_id: str,
    user_config: dict[str, Any],
) -> dict[str, Any]:
    """Async version - falls back to registry.modelcontextprotocol.io.

    Resolution order:
    0. Smithery proxy (``via: smithery``) → streamable_http via Smithery
    1. Explicit config → as-is
    2. Static catalog → merge
    3. Online registry → auto-build config from registry entry
    """
    if user_config.get("via") == "smithery":
        return _resolve_via_smithery(server_id, user_config)

    if any(k in user_config for k in ("command", "url", "transport")):
        return dict(user_config)

    entry = CATALOG.get(server_id)
    if entry is not None:
        return _resolve_from_catalog(entry, user_config)

    registry_entry = await _search_registry(server_id)
    if registry_entry is not None:
        logger.info(
            "mcp_registry_resolved server=%s registry_name=%s",
            server_id, registry_entry.get("name", "?"),
        )
        return _registry_entry_to_config(registry_entry, user_config)

    available = ", ".join(list_catalog())
    raise ValueError(
        f"Unknown MCP server '{server_id}'. Not found in built-in catalog "
        f"or registry.modelcontextprotocol.io. "
        f"Provide an explicit config (command/url/transport) "
        f"or use a known server: {available}"
    )


def _resolve_via_smithery(
    server_id: str,
    user_config: dict[str, Any],
) -> dict[str, Any]:
    """Resolve a server config to use Smithery Connect.

    Two modes supported:

    **Connect API** (recommended - requires ``smithery_namespace``):
        URL: ``api.smithery.ai/connect/{namespace}/{server_id}/mcp``
        Auth: ``Authorization: Bearer {smithery_key}``
        The namespace must exist in the user's Smithery dashboard.

    **Proxy** (fallback - no namespace needed):
        URL: ``server.smithery.ai/{slug}/mcp``
        Auth: ``Authorization: Bearer {smithery_key}``
        May require a different token type (service token with ``mcp`` scope).

    Slug resolution order:
    1. Explicit ``smithery_slug`` in user_config
    2. ``smithery_slug`` field on the catalog entry (if server is in catalog)
    3. ``_SMITHERY_SLUGS`` mapping (built-in known slugs)
    4. Fallback: server_id as-is

    Server-specific credentials (e.g. ``token``, ``api_key``) are passed
    as a JSON ``config`` query parameter - Smithery injects them as env
    vars into the hosted server process.
    """
    import urllib.parse

    smithery_key = user_config.get("smithery_key", "")
    if not smithery_key:
        raise ValueError(
            f"Server '{server_id}' uses 'via: smithery' but no "
            f"'smithery_key' was provided. Add: smithery_key: \"{{{{secret.SMITHERY_KEY}}}}\""
        )

    namespace = user_config.get("smithery_namespace", "")

    slug = user_config.get("smithery_slug", "")
    if not slug:
        entry = CATALOG.get(server_id)
        if entry and entry.smithery_slug:
            slug = entry.smithery_slug
    if not slug:
        slug = _SMITHERY_SLUGS.get(server_id, "")
    if not slug:
        slug = server_id

    if namespace:
        url = f"{_SMITHERY_CONNECT_BASE}/{namespace}/{server_id}/mcp"
    else:
        url = f"{_SMITHERY_PROXY_BASE}/{slug}/mcp"

    server_config: dict[str, str] = {}
    entry = CATALOG.get(server_id)
    for key, value in user_config.items():
        if key in _STANDARD_KEYS:
            continue
        if entry and key in entry.env_mapping:
            env_var = entry.env_mapping[key]
            if env_var != _ARG_APPEND:
                server_config[env_var] = str(value)
            else:
                server_config[key] = str(value)
        else:
            server_config[key] = str(value)

    if server_config:
        import json as _json
        config_json = _json.dumps(server_config)
        url = f"{url}?config={urllib.parse.quote(config_json)}"

    resolved: dict[str, Any] = {
        "transport": "streamable_http",
        "url": url,
        "headers": {"Authorization": f"Bearer {smithery_key}"},
        "timeout": user_config.get("timeout", 30.0),
    }

    if "examples" in user_config:
        resolved["examples"] = user_config["examples"]

    logger.info(
        "mcp_smithery_resolved server=%s slug=%s namespace=%s has_config=%s",
        server_id, slug, namespace or "(proxy)", bool(server_config),
    )

    return resolved


def _resolve_from_catalog(
    entry: CatalogEntry,
    user_config: dict[str, Any],
) -> dict[str, Any]:
    """Build config from a static catalog entry + user overrides."""

    resolved: dict[str, Any] = {
        "transport": entry.transport,
        "command": entry.command,
        "args": list(entry.args),
        "env": dict(entry.default_env),
        "timeout": entry.timeout,
    }

    for key, value in user_config.items():
        if key in _STANDARD_KEYS:
            continue
        mapping = entry.env_mapping.get(key)
        if mapping is None:
            continue
        if mapping == _ARG_APPEND:
            resolved["args"].append(str(value))
        else:
            resolved["env"][mapping] = str(value)

    user_auth = user_config.get("auth")
    if isinstance(user_auth, dict) and entry.oauth_provider:
        if "type" not in user_auth:
            user_auth["type"] = "oauth2"
        if "provider" not in user_auth:
            user_auth["provider"] = entry.oauth_provider
        if "env_token_var" not in user_auth and entry.oauth_env_token_var:
            user_auth["env_token_var"] = entry.oauth_env_token_var
        if "scopes" not in user_auth and entry.oauth_scopes:
            user_auth["scopes"] = list(entry.oauth_scopes)
        resolved["auth"] = user_auth
    elif "auth" in user_config:
        resolved["auth"] = user_config["auth"]

    for key in _STANDARD_KEYS:
        if key == "auth":
            continue
        if key not in user_config:
            continue
        if key == "env":
            resolved["env"].update(user_config["env"])
        elif key == "args":
            resolved["args"] = list(user_config["args"])
        else:
            resolved[key] = user_config[key]

    if "examples" in user_config:
        resolved["examples"] = user_config["examples"]

    return resolved


def check_runtime(entry: CatalogEntry) -> str | None:
    """Check if the server's command is available.

    Returns an actionable error message, or None if OK.
    """
    if not entry.command:
        return None

    if entry.runtime == "npm":
        from digitorn.core.mcp_store import _ensure_node_in_path
        _ensure_node_in_path()

    if shutil.which(entry.command) is not None:
        return None

    if entry.runtime == "npm":
        return (
            f"MCP server '{entry.display_name}' requires Node.js. "
            f"Install from https://nodejs.org/ then run: "
            f"npm install -g {entry.package}"
        )
    if entry.runtime == "pip":
        return (
            f"MCP server '{entry.display_name}' requires Python package "
            f"'{entry.package}'. Install with: pip install {entry.package}"
        )
    return (
        f"MCP server '{entry.display_name}': "
        f"command '{entry.command}' not found."
    )

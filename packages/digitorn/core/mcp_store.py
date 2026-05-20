"""MCP Store - CRUD for daemon-managed MCP servers.

Provides async operations to search, install, test, configure, and
remove MCP servers from the daemon's persistent registry.

All MCP servers must be installed via this store before apps can use them.
The daemon validates configuration and health before making servers available.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn.core.models import ManagedMCPServer
from digitorn.modules.mcp.catalog import (
    _ARG_APPEND,
    CATALOG,
    CatalogEntry,
    get_catalog_entry,
    list_catalog,
    resolve_server_config,
    _search_registry,
    _registry_entry_to_config,
)

logger = logging.getLogger(__name__)

# Valid status transitions:
#   installed  →  tested  →  ready
#   any        →  error
#   error      →  tested  (after re-test succeeds)
VALID_STATUSES = ("installed", "tested", "ready", "error")


# ---------------------------------------------------------------------------
# Config validation - required credentials per server
# ---------------------------------------------------------------------------


def get_required_config_keys(
    server_id: str,
    server: "ManagedMCPServer | None" = None,
) -> dict[str, str]:
    """Return required config keys for a server.

    Works for both catalog and registry servers:
    - Catalog: uses `env_mapping` + `oauth_provider`
    - Registry: detects OAuth from auto-detected `config.auth._detected`

    Returns a dict of {shorthand_key: env_var_name}.
    For OAuth servers, includes `auth.client_id` and `auth.client_secret`.
    Empty dict if the server needs no credentials or is unknown.
    """
    entry = get_catalog_entry(server_id)
    result: dict[str, str] = {}

    if entry is not None:
        result = {k: v for k, v in entry.env_mapping.items() if v != _ARG_APPEND}
        # OAuth servers require client_id + client_secret
        if entry.oauth_provider:
            result["auth.client_id"] = "_OAUTH_CLIENT_ID"
            result["auth.client_secret"] = "_OAUTH_CLIENT_SECRET"
    elif server is not None:
        config = server.config or {}
        # Registry server: use stored required env vars from registry metadata
        for ev in config.get("_registry_required_env", []):
            env_name = ev.get("name", "")
            if not env_name:
                continue
            from digitorn.modules.mcp.catalog import _env_var_to_shorthand
            shorthand = _env_var_to_shorthand(env_name)
            result[shorthand] = env_name
        # Auto-detected OAuth
        auth = config.get("auth", {})
        if isinstance(auth, dict) and auth.get("provider"):
            result["auth.client_id"] = auth.get("_client_id_env", "_OAUTH_CLIENT_ID")
            result["auth.client_secret"] = auth.get("_client_secret_env", "_OAUTH_CLIENT_SECRET")

    return result


def get_missing_config(server: ManagedMCPServer) -> list[str]:
    """Return config keys that are required but not yet set.

    Checks the server's `config` dict against the catalog's `env_mapping`.
    Also checks `env` (already-resolved env vars) for backward compat.
    For OAuth servers, checks if auth credentials are configured.
    Works for both catalog and registry servers.
    """
    required = get_required_config_keys(server.server_id, server=server)
    if not required:
        return []

    config = server.config or {}
    env = server.env or {}
    missing = []

    for shorthand, env_var in required.items():
        # OAuth keys - check nested auth config
        if shorthand.startswith("auth."):
            auth_key = shorthand.split(".", 1)[1]  # "client_id" or "client_secret"
            auth_block = config.get("auth", {})
            if isinstance(auth_block, dict) and auth_block.get(auth_key):
                continue
            missing.append(shorthand)
            continue

        if shorthand in config and config[shorthand]:
            continue
        if env_var in env and env[env_var]:
            continue
        missing.append(shorthand)

    return missing


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def search_servers(query: str) -> list[dict[str, Any]]:
    """Search for MCP servers in catalog + remote registry.

    Returns a merged list of results with source info.
    """
    results: list[dict[str, Any]] = []
    q = query.lower()
    browse_all = q in ("*", "all", "")

    # 1. Search static catalog
    for sid, entry in CATALOG.items():
        if browse_all or (
            q in sid
            or q in entry.display_name.lower()
            or q in entry.description.lower()
        ):
            results.append({
                "server_id": sid,
                "name": entry.display_name,
                "description": entry.description,
                "source": "catalog",
                "runtime": entry.runtime,
                "package": entry.package,
                "transport": entry.transport,
            })

    # 2. Search remote registry
    try:
        registry_entry = await _search_registry(query)
        if registry_entry is not None:
            name = registry_entry.get("name", query)
            short = name.rsplit("/", 1)[-1] if "/" in name else name
            # Skip if already found in catalog
            if not any(r["server_id"] == short for r in results):
                packages = registry_entry.get("packages", [])
                remotes = registry_entry.get("remotes", [])
                runtime = "npm"
                package = ""
                transport = "stdio"
                for pkg in packages:
                    rt = pkg.get("registryType", "")
                    if rt == "npm":
                        runtime = "npm"
                        package = pkg.get("identifier", "")
                        break
                    elif rt in ("pip", "pypi"):
                        runtime = "pip"
                        package = pkg.get("identifier", "")
                if not package and remotes:
                    transport = "streamable_http"

                results.append({
                    "server_id": short,
                    "name": name,
                    "description": registry_entry.get("description", ""),
                    "source": "registry",
                    "runtime": runtime,
                    "package": package,
                    "transport": transport,
                })
    except Exception as exc:
        logger.debug("mcp_search_registry_failed: %s", exc)

    return results


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


async def install_server(
    session: AsyncSession,
    server_id: str,
    config: dict[str, Any] | None = None,
    credential_store: "Any | None" = None,
) -> ManagedMCPServer:
    """Install an MCP server into the daemon registry.

    Resolution order:
    1. Static catalog (built-in)
    2. Remote registry (registry.modelcontextprotocol.io)
    3. Custom config (user provides transport/command/url)

    Raises ValueError if server cannot be found or installed.
    """
    config = config or {}

    existing = await get_server(session, server_id)
    if existing is not None:
        raise ValueError(f"Server '{server_id}' is already installed (status={existing.status})")

    # Resolve full config
    resolved = await _resolve_for_install(server_id, config)

    # For stdio servers, install the package in an isolated directory
    # Skip for custom servers (they run from the user's local path)
    if resolved["transport"] == "stdio" and resolved.get("source") != "custom":
        install_err = await _install_package(
            server_id,
            resolved.get("runtime", "npm"),
            resolved.get("package", ""),
            resolved.get("command", ""),
        )
        if install_err:
            raise ValueError(install_err)

    # Resolve user-provided config keys → env vars / args / headers at install time
    env = dict(resolved.get("env", {}))
    args = list(resolved.get("args", []))
    headers = dict(resolved.get("headers", {}))
    entry = get_catalog_entry(server_id)
    if entry and config:
        for key, value in config.items():
            if key in ("command", "url", "transport", "args", "env", "headers",
                        "timeout", "runtime", "package", "auth"):
                continue  # Standard keys, not shorthand
            mapping = entry.env_mapping.get(key)
            if mapping and mapping != _ARG_APPEND:
                env[mapping] = str(value)
            elif mapping == _ARG_APPEND:
                if str(value) not in args:
                    args.append(str(value))
    # For remote servers, map config keys to Authorization header
    if resolved["transport"] in ("sse", "streamable_http") and config:
        for key in ("api_key", "token", "access_token"):
            if key in config and "Authorization" not in headers:
                headers["Authorization"] = f"Bearer {config[key]}"

    # Inject Digitorn-provided creds + hosted URL declared by the
    # catalog entry to enable no-config installs.
    if entry is not None:
        await _inject_digitorn_provided(entry, env, headers, credential_store)
        if entry.hosted_url and resolved["transport"] in ("sse", "streamable_http"):
            if not resolved.get("url"):
                resolved["url"] = entry.hosted_url

    # Persist registry metadata in config for later use (credentials detection)
    saved_config = dict(config)
    if resolved.get("_registry_required_env"):
        saved_config["_registry_required_env"] = resolved["_registry_required_env"]
    if resolved.get("auth") and "auth" not in saved_config:
        saved_config["auth"] = resolved["auth"]

    # Create a temporary server object for probing (needs server_id, runtime, package)
    server = ManagedMCPServer(
        server_id=server_id,
        display_name=resolved.get("display_name", server_id),
        description=resolved.get("description"),
        source=resolved.get("source", "custom"),
        transport=resolved["transport"],
        command=resolved.get("command"),
        args=args,
        env=env,
        url=resolved.get("url"),
        headers=headers,
        runtime=resolved.get("runtime", "npm"),
        package=resolved.get("package"),
        config=saved_config,
        status="installed",
        timeout=resolved.get("timeout", 30.0),
    )

    # Post-install probe: scan source for env vars to correct
    # registry metadata and surface required credentials.
    if resolved.get("source") in ("registry", "custom") and resolved["transport"] == "stdio":
        _post_install_probe(server)

    session.add(server)
    await session.commit()
    await session.refresh(server)

    logger.info("mcp_server_installed id=%s source=%s", server_id, server.source)
    return server


async def _resolve_for_install(
    server_id: str,
    user_config: dict[str, Any],
) -> dict[str, Any]:
    """Resolve server config for installation, adding metadata."""

    # Custom config (has command/url/transport)
    if any(k in user_config for k in ("command", "url", "transport")):
        # Auto-detect runtime from command
        cmd = user_config.get("command", "")
        if "runtime" not in user_config:
            if cmd in ("python3", "python", "uvx", "uv") or cmd.endswith("/python3"):
                runtime = "pip"
            elif cmd in ("node", "npx", "tsx") or cmd.endswith("/node"):
                runtime = "npm"
            else:
                runtime = "custom"
        else:
            runtime = user_config["runtime"]
        return {
            "transport": user_config.get("transport", "stdio"),
            "command": cmd,
            "args": user_config.get("args", []),
            "env": user_config.get("env", {}),
            "url": user_config.get("url"),
            "headers": user_config.get("headers", {}),
            "display_name": user_config.get("name", server_id),
            "description": user_config.get("description"),
            "source": "custom",
            "runtime": runtime,
            "package": user_config.get("package"),
            "timeout": user_config.get("timeout", 30.0),
        }

    # Static catalog
    entry = get_catalog_entry(server_id)
    if entry is not None:
        return {
            "transport": entry.transport,
            "command": entry.command,
            "args": list(entry.args),
            "env": dict(entry.default_env),
            "display_name": entry.display_name,
            "description": entry.description,
            "source": "catalog",
            "runtime": entry.runtime,
            "package": entry.package,
            "timeout": entry.timeout,
        }

    # Remote registry
    registry_entry = await _search_registry(server_id)
    if registry_entry is not None:
        resolved = _registry_entry_to_config(registry_entry, user_config)
        resolved["display_name"] = registry_entry.get("name", server_id)
        resolved["description"] = registry_entry.get("description", "")
        resolved["source"] = "registry"
        for pkg in registry_entry.get("packages", []):
            rt = pkg.get("registryType", "")
            if rt == "npm":
                resolved["runtime"] = "npm"
                resolved["package"] = pkg.get("identifier", "")
                # Store required env vars from registry for get_required_config_keys
                req_env = [
                    ev for ev in pkg.get("environmentVariables", [])
                    if ev.get("isRequired", False)
                ]
                if req_env:
                    resolved.setdefault("_registry_required_env", []).extend(req_env)
                break
            elif rt in ("pip", "pypi"):
                resolved["runtime"] = "pip"
                resolved["package"] = pkg.get("identifier", "")
                req_env = [
                    ev for ev in pkg.get("environmentVariables", [])
                    if ev.get("isRequired", False)
                ]
                if req_env:
                    resolved.setdefault("_registry_required_env", []).extend(req_env)
        return resolved

    available = ", ".join(list_catalog()[:20])
    raise ValueError(
        f"Unknown MCP server '{server_id}'. Not found in catalog or registry. "
        f"Available in catalog: {available}"
    )


def _post_install_probe(server: ManagedMCPServer) -> None:
    """Probe installed source to correct registry env var metadata.

    The MCP registry often declares env var names that don't match what
    the server actually reads (e.g. `YOUR_API_KEY` vs `TODOIST_API_TOKEN`).

    This function runs immediately after package install to:
    1. Discover the actual env vars the server reads from source
    2. Match them against registry-declared vars
    3. Replace `_registry_required_env` with corrected mappings

    This ensures `get_required_config_keys` returns the right shorthands
    and `update_server_config` maps to the right env vars - so the user
    never encounters a mismatch at test/connect time.
    """
    try:
        actual_vars = _probe_server_env_vars(server)
    except Exception:
        return
    if not actual_vars:
        return

    config = server.config or {}
    registry_env = config.get("_registry_required_env", [])
    if not registry_env:
        # No registry env vars declared - synthesize from probed source.
        # Pick credential-like vars (TOKEN, KEY, SECRET, API, PASSWORD).
        _CRED_KEYWORDS = ("TOKEN", "KEY", "SECRET", "API", "PASSWORD")
        cred_vars = [
            v for v in sorted(actual_vars)
            if any(k in v for k in _CRED_KEYWORDS)
        ]
        if cred_vars:
            config["_registry_required_env"] = [
                {"name": v, "isRequired": True, "_probed": True}
                for v in cred_vars
            ]
            server.config = config
            logger.info(
                "mcp_probe_discovered server=%s vars=%s",
                server.server_id, cred_vars,
            )
        return

    # Registry declared env vars - check if they match actual source
    corrected = []
    for ev in registry_env:
        declared_name = ev.get("name", "")
        if declared_name in actual_vars:
            # Registry name matches source - keep as is
            corrected.append(ev)
        else:
            # Mismatch: find the actual var that best matches
            _CRED_KEYWORDS = ("TOKEN", "KEY", "SECRET", "API", "PASSWORD")
            candidates = [
                v for v in actual_vars
                if any(k in v for k in _CRED_KEYWORDS)
                and v not in {e.get("name") for e in corrected}
            ]
            actual_name = _best_env_match(declared_name, candidates) if candidates else None
            if actual_name:
                corrected.append({
                    **ev,
                    "name": actual_name,
                    "_registry_name": declared_name,  # Keep original for reference
                    "_probed": True,
                })
                logger.info(
                    "mcp_probe_corrected server=%s registry=%s actual=%s",
                    server.server_id, declared_name, actual_name,
                )
            else:
                # No match found in source - keep registry name as fallback
                corrected.append(ev)

    config["_registry_required_env"] = corrected
    server.config = config


def _ensure_node_in_path() -> None:
    """Add Node.js to PATH if not already discoverable.

    Discovers Node.js from common installation layouts so that
    `npm` / `npx` commands work even when the parent shell didn't
    activate a version manager (IDE-spawned processes, systemd / NSSM
    services, fresh CI runners, …).

    Supported layouts:

    Posix:
      * nvm        - `~/.nvm/versions/node/v*/bin/`
      * fnm        - `~/.local/share/fnm/node-versions/v*/installation/bin/`
      * volta      - `~/.volta/bin/`
      * homebrew   - `/opt/homebrew/bin`, `/usr/local/bin`
      * official   - `/usr/local/bin/node`

    Windows:
      * nvm-windows - `%APPDATA%\\nvm\\v*\\` (node + npm side-by-side)
      * fnm        - `%LOCALAPPDATA%\\fnm\\node-versions\\v*\\installation\\`
      * volta      - `%LOCALAPPDATA%\\Volta\\bin`
      * official   - `%ProgramFiles%\\nodejs\\`,
                     `%ProgramFiles(x86)%\\nodejs\\`
    """
    if shutil.which("node"):
        return

    from pathlib import Path
    home = Path.home()
    node_exe = "node.exe" if _IS_WINDOWS else "node"

    def _prepend_path(p: "Path") -> None:
        os.environ["PATH"] = f"{p}{os.pathsep}{os.environ.get('PATH', '')}"

    candidates: list[Path] = []

    if _IS_WINDOWS:
        # nvm-windows: %APPDATA%\nvm\v{version}\
        appdata = os.environ.get("APPDATA")
        if appdata:
            nvmw = Path(appdata) / "nvm"
            if nvmw.is_dir():
                for v in sorted(nvmw.iterdir(), reverse=True):
                    if v.is_dir() and (v / node_exe).exists():
                        candidates.append(v)
                        break

        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            # fnm (Windows)
            fnmw = Path(local_appdata) / "fnm" / "node-versions"
            if fnmw.is_dir():
                for v in sorted(fnmw.iterdir(), reverse=True):
                    bin_dir = v / "installation"
                    if (bin_dir / node_exe).exists():
                        candidates.append(bin_dir)
                        break

            # Volta (Windows)
            volta_bin = Path(local_appdata) / "Volta" / "bin"
            if (volta_bin / node_exe).exists():
                candidates.append(volta_bin)

        # Official MSI installer
        for env_var in ("ProgramFiles", "ProgramFiles(x86)"):
            pf = os.environ.get(env_var)
            if pf:
                pf_node = Path(pf) / "nodejs"
                if (pf_node / node_exe).exists():
                    candidates.append(pf_node)

    else:
        # nvm: ~/.nvm/versions/node/v*/bin/
        nvm_dir = home / ".nvm" / "versions" / "node"
        if nvm_dir.is_dir():
            for v in sorted(nvm_dir.iterdir(), reverse=True):
                if (v / "bin" / node_exe).exists():
                    candidates.append(v / "bin")
                    break

        # fnm: ~/.local/share/fnm/node-versions/v*/installation/bin/
        fnm_dir = home / ".local" / "share" / "fnm" / "node-versions"
        if fnm_dir.is_dir():
            for v in sorted(fnm_dir.iterdir(), reverse=True):
                bin_dir = v / "installation" / "bin"
                if (bin_dir / node_exe).exists():
                    candidates.append(bin_dir)
                    break

        # Volta
        volta_bin = home / ".volta" / "bin"
        if (volta_bin / node_exe).exists():
            candidates.append(volta_bin)

        # Homebrew on Apple Silicon + Intel
        for brew in (Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
            if (brew / node_exe).exists():
                candidates.append(brew)
                break

    for c in candidates:
        _prepend_path(c)
        if shutil.which("node"):
            return


def _server_dir(server_id: str) -> "Path":
    """Return the isolated install directory for a server."""
    from pathlib import Path
    from digitorn.core.paths import mcp_servers_dir
    return mcp_servers_dir() / server_id


async def _inject_digitorn_provided(
    entry: "Any",
    env: dict[str, str],
    headers: dict[str, str],
    credential_store: "Any | None",
) -> None:
    """Inject Digitorn-managed shared credentials into the install env.

    `entry.digitorn_provided` is a `{env_var_name: credential_name}`
    map declared on the Hub catalog entry (see migration 0009 - column
    `digitorn_provided`). For each pair we resolve the credential
    against the daemon's system-wide credential store and write the
    decrypted value into `env` (or as a Bearer header when the env
    var name signals an Authorization).

    Behaviour on a missing credential is non-fatal: we log a warning
    and leave the env var alone. The catalog entry can still ship,
    the user is just expected to fall back to providing the value
    themselves via the install dialog. This lets us flip
    `digitorn_provided` on per-entry without coordinating a daemon
    redeploy with a credential-store push.

    No-op when `digitorn_provided` is empty (the default for every
    entry today - provisioning happens entry-by-entry).
    """
    provided = dict(getattr(entry, "digitorn_provided", None) or {})
    if not provided:
        return

    if credential_store is None:
        logger.warning(
            "digitorn_provided_skip server=%s reason=no_credential_store_in_install_context",
            getattr(entry, "server_id", "?"),
        )
        return

    try:
        from digitorn.core.credentials import Scope
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "digitorn_provided_skip server=%s reason=credentials_module_missing: %s",
            getattr(entry, "server_id", "?"), exc,
        )
        return
    store = credential_store

    for env_var, cred_name in provided.items():
        if env_var in env and env[env_var]:
            # User-supplied value already wins, skip overwrite.
            continue
        try:
            cred = await store.get_credential_by_name(
                name=cred_name, scope=Scope.SYSTEM_WIDE, decrypt=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "digitorn_provided_lookup_failed server=%s var=%s cred=%s: %s",
                getattr(entry, "server_id", "?"), env_var, cred_name, exc,
            )
            continue
        if cred is None:
            logger.warning(
                "digitorn_provided_credential_missing server=%s var=%s cred=%s "
                "- store has no system_wide credential by that name yet. "
                "Provision it via the admin UI; the user keeps having to "
                "fill the field manually until then.",
                getattr(entry, "server_id", "?"), env_var, cred_name,
            )
            continue
        fields = cred.get("fields") or {}
        value = (
            fields.get(env_var.lower())
            or fields.get("api_key")
            or fields.get("token")
            or fields.get("access_token")
            or fields.get("value")
            or (next(iter(fields.values())) if fields else None)
        )
        if not value:
            logger.warning(
                "digitorn_provided_empty_field server=%s var=%s cred=%s",
                getattr(entry, "server_id", "?"), env_var, cred_name,
            )
            continue
        env[env_var] = str(value)
        logger.info(
            "digitorn_provided_injected server=%s var=%s cred=%s",
            getattr(entry, "server_id", "?"), env_var, cred_name,
        )


async def _install_package(
    server_id: str, runtime: str, package: str, command: str,
) -> str | None:
    """Install npm/pip package in an isolated directory.

    npm servers:  `~/.local/share/digitorn/mcp-servers/<id>/node_modules/`
    pip servers:  `~/.local/share/digitorn/mcp-servers/<id>/.venv/`

    `uvx` / `npx` style commands are no-ops here: the package is
    pulled lazily on first invocation by the runtime itself, so we
    skip the eager install (and the `.venv` / `node_modules` it
    would otherwise create).

    Returns error message or None on success.
    """
    if not package and not command:
        return None

    cmd_base = Path(command or "").stem.lower() if command else ""
    if cmd_base in ("uvx", "pipx", "npx"):
        # Lazy runtime - package is fetched on first run. Verify the
        # runtime itself is on PATH so the user gets a clear error NOW
        # instead of a confusing "command not found" later at start time.
        if shutil.which(cmd_base) is None:
            return _missing_runtime_hint(cmd_base)
        logger.info(
            "mcp_install_skip_lazy_runtime server=%s package=%s cmd=%s",
            server_id, package, cmd_base,
        )
        return None

    server_dir = _server_dir(server_id)
    server_dir.mkdir(parents=True, exist_ok=True)

    if runtime == "npm":
        return await _install_npm(server_id, package, server_dir)
    elif runtime == "pip":
        return await _install_pip(server_id, package, server_dir)

    return None


async def _install_npm(
    server_id: str, package: str, server_dir: "Path",
) -> str | None:
    """Install an npm package in an isolated directory.

    Creates a package.json and runs `npm install <package>`.
    The binary is then at `server_dir/node_modules/.bin/<cmd>`.
    """
    _ensure_node_in_path()

    npm = shutil.which("npm")
    if npm is None:
        npx = shutil.which("npx")
        if npx is None:
            node = shutil.which("node")
            if node is None:
                return (
                    "Node.js is not installed. MCP servers using npm require Node.js. "
                    "Install from https://nodejs.org/ or run: digitorn doctor"
                )
            return (
                "npm not found (Node.js is installed but npm is missing). "
                "Try reinstalling Node.js."
            )
        npm = npx

    pkg_json = server_dir / "package.json"
    if not pkg_json.exists():
        import json as _json
        pkg_json.write_text(_json.dumps({
            "name": f"digitorn-mcp-{server_id}",
            "version": "1.0.0",
            "private": True,
        }, indent=2))
        
    cmd = [npm, "install", package]
    logger.info(
        "mcp_npm_install server=%s package=%s dir=%s",
        server_id, package, server_dir,
    )

    try:
        proc = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                cmd, capture_output=True, text=True, timeout=180,
                cwd=str(server_dir),
            ),
        )
        if proc.returncode != 0:
            return f"npm install failed for {package}: {proc.stderr[:400]}"

        # Verify the binary was installed
        bin_dir = server_dir / "node_modules" / ".bin"
        if bin_dir.exists() and any(bin_dir.iterdir()):
            logger.info(
                "mcp_npm_install_ok server=%s dir=%s binaries=%s",
                server_id, server_dir,
                [f.name for f in bin_dir.iterdir()][:5],
            )
        else:
            logger.info(
                "mcp_npm_install_ok server=%s dir=%s (no binaries, library package)",
                server_id, server_dir,
            )

    except Exception as exc:
        return f"npm install failed for {package}: {exc}"

    return None


_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS = sys.platform == "darwin"


def _missing_runtime_hint(cmd: str) -> str:
    """Return a clear, OS-specific error message when *cmd* isn't on PATH.

    Used by both install-time (refuse to save a row pointing at a tool
    the user doesn't have) and start-time (the SDK FileNotFoundError
    wrapper) so the message is consistent.
    """
    if cmd == "uvx":
        if _IS_WINDOWS:
            install = 'Install uv: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
        elif _IS_MACOS:
            install = 'Install uv: brew install uv  (or: curl -LsSf https://astral.sh/uv/install.sh | sh)'
        else:
            install = 'Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh'
        return (
            f"'uvx' is not on PATH. The server uses uvx as its runtime "
            f"(Python tool runner from astral-sh/uv). {install}. "
            f"After install, restart the daemon so it picks up the new PATH."
        )
    if cmd == "pipx":
        if _IS_WINDOWS:
            install = 'Install pipx: py -m pip install --user pipx && py -m pipx ensurepath'
        elif _IS_MACOS:
            install = 'Install pipx: brew install pipx && pipx ensurepath'
        else:
            install = 'Install pipx: python3 -m pip install --user pipx && python3 -m pipx ensurepath'
        return (
            f"'pipx' is not on PATH. The server uses pipx as its runtime. "
            f"{install}. After install, restart the daemon so it picks up "
            f"the new PATH."
        )
    if cmd == "npx":
        if _IS_WINDOWS:
            install = "Install Node.js from https://nodejs.org/ (the MSI ships npm + npx)"
        elif _IS_MACOS:
            install = "Install Node.js: brew install node  (or: https://nodejs.org/)"
        else:
            install = "Install Node.js from https://nodejs.org/ or via your package manager (apt install nodejs npm)"
        return (
            f"'npx' is not on PATH. The server uses npx as its runtime "
            f"(comes with Node.js). {install}. After install, restart "
            f"the daemon so it picks up the new PATH."
        )
    return f"'{cmd}' is not on PATH. Install it and restart the daemon."


def _exe_extensions() -> tuple[str, ...]:
    """Executable filename extensions to try when resolving a command.

    Windows uses `PATHEXT` (`.exe`/`.cmd`/`.bat`). npm shims
    install as `<name>.cmd` (cmd.exe wrapper) and `<name>` (bash
    shell script) - only the `.cmd` flavour is launchable via
    `subprocess` without `shell=True`, so we must explicitly probe
    it. Posix returns the bare extension only.
    """
    if _IS_WINDOWS:
        return (".cmd", ".exe", ".bat", ".ps1", "")
    return ("",)


def _resolve_exe_in(directory: "Path", name: str) -> "Path | None":
    """Find an executable named *name* inside *directory*.

    Probes platform-appropriate extensions in priority order. Returns
    the resolved path or `None` if no match exists. Empty/missing
    *directory* is a soft None (callers fall through to the next
    lookup strategy).
    """
    if not name:
        return None
    try:
        if not directory.exists():
            return None
    except OSError:
        return None
    for ext in _exe_extensions():
        cand = directory / f"{name}{ext}"
        if cand.exists():
            return cand
    return None


def _venv_bin_dir(venv_dir: "Path") -> "Path":
    """Cross-platform venv binaries directory.

    Posix venvs put scripts under `bin/`; Windows uses `Scripts/`.
    """
    if _IS_WINDOWS:
        return venv_dir / "Scripts"
    return venv_dir / "bin"


def _venv_python(venv_dir: "Path") -> "Path":
    """Resolve the python executable inside *venv_dir*."""
    bin_dir = _venv_bin_dir(venv_dir)
    if _IS_WINDOWS:
        return bin_dir / "python.exe"
    return bin_dir / "python"


async def _install_pip(
    server_id: str, package: str, server_dir: "Path",
) -> str | None:
    """Install a pip package in an isolated venv.

    Strategy (in order):
      1. `uvx` / `uv tool` runtime - no install needed; uv lazily
         downloads the package on first run into its own managed env.
      2. `uv` available - create the venv with `uv venv` and
         install with `uv pip install` (does **not** require pip
         to be present inside the venv).
      3. Plain `python -m venv` - creates a venv with pip bundled
         and runs that pip directly.
    """
    if not package:
        return None

    venv_dir = server_dir / ".venv"
    uv = shutil.which("uv")

    # Prefer `uv venv` when available; it skips bundling pip, so
    # follow up with `uv pip install` instead of the venv's pip.
    if not venv_dir.exists():
        if uv:
            cmd = [uv, "venv", str(venv_dir), "--python", sys.executable]
        else:
            cmd = [sys.executable, "-m", "venv", str(venv_dir)]

        logger.info("mcp_pip_venv_create server=%s dir=%s", server_id, venv_dir)
        try:
            proc = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=60),
            )
            if proc.returncode != 0:
                return f"Failed to create venv: {proc.stderr[:300]}"
        except Exception as exc:
            return f"Failed to create venv: {exc}"

    # Install package into venv. Three paths, picked by what's available.
    if uv:
        venv_python = _venv_python(venv_dir)
        cmd = [uv, "pip", "install", package, "--python", str(venv_python)]
    else:
        bin_dir = _venv_bin_dir(venv_dir)
        venv_pip = (
            bin_dir / "pip.exe" if sys.platform == "win32" else bin_dir / "pip"
        )
        if not venv_pip.exists():
            return (
                f"Cannot install into venv: pip not found in {venv_dir}. "
                f"Install `uv` (https://docs.astral.sh/uv/) or rebuild "
                f"the venv with the stdlib `python -m venv` so it ships "
                f"with pip."
            )
        cmd = [str(venv_pip), "install", package]

    logger.info(
        "mcp_pip_install server=%s package=%s venv=%s tool=%s",
        server_id, package, venv_dir, cmd[0],
    )

    try:
        proc = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=180),
        )
        if proc.returncode != 0:
            return f"pip install failed for {package}: {proc.stderr[:400]}"

        venv_bin = _venv_bin_dir(venv_dir)
        binaries: list[str] = []
        if venv_bin.exists():
            binaries = [
                f.name for f in venv_bin.iterdir()
                if f.is_file()
                and not f.name.lower().startswith(("python", "pip", "activate"))
            ]
        logger.info(
            "mcp_pip_install_ok server=%s binaries=%s",
            server_id, binaries[:5],
        )

    except Exception as exc:
        return f"pip install failed for {package}: {exc}"

    return None


# ---------------------------------------------------------------------------
# Test (connect, discover tools, healthcheck)
# ---------------------------------------------------------------------------


async def test_server(
    session: AsyncSession,
    server_id: str,
) -> dict[str, Any]:
    """Test an installed MCP server: connect, list tools, healthcheck.

    Updates the server status in DB based on test results.
    Returns a dict with test results.
    """
    from digitorn.modules.mcp.connections import MCPConnectionPool

    server = await get_server(session, server_id)
    if server is None:
        raise ValueError(f"Server '{server_id}' is not installed")

    pool = MCPConnectionPool()
    result: dict[str, Any] = {
        "server_id": server_id,
        "connected": False,
        "tools": [],
        "resources_count": 0,
        "prompts_count": 0,
        "health": False,
        "error": None,
    }

    try:
        # Refresh OAuth token if expired (before connecting)
        await _refresh_token_if_needed(session, server)

        # Build connect kwargs
        kwargs = _build_connect_kwargs(server)

        # Connect
        entry = await pool.connect(server_id, server.transport, **kwargs)
        result["connected"] = True
        result["tools"] = [
            {"name": t.name, "description": t.description}
            for t in entry.tools
        ]
        result["resources_count"] = len(entry.resources)
        result["prompts_count"] = len(entry.prompts)

        # Healthcheck
        health = await pool.ping(server_id)
        result["health"] = health

        server.status = "ready"
        server.status_message = None
        server.tools_count = len(entry.tools)
        server.tools_schema = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in entry.tools
        ]
        server.health_ok = health
        server.last_health_check = datetime.now(timezone.utc)

    except Exception as exc:
        result["error"] = str(exc)
        server.status = "error"
        server.status_message = str(exc)[:500]
        server.health_ok = False
        server.last_health_check = datetime.now(timezone.utc)
    finally:
        # Always disconnect the test pool
        await pool.disconnect_all()

    await session.commit()
    logger.info(
        "mcp_server_tested id=%s status=%s tools=%d",
        server_id, server.status, server.tools_count,
    )
    return result


async def _refresh_token_if_needed(
    session: AsyncSession,
    server: ManagedMCPServer,
) -> None:
    """Refresh the OAuth access_token if it's expired (or close to expiry).

    Uses the stored refresh_token to get a new access_token from the provider.
    Updates the server config in DB and rewrites credentials files as needed.
    """
    config = server.config or {}
    auth = config.get("auth")
    if not isinstance(auth, dict):
        return

    refresh_token = auth.get("refresh_token")
    if not refresh_token:
        return  # No refresh token - can't refresh

    import time
    expires_in = auth.get("expires_in")
    auth_timestamp = auth.get("_auth_timestamp", 0)
    if expires_in and auth_timestamp:
        elapsed = time.time() - auth_timestamp
        if elapsed < (expires_in - 300):
            return  # Token still valid

    # Determine OAuth provider: catalog > config.auth.provider
    entry = get_catalog_entry(server.server_id)
    oauth_provider = (entry.oauth_provider if entry else None) or auth.get("provider")
    if not oauth_provider:
        return  # Can't refresh without knowing the provider

    # Build OAuthProviderConfig for refresh
    from digitorn.modules.mcp.oauth import OAuthProviderConfig, OAuthManager

    oauth_config = OAuthProviderConfig(
        provider=oauth_provider,
        client_id=auth.get("client_id", ""),
        client_secret=auth.get("client_secret", ""),
        env_token_var=(entry.oauth_env_token_var if entry else "") or auth.get("env_token_var", ""),
        # Custom servers may have explicit URLs in auth block
        authorize_url=auth.get("authorize_url", ""),
        token_url=auth.get("token_url", ""),
        token_auth_method=auth.get("token_auth_method", "body"),
    )

    if not oauth_config.client_id or not oauth_config.client_secret:
        return

    try:
        oauth_manager = OAuthManager()
        token_data = await oauth_manager.refresh_access_token(oauth_config, refresh_token)
    except Exception as exc:
        logger.warning(
            "mcp_token_refresh_failed server=%s error=%s",
            server.server_id, exc,
        )
        return  # Don't crash - the old token may still work

    new_auth = {**auth}
    new_auth["access_token"] = token_data.get("access_token", auth.get("access_token", ""))
    if token_data.get("refresh_token"):
        new_auth["refresh_token"] = token_data["refresh_token"]
    if token_data.get("expires_in"):
        new_auth["expires_in"] = token_data["expires_in"]
    new_auth["_auth_timestamp"] = time.time()

    server.config = {**config, "auth": new_auth}
    await session.flush()

    # Rewrite Google credentials file if applicable (auto-detect via probe)
    if oauth_provider == "google":
        probed = _probe_server_oauth_env_vars(server)
        cred_filename = probed.get("credentials_filename") or ".credentials.json"
        server_dir = _server_dir(server.server_id)
        credentials_path = server_dir / cred_filename
        if credentials_path.exists():
            expires_in_s = int(token_data.get("expires_in", 3600))
            expiry_date_ms = int(time.time() * 1000) + (expires_in_s * 1000)
            creds = {
                "type": "authorized_user",
                "access_token": new_auth["access_token"],
                "refresh_token": new_auth.get("refresh_token", ""),
                "scope": token_data.get("scope", auth.get("scope", "")),
                "token_type": token_data.get("token_type", "Bearer"),
                "expiry_date": expiry_date_ms,
            }
            credentials_path.write_text(json.dumps(creds, indent=2))
            credentials_path.chmod(0o600)  # Owner-only read/write

    logger.info("mcp_token_refreshed server=%s", server.server_id)


def _build_connect_kwargs(server: ManagedMCPServer) -> dict[str, Any]:
    """Build kwargs for MCPConnectionPool.connect() from a managed server.

    For daemon-managed installs, uses the local isolated directory instead
    of `npx -y` (npm) or global pip commands (pip):

    - npm: `node server_dir/node_modules/.bin/<cmd>` or
            `node server_dir/node_modules/<package>/dist/index.js`
    - pip: `server_dir/.venv/bin/<cmd>`
    """
    kwargs: dict[str, Any] = {"timeout": server.timeout}

    if server.transport == "stdio":
        command, args = _resolve_local_command(server)
        kwargs["command"] = command
        kwargs["args"] = args
        # Use already-resolved env (update_server_config maps config → env)
        env = dict(server.env or {})
        # Remap env vars if registry names don't match actual source code
        _remap_env_from_source(server, env)
        # Prepare OAuth files if needed (writes keys file, sets env vars)
        _prepare_oauth_env(server, env)
        kwargs["env"] = env
    elif server.transport in ("sse", "streamable_http"):
        kwargs["url"] = server.url or ""
        headers = dict(server.headers or {})
        # Inject OAuth token as Authorization header if available
        _prepare_oauth_headers(server, headers)
        kwargs["headers"] = headers

    return kwargs


def _remap_env_from_source(server: ManagedMCPServer, env: dict[str, str]) -> None:
    """Remap env vars when registry-declared names don't match actual source.

    Common case: registry declares `YOUR_API_KEY` but the server reads
    `TODOIST_API_TOKEN`. This function probes the installed source to
    find the real env var names and remaps values accordingly.

    Only runs for registry servers (catalog servers have correct mappings).
    """
    if server.source != "registry":
        return
    if not env:
        return

    try:
        actual_vars = _probe_server_env_vars(server)
    except Exception:
        return
    if not actual_vars:
        return

    # Find env vars we set that the source doesn't read (mismatched names)
    set_vars = {k: v for k, v in env.items() if v}
    mismatched_values: list[str] = []
    for var_name, value in set_vars.items():
        if var_name not in actual_vars:
            mismatched_values.append(value)

    if not mismatched_values:
        return  # All set vars are recognized by the source

    # Find actual vars that the source reads but aren't set
    # Focus on token/key/secret vars (the ones that hold credentials)
    _CRED_PATTERNS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "API")
    unset_cred_vars = [
        v for v in actual_vars
        if v not in env and any(p in v for p in _CRED_PATTERNS)
    ]

    if not unset_cred_vars:
        return

    # Remap: assign mismatched values to unset credential vars
    # If there's exactly one mismatched value and one unset cred var, it's clear
    if len(mismatched_values) == 1 and len(unset_cred_vars) == 1:
        env[unset_cred_vars[0]] = mismatched_values[0]
        logger.info(
            "mcp_env_remapped server=%s registry_miss→actual=%s",
            server.server_id, unset_cred_vars[0],
        )
    elif len(mismatched_values) <= len(unset_cred_vars):
        # Multiple values: try to match by similarity (token→token, key→key)
        for value in mismatched_values:
            # Find which registry var had this value
            registry_var = next(
                (k for k, v in set_vars.items() if v == value and k not in actual_vars),
                "",
            )
            best_match = _best_env_match(registry_var, unset_cred_vars)
            if best_match:
                env[best_match] = value
                unset_cred_vars.remove(best_match)
                logger.info(
                    "mcp_env_remapped server=%s %s→%s",
                    server.server_id, registry_var, best_match,
                )


def _best_env_match(registry_var: str, candidates: list[str]) -> str | None:
    """Find the best matching actual env var for a registry-declared name.

    Uses keyword overlap: TOKEN↔TOKEN, KEY↔KEY, SECRET↔SECRET, etc.
    """
    if not candidates:
        return None
    reg_parts = set(registry_var.upper().split("_"))
    best = None
    best_score = 0
    for c in candidates:
        c_parts = set(c.upper().split("_"))
        score = len(reg_parts & c_parts)
        if score > best_score:
            best_score = score
            best = c
    return best or candidates[0]


def _prepare_oauth_env(server: ManagedMCPServer, env: dict[str, str]) -> None:
    """Prepare OAuth credentials for servers that need them.

    Works universally for catalog, registry, and custom servers.

    For Google OAuth servers (detected by `oauth_provider == "google"`):
    - Writes `gcp-oauth.keys.json` in Google's "installed app" format
    - **Auto-probes** the server source code to discover env var names
      (`*_OAUTH_PATH`, `*_CREDENTIALS_PATH`) - no hardcoding needed
    - Writes the credentials file with stored tokens

    For env_token servers (e.g. Notion):
    - Injects the access_token as an env var

    This function is called at connect time to inject credentials into the
    server process environment regardless of the OAuth provider.
    """
    config = server.config or {}
    auth = config.get("auth")
    if not isinstance(auth, dict):
        return

    client_id = auth.get("client_id", "")
    client_secret = auth.get("client_secret", "")

    entry = get_catalog_entry(server.server_id)
    oauth_provider = (entry.oauth_provider if entry else None) or auth.get("provider")

    # --- Google keyfile style (auto-detected or catalog) ---
    if oauth_provider == "google" and client_id and client_secret:
        _prepare_google_keyfile_auto(server, auth, env)

    # --- Env token injection ---
    env_token_var = (entry.oauth_env_token_var if entry else "") or auth.get("env_token_var", "")
    if env_token_var:
        _prepare_env_token_from_var(auth, config, env_token_var, env)


def _probe_server_source(server: ManagedMCPServer) -> str:
    """Read the installed server's source code for probing.

    Scans multiple candidate paths (dist/, build/, src/, etc.) and
    concatenates all found JS/Python source up to a size limit.

    Supports:
    - npm packages installed in server_dir/node_modules/
    - pip packages installed in server_dir/.venv/
    - Custom/local servers (resolves the entry point file and its directory)
    """
    from pathlib import Path

    server_dir = _server_dir(server.server_id)
    sources: list[str] = []
    max_total = 200_000  # 200KB total budget
    current_size = 0

    def _scan_dir(directory: Path, extensions: tuple[str, ...]) -> None:
        nonlocal current_size
        if not directory.is_dir():
            return
        for f in directory.rglob("*"):
            if current_size >= max_total:
                break
            if f.suffix in extensions and f.is_file():
                try:
                    chunk = f.read_text(errors="ignore")[:50_000]
                    sources.append(chunk)
                    current_size += len(chunk)
                except Exception:
                    pass  # Best-effort source scan - skip unreadable files

    if server.runtime == "npm" and server.package:
        pkg_dir = server_dir / "node_modules" / server.package
        for subdir in ("dist", "build", "src", "lib", "."):
            candidate_dir = pkg_dir / subdir
            _scan_dir(candidate_dir, (".js", ".mjs", ".cjs"))
    elif server.runtime == "pip" and (server_dir / ".venv").exists():
        _scan_dir(server_dir / ".venv" / "lib", (".py",))

    # Custom/local servers: resolve the entry point from command + args
    if not sources and server.source == "custom" and server.command:
        entry_point = _find_custom_entry_point(server)
        if entry_point and entry_point.is_file():
            try:
                chunk = entry_point.read_text(errors="ignore")[:50_000]
                sources.append(chunk)
                current_size += len(chunk)
            except Exception:
                pass  # Best-effort source scan - skip unreadable entry point
            # Also scan sibling files in the same directory
            _scan_dir(entry_point.parent, (".js", ".mjs", ".cjs", ".py", ".ts"))

    return "\n".join(sources)


def _find_custom_entry_point(server: ManagedMCPServer) -> "Path | None":
    """Resolve the entry point file for a custom/local server.

    Looks at command + args to find the actual source file:
    - `python3 -m my_server` → find my_server/__main__.py
    - `python3 server.py` → server.py
    - `node server.js` → server.js
    - `/path/to/my-server` → the script itself
    """
    from pathlib import Path

    command = server.command or ""
    args = list(server.args or [])

    # node/python + script file
    if command in ("node", "python3", "python") and args:
        # Skip flags like -m, -u, etc.
        for i, arg in enumerate(args):
            if arg == "-m" and i + 1 < len(args):
                # python -m module → find module dir
                mod = args[i + 1].replace(".", "/")
                for candidate in [
                    Path(mod) / "__main__.py",
                    Path(mod) / "server.py",
                    Path(mod + ".py"),
                ]:
                    if candidate.exists():
                        return candidate
                return None
            if not arg.startswith("-"):
                p = Path(arg)
                if p.exists():
                    return p
                return None

    # Direct command path (e.g. /usr/local/bin/my-server or ./server.py)
    cmd_path = Path(command)
    if cmd_path.is_file():
        return cmd_path

    # Resolve from PATH
    resolved = shutil.which(command)
    if resolved:
        p = Path(resolved)
        if p.is_file():
            return p

    return None


def _probe_server_env_vars(server: ManagedMCPServer) -> set[str]:
    """Discover ALL env vars the server reads from its source code.

    Scans for `process.env.XXX` (JS) and `os.environ.get("XXX")` (Python).
    Returns a set of env var names.
    """
    import re

    source = _probe_server_source(server)
    if not source:
        return set()

    env_vars = set(re.findall(r'process\.env\.([A-Z][A-Z0-9_]*)', source))
    env_vars.update(re.findall(r'os\.(?:environ\.get|getenv)\(\s*["\']([A-Z][A-Z0-9_]*)', source))
    # Filter out generic Node/Python vars
    env_vars -= {"NODE_ENV", "PATH", "HOME", "DEBUG", "LOG_LEVEL", "PORT", "HOST"}
    return env_vars


def _probe_server_oauth_env_vars(server: ManagedMCPServer) -> dict[str, str]:
    """Probe the installed server's source code to discover OAuth env vars.

    Returns a dict with keys like `oauth_path_env`, `credentials_path_env`,
    `credentials_filename`, `token_env`.
    """
    import re

    result: dict[str, str] = {}
    source = _probe_server_source(server)
    if not source:
        return result

    env_vars = set(re.findall(r'process\.env\.([A-Z][A-Z0-9_]*)', source))
    env_vars.update(re.findall(r'os\.(?:environ\.get|getenv)\(\s*["\']([A-Z][A-Z0-9_]*)', source))

    for var in env_vars:
        if "OAUTH_PATH" in var or "OAUTH_KEYS" in var:
            result["oauth_path_env"] = var
        elif "CREDENTIALS_PATH" in var or "CREDENTIAL_PATH" in var:
            result["credentials_path_env"] = var
        elif "TOKEN" in var and "CLIENT" not in var:
            result.setdefault("token_env", var)

    # Also find the default credentials filename
    cred_file_match = re.search(
        r'CREDENTIALS_PATH\s*(?:=|:)\s*.*?[\'"]([^"\']+\.json)[\'"]',
        source,
    )
    if cred_file_match:
        result["credentials_filename"] = cred_file_match.group(1).split("/")[-1]

    return result


def _prepare_google_keyfile_auto(
    server: ManagedMCPServer,
    auth: dict[str, Any],
    env: dict[str, str],
) -> None:
    """Write Google OAuth key file and set env vars.

    Auto-discovers env var names by probing the server source code.
    Falls back to catalog fields if available.
    """
    client_id = auth.get("client_id", "")
    client_secret = auth.get("client_secret", "")
    if not client_id or not client_secret:
        return

    server_dir = _server_dir(server.server_id)
    server_dir.mkdir(parents=True, exist_ok=True)

    # --- Discover env vars: probe source > catalog > config cache ---
    entry = get_catalog_entry(server.server_id)
    probed = auth.get("_probed_env_vars")
    if not probed:
        probed = _probe_server_oauth_env_vars(server)
        # Cache the probe result in config to avoid re-probing
        if probed:
            auth["_probed_env_vars"] = probed

    oauth_path_env = (
        probed.get("oauth_path_env")
        or (entry.oauth_keyfile_env if entry else "")
    )
    credentials_path_env = (
        probed.get("credentials_path_env")
        or (entry.oauth_credentials_env if entry else "")
    )
    credentials_filename = (
        probed.get("credentials_filename")
        or (entry.oauth_credentials_filename if entry else "")
        or ".credentials.json"
    )

    # Write gcp-oauth.keys.json in Google's "installed app" format
    oauth_keys_path = server_dir / "gcp-oauth.keys.json"
    keys_data = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": ["http://localhost"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    oauth_keys_path.write_text(json.dumps(keys_data, indent=2))
    oauth_keys_path.chmod(0o600)  # Owner-only read/write
    logger.debug("mcp_oauth_keys_written server=%s path=%s", server.server_id, oauth_keys_path)

    # Set env vars so the MCP server finds the files
    if oauth_path_env:
        env[oauth_path_env] = str(oauth_keys_path)
    if credentials_path_env:
        credentials_path = server_dir / credentials_filename
        env[credentials_path_env] = str(credentials_path)


def _prepare_env_token_from_var(
    auth: dict[str, Any],
    config: dict[str, Any],
    env_token_var: str,
    env: dict[str, str],
) -> None:
    """Inject OAuth token into an env var for servers that read it directly.

    Works for both catalog and custom servers - only needs the env var name.
    """
    token = auth.get("access_token") or auth.get("token")

    # Also check top-level config (some servers use env_mapping for this)
    if not token:
        token = config.get("token")

    if token:
        env[env_token_var] = str(token)


def _prepare_oauth_headers(
    server: ManagedMCPServer,
    headers: dict[str, str],
) -> None:
    """Inject OAuth token as Authorization header for HTTP transports."""
    config = server.config or {}
    auth = config.get("auth")
    if not isinstance(auth, dict):
        return

    token = auth.get("access_token")
    if not token:
        return

    # Don't overwrite an existing Authorization header (user override wins)
    if "Authorization" not in headers:
        token_type = auth.get("token_type", "Bearer")
        headers["Authorization"] = f"{token_type} {token}"


def _resolve_bare_command(command: str) -> str:
    """If *command* is a bare name (no separators), resolve it through PATH.

    On Windows `subprocess` does **not** consult PATHEXT for bare names,
    so `npx`/`uvx`/`python` need to be expanded to their full
    `.cmd` / `.exe` path before being handed to `stdio_client`.
    Posix systems resolve names through `execvp` and don't need this
    pass; we still run `shutil.which` to surface a clearer error early
    when the command isn't installed at all.
    """
    if not command:
        return command
    if os.sep in command or (os.altsep and os.altsep in command):
        return command
    resolved = shutil.which(command)
    return resolved or command


def _resolve_local_command(server: ManagedMCPServer) -> tuple[str, list[str]]:
    """Resolve the actual command + args using the local isolated install.

    Prefers local install, falls back to original command if not found.
    Every return path goes through :func:`_resolve_bare_command` so the
    final command string is launchable as-is on Windows + Posix.
    """
    cmd, args = _resolve_local_command_inner(server)
    return _resolve_bare_command(cmd), args


def _resolve_local_command_inner(server: ManagedMCPServer) -> tuple[str, list[str]]:
    """Internal: original local-install resolution (no PATH expansion)."""
    server_dir = _server_dir(server.server_id)
    original_command = server.command or ""
    original_args = list(server.args or [])

    if server.runtime == "npm" and server.package:
        bin_dir = server_dir / "node_modules" / ".bin"
        stripped_args = _strip_npx_args(original_args)

        # 1. Read the package's own package.json → "bin" field (authoritative)
        bin_name = _read_npm_bin_name(server_dir, server.package)
        if bin_name:
            bin_path = _resolve_exe_in(bin_dir, bin_name)
            if bin_path is not None:
                return str(bin_path), stripped_args

        # 2. Catalog binary_name override
        catalog_entry = get_catalog_entry(server.server_id)
        if catalog_entry and catalog_entry.binary_name:
            bin_path = _resolve_exe_in(bin_dir, catalog_entry.binary_name)
            if bin_path is not None:
                return str(bin_path), stripped_args

        # 3. Heuristic candidates (fallback)
        if bin_dir.exists():
            pkg_short = server.package.rsplit("/", 1)[-1]
            for candidate in [
                pkg_short, f"mcp-{pkg_short}",
                Path(original_command).name if original_command else "",
                server.server_id,
            ]:
                bin_path = _resolve_exe_in(bin_dir, candidate)
                if bin_path is not None:
                    return str(bin_path), stripped_args

            # Filter: prefer bins with "mcp" or "server" in the stem. On
            # Windows a single npm shim shows up as both `foo` (bash)
            # and `foo.cmd` (cmd.exe) - pick the launchable one.
            bins = sorted(bin_dir.iterdir())
            mcp_bins = [
                f for f in bins
                if f.is_file() and ("mcp" in f.name or "server" in f.name)
            ]
            if mcp_bins:
                if _IS_WINDOWS:
                    win_exts = (".cmd", ".exe", ".bat", ".ps1")
                    winnable = [
                        f for f in mcp_bins
                        if f.suffix.lower() in win_exts
                    ]
                    chosen = winnable[0] if winnable else mcp_bins[0]
                else:
                    chosen = mcp_bins[0]
                return str(chosen), stripped_args

        # 4. Fallback: node + dist/index.js
        pkg_main = server_dir / "node_modules" / server.package / "dist" / "index.js"
        if pkg_main.exists():
            node = shutil.which("node")
            if node:
                return node, [str(pkg_main)] + stripped_args

        # 5. Final fallback: original command (npx -y ...)
        logger.debug(
            "mcp_local_npm_not_found server=%s dir=%s, falling back to %s",
            server.server_id, server_dir, original_command,
        )
        return original_command, original_args

    elif server.runtime == "pip":
        # Binary inside the venv: `Scripts/` on Windows, `bin/` on Posix.
        venv_bin = _venv_bin_dir(server_dir / ".venv")
        if venv_bin.exists():
            # Try the command name first. `Path.name` is cross-platform -
            # `rsplit('/', 1)` would miss backslash-separated entries.
            cmd_name = Path(original_command).name if original_command else ""
            if cmd_name:
                # Strip platform extension if the saved command carries one,
                # so the probe doesn't double-suffix `foo.exe.exe`.
                stem = Path(cmd_name).stem
                local_cmd = _resolve_exe_in(venv_bin, stem)
                if local_cmd is not None:
                    return str(local_cmd), original_args

            # Try package name (some pip packages have different binary names)
            pkg_name = (server.package or "").replace("-", "_")
            for candidate in [server.server_id, pkg_name]:
                local_cmd = _resolve_exe_in(venv_bin, candidate)
                if local_cmd is not None:
                    return str(local_cmd), original_args

        logger.debug(
            "mcp_local_pip_not_found server=%s dir=%s, falling back to %s",
            server.server_id, server_dir, original_command,
        )
        return original_command, original_args

    return original_command, original_args


def _read_npm_bin_name(server_dir: "Path", package: str) -> str | None:
    """Read the binary name from the npm package's own package.json.

    The `"bin"` field in package.json is the authoritative source for
    what binary an npm package installs.  Returns the first binary name,
    or None if not found.

    Examples::

        {"bin": {"mcp-server-gdrive": "dist/index.js"}}  → "mcp-server-gdrive"
        {"bin": "dist/index.js"}  → package short name
    """
    import json as _json

    pkg_json = server_dir / "node_modules" / package / "package.json"
    if not pkg_json.exists():
        return None
    try:
        data = _json.loads(pkg_json.read_text())
    except Exception:
        return None

    bin_field = data.get("bin")
    if isinstance(bin_field, dict):
        # {"mcp-server-gdrive": "dist/index.js"} → first key
        names = list(bin_field.keys())
        return names[0] if names else None
    elif isinstance(bin_field, str):
        # Single binary: name is the package's short name
        return package.rsplit("/", 1)[-1]
    return None


def _strip_npx_args(args: list[str]) -> list[str]:
    """Strip npx-specific args (`-y` and the package name) from args list.

    When we switch from `npx -y @package/name` to running the binary
    directly, we need to remove the npx flags and package identifier.
    """
    result = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "-y":
            continue
        if arg.startswith("@") or arg.startswith("-"):
            # Skip package names like @modelcontextprotocol/server-github
            if arg.startswith("@"):
                continue
        result.append(arg)
    return result


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def get_server(session: AsyncSession, server_id: str) -> ManagedMCPServer | None:
    """Get a managed server by ID."""
    result = await session.execute(
        select(ManagedMCPServer).where(ManagedMCPServer.server_id == server_id)
    )
    return result.scalar_one_or_none()


async def list_servers(session: AsyncSession, status: str | None = None) -> list[ManagedMCPServer]:
    """List all managed servers, optionally filtered by status."""
    query = select(ManagedMCPServer).order_by(ManagedMCPServer.server_id)
    if status:
        query = query.where(ManagedMCPServer.status == status)
    result = await session.execute(query)
    return list(result.scalars().all())


async def update_server_config(
    session: AsyncSession,
    server_id: str,
    config: dict[str, Any],
) -> ManagedMCPServer:
    """Update a server's runtime config (credentials, options).

    Also resolves shorthand config keys to env vars (via catalog mapping)
    and updates the `env` column so that connect kwargs are ready.
    """
    server = await get_server(session, server_id)
    if server is None:
        raise ValueError(f"Server '{server_id}' is not installed")

    # Deep merge new config into existing (handles nested auth blocks)
    merged = dict(server.config or {})
    for key, value in config.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    server.config = merged

    # Auto-resolve shorthand keys → env vars / args
    entry = get_catalog_entry(server_id)
    env = dict(server.env or {})
    args = list(server.args or [])
    if entry:
        for key, value in config.items():
            mapping = entry.env_mapping.get(key)
            if mapping and mapping != _ARG_APPEND:
                env[mapping] = str(value)
            elif mapping == _ARG_APPEND:
                if str(value) not in args:
                    args.append(str(value))
    else:
        # Registry/custom server: resolve shorthands via _registry_required_env
        from digitorn.modules.mcp.catalog import _env_var_to_shorthand
        for ev in merged.get("_registry_required_env", []):
            env_name = ev.get("name", "")
            if not env_name:
                continue
            shorthand = _env_var_to_shorthand(env_name)
            if shorthand in config and config[shorthand]:
                env[env_name] = str(config[shorthand])
            # Also accept the raw env var name as key
            elif env_name in config and config[env_name]:
                env[env_name] = str(config[env_name])
    server.env = env
    server.args = args

    # For remote servers, map common auth keys → Authorization header
    if server.transport in ("sse", "streamable_http"):
        headers = dict(server.headers or {})
        for key in ("api_key", "token", "access_token"):
            if key in config and config[key]:
                headers["Authorization"] = f"Bearer {config[key]}"
        server.headers = headers

    # Config changed → require re-test before app use
    if server.status in ("tested", "ready"):
        server.status = "installed"
        server.status_message = "Config updated - re-test required"
        logger.info("mcp_config_changed_status_reset server=%s", server_id)

    server.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return server


async def remove_server(session: AsyncSession, server_id: str) -> None:
    """Remove a managed server from the daemon registry.

    Also cleans up the isolated install directory on disk.
    """
    server = await get_server(session, server_id)
    if server is None:
        raise ValueError(f"Server '{server_id}' is not installed")

    # Clean up install directory
    server_dir = _server_dir(server_id)
    if server_dir.exists():
        try:
            shutil.rmtree(server_dir)
            logger.info("mcp_server_dir_removed server=%s dir=%s", server_id, server_dir)
        except Exception as exc:
            logger.warning("mcp_server_dir_cleanup_failed server=%s: %s", server_id, exc)

    await session.delete(server)
    await session.commit()
    logger.info("mcp_server_removed id=%s", server_id)


async def get_available_servers(session: AsyncSession) -> list[str]:
    """List server_ids of all servers that are ready for app use."""
    result = await session.execute(
        select(ManagedMCPServer.server_id).where(
            ManagedMCPServer.status == "ready",
        )
    )
    return [row[0] for row in result.all()]


async def get_server_for_app(
    session: AsyncSession,
    server_id: str,
) -> ManagedMCPServer:
    """Get a server that is ready for app use.

    Raises ValueError with actionable message if not found or not ready.
    """
    server = await get_server(session, server_id)
    if server is None:
        raise ValueError(
            f"MCP server '{server_id}' is not installed in the daemon. "
            f"Install it first: digitorn mcp install {server_id}"
        )

    if server.status == "error":
        raise ValueError(
            f"MCP server '{server_id}' is in error state: {server.status_message or 'unknown error'}. "
            f"Fix the issue and re-test: digitorn mcp test {server_id}"
        )

    if server.status == "installed":
        missing = get_missing_config(server)
        if missing:
            hints = ", ".join(f"{k}=xxx" for k in missing)
            raise ValueError(
                f"MCP server '{server_id}' is installed but not configured. "
                f"Set required credentials: digitorn mcp config {server_id} --set {hints}\n"
                f"Then test: digitorn mcp test {server_id}"
            )
        raise ValueError(
            f"MCP server '{server_id}' is installed but not tested. "
            f"Test it first: digitorn mcp test {server_id}"
        )

    if server.status == "tested":
        # "tested" is a legacy status, treat it as ready
        return server

    if server.status != "ready":
        raise ValueError(
            f"MCP server '{server_id}' is in unexpected state '{server.status}'. "
            f"Re-test it: digitorn mcp test {server_id}"
        )

    return server

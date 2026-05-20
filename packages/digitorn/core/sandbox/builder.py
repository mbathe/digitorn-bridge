"""Build a SandboxProfile from a CompiledApp."""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .profile import SandboxProfile

if TYPE_CHECKING:
    from digitorn.core.app.compiler import CompiledApp

logger = logging.getLogger(__name__)


def build_sandbox_profile(
    compiled: CompiledApp,
    workspace_override: str | None = None,
) -> SandboxProfile:
    """Translate a CompiledApp into an OS-level SandboxProfile."""
    profile = SandboxProfile(app_id=compiled.meta.app_id)
    workspace = workspace_override or _resolve_workspace(compiled)

    if workspace:
        profile.writable_paths.add(workspace)

    profile.add_system_paths()

    security = compiled.security_profile
    if security is None:
        profile.fs_unrestricted = True
        profile.allow_exec = True
        profile.allow_fork = True
        profile.allow_network = True
        return profile

    for mid, mod_cfg in compiled.modules.items():
        if not security.can_access_module(mid):
            continue

        constraints = mod_cfg.constraints or {}
        config = mod_cfg.config or {}

        handler = _MODULE_HANDLERS.get(mid)
        if handler:
            handler(profile, constraints, config, workspace)
        else:
            _apply_declared_sandbox(profile, config, workspace)

    _apply_granted_permissions(profile, security)
    _apply_resource_limits(profile, compiled)
    _apply_allow_paths(profile, compiled)

    return profile


def _handle_filesystem(
    profile: SandboxProfile,
    constraints: dict[str, Any],
    config: dict[str, Any],
    workspace: str,
) -> None:
    if constraints.get("unrestricted") is True:
        profile.fs_unrestricted = True
        return

    paths = constraints.get("paths", [])
    for p in paths:
        profile.writable_paths.add(str(Path(p).resolve()))

    if not paths and workspace:
        profile.writable_paths.add(workspace)


def _handle_shell(
    profile: SandboxProfile,
    constraints: dict[str, Any],
    config: dict[str, Any],
    workspace: str,
) -> None:
    profile.allow_exec = True
    profile.allow_fork = True

    for p in constraints.get("allowed_paths", []):
        profile.readable_paths.add(str(Path(p).resolve()))

    if constraints.get("unrestricted") is True:
        profile.fs_unrestricted = True


def _handle_network_module(
    profile: SandboxProfile,
    constraints: dict[str, Any],
    config: dict[str, Any],
    workspace: str,
) -> None:
    profile.allow_network = True

    for key in ("allowed_domains", "allowed_hosts"):
        for host in constraints.get(key, []):
            profile.allowed_hosts.add(host)

    egress = config.get("egress", {})
    for host in egress.get("allowed_domains", []):
        profile.allowed_hosts.add(host)


def _handle_database(
    profile: SandboxProfile,
    constraints: dict[str, Any],
    config: dict[str, Any],
    workspace: str,
) -> None:
    profile.allow_network = True

    for host in constraints.get("allowed_hosts", []):
        profile.allowed_hosts.add(host)

    profile.allowed_hosts.add("localhost")
    profile.allowed_hosts.add("127.0.0.1")


def _handle_git(
    profile: SandboxProfile,
    constraints: dict[str, Any],
    config: dict[str, Any],
    workspace: str,
) -> None:
    profile.allow_exec = True
    if workspace:
        profile.writable_paths.add(workspace)


def _handle_mcp(
    profile: SandboxProfile,
    constraints: dict[str, Any],
    config: dict[str, Any],
    workspace: str,
) -> None:
    """Apply per-server sandbox permissions for MCP (deny-by-default)."""
    servers = config.get("servers", {})
    if not servers:
        return

    for server_id, server_cfg in servers.items():
        if not isinstance(server_cfg, dict):
            continue

        declared = server_cfg.get("sandbox", {})
        transport = _infer_mcp_transport(server_cfg)

        if not declared:
            logger.warning(
                "mcp_server_no_sandbox server=%s transport=%s - "
                "no sandbox permissions declared, server will be blocked "
                "by OS sandbox. Add a 'sandbox:' block to the server config.",
                server_id, transport,
            )
            continue

        _apply_declared_sandbox(profile, {"sandbox": declared}, workspace)

        perms = set(declared.get("permissions", []))
        if transport == "stdio" and not perms & {"process.exec", "process.*"}:
            logger.warning(
                "mcp_server_missing_exec server=%s - "
                "stdio transport requires 'process.exec' permission, "
                "server will fail to start.",
                server_id,
            )
        if transport in ("sse", "http") and not perms & {"net.http", "net.*"}:
            logger.warning(
                "mcp_server_missing_network server=%s - "
                "%s transport requires 'net.http' permission, "
                "server will fail to connect.",
                server_id, transport,
            )


def _infer_mcp_transport(server_cfg: dict[str, Any]) -> str:
    explicit = server_cfg.get("transport", "").lower()
    if explicit in ("stdio", "sse", "http", "streamable_http"):
        return "http" if explicit == "streamable_http" else explicit

    if server_cfg.get("command"):
        return "stdio"
    if server_cfg.get("url"):
        return "sse"

    return "stdio"


def _handle_llm_provider(
    profile: SandboxProfile,
    constraints: dict[str, Any],
    config: dict[str, Any],
    workspace: str,
) -> None:
    profile.allow_network = True


_MODULE_HANDLERS = {
    "filesystem": _handle_filesystem,
    "shell": _handle_shell,
    "web": _handle_network_module,
    "http": _handle_network_module,
    "database": _handle_database,
    "git": _handle_git,
    "mcp": _handle_mcp,
    "llm_provider": _handle_llm_provider,
}


def _apply_declared_sandbox(
    profile: SandboxProfile,
    config: dict[str, Any],
    workspace: str,
) -> None:
    declared = config.get("sandbox", {})
    if not declared:
        return

    perms = set(declared.get("permissions", []))
    paths = declared.get("paths", {})

    if perms & {"fs.read", "fs.list"}:
        for p in paths.get("read", []):
            resolved = p.replace("{{workspace}}", workspace) if workspace else p
            profile.readable_paths.add(str(Path(resolved).resolve()))

    if perms & {"fs.write", "fs.delete"}:
        for p in paths.get("write", []):
            resolved = p.replace("{{workspace}}", workspace) if workspace else p
            profile.writable_paths.add(str(Path(resolved).resolve()))

    if perms & {"process.exec", "process.spawn_daemon"}:
        profile.allow_exec = True
        profile.allow_fork = True

    if perms & {"net.http", "net.socket", "net.listen"}:
        profile.allow_network = True
        for host in declared.get("allowed_hosts", []):
            profile.allowed_hosts.add(host)


def _apply_granted_permissions(
    profile: SandboxProfile,
    security: Any,
) -> None:
    perms = security.granted_permissions

    def _has(pattern: str) -> bool:
        return any(fnmatch.fnmatch(pattern, p) for p in perms)

    if _has("process.exec") or _has("process.*"):
        profile.allow_exec = True
    if _has("process.spawn_daemon") or _has("process.*"):
        profile.allow_fork = True
    if _has("net.http") or _has("net.*"):
        profile.allow_network = True


def _apply_resource_limits(
    profile: SandboxProfile,
    compiled: CompiledApp,
) -> None:
    resources = getattr(compiled.execution, "resources", None)
    if not resources:
        return

    if isinstance(resources, dict):
        profile.memory_limit = _parse_bytes(resources.get("memory", 0))
        profile.cpu_percent = int(resources.get("cpu", 0)) * 100
        profile.max_processes = int(resources.get("processes", 10))


def _apply_allow_paths(
    profile: SandboxProfile,
    compiled: CompiledApp,
) -> None:
    sandbox_cfg = getattr(
        getattr(compiled.execution, "sandbox", None), "allow_paths", None,
    )
    if not sandbox_cfg:
        return

    for entry in sandbox_cfg:
        entry = str(entry).strip()
        if not entry:
            continue

        if entry.endswith(":rw"):
            raw = entry[:-3].strip()
            resolved = str(Path(raw).expanduser().resolve())
            profile.writable_paths.add(resolved)
        else:
            raw = entry.rstrip(":ro").strip()
            resolved = str(Path(raw).expanduser().resolve())
            profile.readable_paths.add(resolved)


def _resolve_workspace(compiled: CompiledApp) -> str:
    workspace = getattr(compiled.execution, "workspace", "")
    if workspace:
        return str(Path(workspace).resolve())
    if compiled.source_path:
        return str(compiled.source_path.parent.resolve())
    return ""


_SIZE_UNITS = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}


def _parse_bytes(value: Any) -> int:
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value:
        return 0
    upper = value.upper().strip()
    for suffix, mult in _SIZE_UNITS.items():
        if upper.endswith(suffix):
            return int(float(upper[: -len(suffix)]) * mult)
    return int(upper)

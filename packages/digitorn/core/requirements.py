"""Module requirements system."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from digitorn.core.paths import all_module_dirs

logger = logging.getLogger(__name__)


@dataclass
class Requirement:
    """A single external requirement declared by a module."""

    name: str
    binary: str
    role: str
    module_id: str
    kind: str = "binary"
    optional: bool = True
    install: dict[str, str] = field(default_factory=dict)
    installed: bool = False
    path: str = ""

    def check(self) -> bool:
        """Check if this requirement is satisfied."""
        if self.kind == "python":
            return self._check_python()
        found = shutil.which(self.binary)
        self.installed = found is not None
        self.path = found or ""
        return self.installed

    def _check_python(self) -> bool:
        import importlib
        import_map = {
            "beautifulsoup4": "bs4",
            "python-pptx": "pptx",
            "python-calamine": "calamine",
            "pymupdf": "fitz",
            "qdrant-client": "qdrant_client",
        }
        import_name = import_map.get(self.name, self.name.replace("-", "_"))
        try:
            mod = importlib.import_module(import_name)
            self.installed = True
            self.path = getattr(mod, "__file__", "") or "installed"
        except ImportError:
            self.installed = False
            self.path = ""
        return self.installed

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "binary": self.binary,
            "kind": self.kind,
            "role": self.role,
            "module_id": self.module_id,
            "optional": self.optional,
            "install": self.install,
            "installed": self.installed,
            "path": self.path,
        }


def _detect_platform() -> str:
    import platform
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    return "linux"


def _detect_available_managers() -> dict[str, str]:
    managers: dict[str, str] = {}

    cross = ("pip", "pip3", "pipx", "npm", "go", "cargo")

    plat = _detect_platform()
    if plat == "linux":
        os_specific = ("apt", "dnf", "pacman", "snap")
    elif plat == "macos":
        os_specific = ("brew",)
    else:
        os_specific = ("winget", "choco", "scoop")

    for name in (*cross, *os_specific):
        path = shutil.which(name)
        if path:
            managers[name] = path

    if "pip3" in managers and "pip" not in managers:
        managers["pip"] = managers["pip3"]
    return managers


def _parse_requires_from_toml(toml_path: Path) -> list[dict[str, Any]]:
    with toml_path.open("rb") as f:
        data = tomllib.load(f)
    return data.get("requires", [])


def scan_all_requirements() -> list[Requirement]:
    """Scan all modules and return their requirements with install status."""
    reqs: list[Requirement] = []
    seen: set[str] = set()

    for base_dir in all_module_dirs():
        subdirs = [base_dir] if (base_dir / "digitorn-module.toml").exists() else []
        if base_dir.is_dir():
            subdirs.extend(d for d in sorted(base_dir.iterdir()) if d.is_dir())

        for module_dir in subdirs:
            toml_path = module_dir / "digitorn-module.toml"
            if not toml_path.exists():
                continue

            with toml_path.open("rb") as f:
                data = tomllib.load(f)
            module_id = data.get("module", {}).get("module_id", module_dir.name)

            for entry in data.get("requires", []):
                binary = entry.get("binary", "")
                if not binary or binary in seen:
                    continue
                seen.add(binary)

                req = Requirement(
                    name=entry.get("name", binary),
                    binary=binary,
                    kind=entry.get("kind", "binary"),
                    role=entry.get("role", ""),
                    module_id=module_id,
                    optional=entry.get("optional", True),
                    install=entry.get("install", {}),
                )
                req.check()
                reqs.append(req)

            pip_reqs = data.get("module", {}).get("requirements", [])
            for pkg in pip_reqs:
                if pkg in seen:
                    continue
                seen.add(pkg)

                req = Requirement(
                    name=pkg,
                    binary="",
                    kind="python",
                    role=f"Python package for {module_id}",
                    module_id=module_id,
                    optional=False,
                    install={"pip": pkg},
                )
                req.check()
                reqs.append(req)

    return reqs


def get_install_command(req: Requirement, managers: dict[str, str] | None = None) -> tuple[str, list[str]] | None:
    """Pick the best install command. Prefers pip > npm > go > cargo > pipx > system."""
    if managers is None:
        managers = _detect_available_managers()

    for mgr in ("pip", "npm", "go", "cargo", "pipx"):
        if mgr in req.install and mgr in managers:
            pkg = req.install[mgr]
            if mgr == "pip":
                return mgr, [managers["pip"], "install", pkg]
            elif mgr == "pipx":
                return mgr, [managers["pipx"], "install", pkg]
            elif mgr == "npm":
                return mgr, [managers["npm"], "install", "-g", *pkg.split()]
            elif mgr == "go":
                return mgr, [managers["go"], "install", pkg]
            elif mgr == "cargo":
                return mgr, [managers["cargo"], "install", pkg]

    system_cmds: dict[str, list[str]] = {
        "snap": ["sudo", "snap", "install"],
        "apt": ["sudo", "apt", "install", "-y"],
        "dnf": ["sudo", "dnf", "install", "-y"],
        "pacman": ["sudo", "pacman", "-S", "--noconfirm"],
        "brew": ["brew", "install"],
        "winget": ["winget", "install", "--accept-package-agreements"],
        "choco": ["choco", "install", "-y"],
        "scoop": ["scoop", "install"],
    }

    for mgr in system_cmds:
        if mgr in req.install and mgr in managers:
            pkg = req.install[mgr]
            cmd = list(system_cmds[mgr])
            cmd.extend(pkg.split())
            if cmd[0] != "sudo" and mgr in managers:
                cmd[0] = managers[mgr]
            return mgr, cmd

    return None


async def install_requirement(req: Requirement) -> dict[str, Any]:
    """Install a single requirement. Returns result dict."""
    managers = _detect_available_managers()
    cmd_info = get_install_command(req, managers)

    if cmd_info is None:
        return {
            "success": False,
            "name": req.name,
            "error": "No supported package manager found for this requirement",
            "available_managers": list(managers.keys()),
            "install_options": req.install,
        }

    mgr_name, cmd_parts = cmd_info

    if cmd_parts[0] == "sudo":
        return {
            "success": False,
            "name": req.name,
            "needs_sudo": True,
            "command": " ".join(cmd_parts),
            "message": f"Run manually: {' '.join(cmd_parts)}",
        }

    logger.info("installing_requirement name=%s via=%s cmd=%s", req.name, mgr_name, " ".join(cmd_parts))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

        if proc.returncode == 0:
            req.check()
            return {
                "success": True,
                "name": req.name,
                "manager": mgr_name,
                "installed": req.installed,
                "path": req.path,
            }
        else:
            return {
                "success": False,
                "name": req.name,
                "manager": mgr_name,
                "exit_code": proc.returncode,
                "stderr": stderr.decode("utf-8", errors="replace")[:500],
            }

    except asyncio.TimeoutError:
        return {"success": False, "name": req.name, "error": "Install timed out (120s)"}
    except Exception as exc:
        return {"success": False, "name": req.name, "error": str(exc)}


async def install_all_missing() -> list[dict[str, Any]]:
    """Install all missing requirements. Returns list of results."""
    reqs = scan_all_requirements()
    missing = [r for r in reqs if not r.installed]
    results = []
    for req in missing:
        result = await install_requirement(req)
        results.append(result)
    return results

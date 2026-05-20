"""Node.js runtime service - detect, auto-install, and spawn."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import platform
import shutil
import sys
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


# Pin a stable Node LTS. Bump this when a new LTS is available.
# v22.11.0 is the active LTS as of 2025 (support until 2027-04).
NODE_VERSION = "22.11.0"
MIN_MAJOR = 20  # system node must be at least v20 to be accepted

# Where to cache the auto-installed runtime.
_RUNTIMES_DIR_NAME = "runtimes"


@dataclass
class NodeRuntimeInfo:
    """Resolved Node runtime paths and metadata."""

    node_path: str
    npm_path: str | None
    npx_path: str | None
    version: str
    source: str  # "system" | "version_manager" | "auto_install"
    install_dir: Path | None = None
    bin_dir: Path | None = None
    extra_path: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "node": self.node_path,
            "npm": self.npm_path,
            "npx": self.npx_path,
            "version": self.version,
            "source": self.source,
            "install_dir": str(self.install_dir) if self.install_dir else None,
        }


class NodeRuntimeError(RuntimeError):
    """Raised when Node cannot be resolved AND auto-install fails."""


class NodeRuntime:
    """Singleton service managing the daemon's Node.js runtime."""

    def __init__(self) -> None:
        self._info: NodeRuntimeInfo | None = None
        self._lock = asyncio.Lock()
        self._auto_install_enabled = True


    @property
    def info(self) -> NodeRuntimeInfo | None:
        """Resolved info or None if `ensure_installed` hasn't been called."""
        return self._info

    @property
    def available(self) -> bool:
        return self._info is not None

    @property
    def node_path(self) -> str:
        self._require_resolved()
        assert self._info is not None
        return self._info.node_path

    @property
    def npm_path(self) -> str | None:
        self._require_resolved()
        assert self._info is not None
        return self._info.npm_path

    @property
    def npx_path(self) -> str | None:
        self._require_resolved()
        assert self._info is not None
        return self._info.npx_path

    @property
    def version(self) -> str:
        self._require_resolved()
        assert self._info is not None
        return self._info.version

    @property
    def env(self) -> dict[str, str]:
        """Return a copy of os.environ with Node's bin dir prepended to PATH."""
        base = dict(os.environ)
        if self._info and self._info.extra_path:
            sep = os.pathsep
            path_parts = [p for p in self._info.extra_path if p]
            path_parts.append(base.get("PATH", ""))
            base["PATH"] = sep.join(p for p in path_parts if p)
        return base

    def _require_resolved(self) -> None:
        if self._info is None:
            raise NodeRuntimeError(
                "NodeRuntime not initialized - call await runtime.ensure_installed()"
            )

    def set_auto_install(self, enabled: bool) -> None:
        """Enable/disable the auto-download path (useful for tests + air-gapped)."""
        self._auto_install_enabled = enabled

    async def ensure_installed(self, auto_install: bool | None = None) -> NodeRuntimeInfo:
        """Resolve a Node runtime, downloading it if needed."""
        async with self._lock:
            if self._info is not None:
                return self._info

            # 1) System PATH
            info = _probe_path_node()
            if info is not None:
                self._info = info
                logger.info(
                    "node_runtime_resolved source=system version=%s path=%s",
                    info.version, info.node_path,
                )
                return info

            # 2) Version managers
            extra = _discover_version_manager_bin()
            if extra is not None:
                env_with_extra = dict(os.environ)
                env_with_extra["PATH"] = f"{extra}{os.pathsep}{env_with_extra.get('PATH', '')}"
                info = _probe_path_node(env=env_with_extra)
                if info is not None:
                    info.source = "version_manager"
                    info.extra_path = [extra]
                    self._info = info
                    logger.info(
                        "node_runtime_resolved source=version_manager version=%s bin=%s",
                        info.version, extra,
                    )
                    return info

            # 3) Auto-install
            do_auto = self._auto_install_enabled if auto_install is None else auto_install
            if not do_auto:
                raise NodeRuntimeError(
                    "Node.js not found and auto_install is disabled. "
                    "Install Node >= v20 from https://nodejs.org/"
                )

            install_dir = _runtimes_dir() / f"node-v{NODE_VERSION}"
            info = await _auto_install_node(NODE_VERSION, install_dir)
            if info is None:
                raise NodeRuntimeError(
                    f"Failed to auto-install Node v{NODE_VERSION}. "
                    "Check your internet connection or install manually from "
                    "https://nodejs.org/"
                )
            self._info = info
            logger.info(
                "node_runtime_installed source=auto_install version=%s path=%s",
                info.version, info.node_path,
            )
            return info

    async def spawn(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        stdout: int | None = asyncio.subprocess.PIPE,
        stderr: int | None = asyncio.subprocess.PIPE,
        stdin: int | None = None,
    ) -> asyncio.subprocess.Process:
        """Spawn a subprocess with Node's bin dir on PATH."""
        self._require_resolved()
        assert self._info is not None

        if command == "node":
            resolved = self._info.node_path
        elif command == "npm" and self._info.npm_path:
            resolved = self._info.npm_path
        elif command == "npx" and self._info.npx_path:
            resolved = self._info.npx_path
        else:
            resolved = command  # let the OS resolve via PATH

        merged_env = self.env
        if env:
            merged_env.update(env)

        arg_list = list(args or [])

        if sys.platform in ("win32", "cygwin") and resolved.lower().endswith(
            (".cmd", ".bat")
        ):
            import subprocess as _sp
            shell_cmdline = _sp.list2cmdline([resolved, *arg_list])
            logger.debug(
                "node_runtime_spawn_shell cmdline=%s cwd=%s",
                shell_cmdline, cwd,
            )
            kwargs: dict[str, Any] = dict(
                cwd=str(cwd) if cwd else None,
                env=merged_env,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
            )
            from digitorn.core.process_group import set_pdeathsig_on_child
            kwargs = set_pdeathsig_on_child(kwargs)
            return await asyncio.create_subprocess_shell(shell_cmdline, **kwargs)

        logger.debug(
            "node_runtime_spawn cmd=%s args=%s cwd=%s",
            resolved, arg_list, cwd,
        )
        kwargs2: dict[str, Any] = dict(
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        from digitorn.core.process_group import set_pdeathsig_on_child
        kwargs2 = set_pdeathsig_on_child(kwargs2)
        return await asyncio.create_subprocess_exec(resolved, *arg_list, **kwargs2)

    async def run(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        """Run a command to completion and return `(rc, stdout, stderr)`."""
        proc = await self.spawn(command, args, cwd=cwd, env=env)
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise
        rc = proc.returncode if proc.returncode is not None else -1
        return rc, stdout_bytes.decode("utf-8", errors="replace"), stderr_bytes.decode("utf-8", errors="replace")


def _runtimes_dir() -> Path:
    """Return the dir where auto-installed runtimes live."""
    from platformdirs import user_data_dir
    return Path(user_data_dir("digitorn")) / _RUNTIMES_DIR_NAME


def _parse_node_version(output: str) -> str:
    s = output.strip()
    if s.startswith("v"):
        s = s[1:]
    return s


def _version_major(version: str) -> int:
    try:
        return int(version.split(".")[0])
    except (ValueError, IndexError):
        return 0


def _probe_path_node(env: dict[str, str] | None = None) -> NodeRuntimeInfo | None:
    if env is None:
        env = dict(os.environ)
    node_exe = _which("node", env=env)
    if node_exe is None:
        return None
    try:
        result = _run_sync(node_exe, ["--version"], env=env, timeout=5.0)
    except Exception as exc:
        logger.debug("node_probe_failed path=%s error=%s", node_exe, exc)
        return None
    if result.returncode != 0:
        return None
    version = _parse_node_version(result.stdout)
    if _version_major(version) < MIN_MAJOR:
        logger.info(
            "node_found_but_too_old path=%s version=%s min=%d",
            node_exe, version, MIN_MAJOR,
        )
        return None
    return NodeRuntimeInfo(
        node_path=node_exe,
        npm_path=_which("npm", env=env),
        npx_path=_which("npx", env=env),
        version=version,
        source="system",
    )


def _discover_version_manager_bin() -> str | None:
    home = Path.home()

    nvm_dir = home / ".nvm" / "versions" / "node"
    if nvm_dir.is_dir():
        for v in sorted(nvm_dir.iterdir(), reverse=True):
            bin_dir = v / "bin"
            if (bin_dir / "node").exists():
                return str(bin_dir)

    fnm_dir = home / ".local" / "share" / "fnm" / "node-versions"
    if fnm_dir.is_dir():
        for v in sorted(fnm_dir.iterdir(), reverse=True):
            bin_dir = v / "installation" / "bin"
            if (bin_dir / "node").exists():
                return str(bin_dir)

    volta_bin = home / ".volta" / "bin"
    if (volta_bin / "node").exists():
        return str(volta_bin)

    return None


def _which(cmd: str, env: dict[str, str] | None = None) -> str | None:
    path = env.get("PATH") if env else None
    resolved = shutil.which(cmd, path=path)
    return resolved


class _CompletedProcess:
    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_sync(
    cmd: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> _CompletedProcess:
    import subprocess
    proc = subprocess.run(
        [cmd, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    return _CompletedProcess(proc.returncode, proc.stdout, proc.stderr)


def _download_target() -> tuple[str, str, str]:
    """Return `(platform_tag, archive_ext, inner_prefix)` for the current host."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "x64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        raise NodeRuntimeError(f"Unsupported arch for Node auto-install: {machine}")

    if sys.platform == "linux":
        return f"linux-{arch}", "tar.xz", f"node-v{NODE_VERSION}-linux-{arch}"
    if sys.platform == "darwin":
        return f"darwin-{arch}", "tar.gz", f"node-v{NODE_VERSION}-darwin-{arch}"
    if sys.platform in ("win32", "cygwin"):
        if arch != "x64":
            raise NodeRuntimeError(
                f"Windows Node auto-install only supports x64 "
                f"(detected {arch}). Install Node manually from https://nodejs.org/"
            )
        return f"win-{arch}", "zip", f"node-v{NODE_VERSION}-win-{arch}"
    raise NodeRuntimeError(f"Unsupported platform for Node auto-install: {sys.platform}")


async def _auto_install_node(
    version: str,
    install_dir: Path,
) -> NodeRuntimeInfo | None:
    install_dir.parent.mkdir(parents=True, exist_ok=True)

    # Already extracted?
    info = _info_from_install_dir(install_dir)
    if info is not None:
        return info

    try:
        platform_tag, ext, inner = _download_target()
    except NodeRuntimeError as exc:
        logger.error("node_auto_install_unsupported: %s", exc)
        return None

    url = f"https://nodejs.org/dist/v{version}/node-v{version}-{platform_tag}.{ext}"
    logger.info("node_auto_install_downloading url=%s", url)

    tmp_dir = install_dir.parent / f".tmp-node-{version}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    archive_path = tmp_dir / f"node-v{version}-{platform_tag}.{ext}"

    try:
        await asyncio.to_thread(_download_file, url, archive_path)
    except Exception as exc:
        logger.error("node_download_failed: %s", exc)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None

    try:
        extracted_root = await asyncio.to_thread(
            _extract_archive, archive_path, tmp_dir,
        )
    except Exception as exc:
        logger.error("node_extract_failed: %s", exc)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None

    # Move extracted_root → install_dir atomically
    try:
        if install_dir.exists():
            shutil.rmtree(install_dir)
        shutil.move(str(extracted_root), str(install_dir))
    except Exception as exc:
        logger.error("node_install_move_failed: %s", exc)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    info = _info_from_install_dir(install_dir)
    if info is None:
        logger.error("node_install_dir_invalid path=%s", install_dir)
        return None
    return info


def _info_from_install_dir(install_dir: Path) -> NodeRuntimeInfo | None:
    if not install_dir.is_dir():
        return None

    # Windows layout: install_dir/node.exe, npm.cmd, npx.cmd at the top.
    # Unix layout: install_dir/bin/node, bin/npm, bin/npx
    if sys.platform in ("win32", "cygwin"):
        node_exe = install_dir / "node.exe"
        if not node_exe.exists():
            return None
        npm = install_dir / "npm.cmd"
        npx = install_dir / "npx.cmd"
        bin_dir = install_dir
    else:
        bin_dir = install_dir / "bin"
        node_exe = bin_dir / "node"
        if not node_exe.exists():
            return None
        npm = bin_dir / "npm"
        npx = bin_dir / "npx"

    try:
        result = _run_sync(str(node_exe), ["--version"], timeout=5.0)
        version = _parse_node_version(result.stdout)
    except Exception:
        version = NODE_VERSION

    return NodeRuntimeInfo(
        node_path=str(node_exe),
        npm_path=str(npm) if npm.exists() else None,
        npx_path=str(npx) if npx.exists() else None,
        version=version,
        source="auto_install",
        install_dir=install_dir,
        bin_dir=bin_dir,
        extra_path=[str(bin_dir)],
    )


def _download_file(url: str, dest: Path) -> None:
    req = Request(url, headers={"User-Agent": "digitorn-daemon/1.0"})
    with urlopen(req, timeout=60.0) as resp:
        if resp.status != 200:
            raise NodeRuntimeError(f"HTTP {resp.status} fetching {url}")
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)


def _extract_archive(archive: Path, dest_dir: Path) -> Path:
    suffix = "".join(archive.suffixes).lower()
    if suffix.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest_dir)
    elif suffix.endswith(".tar.xz"):
        with tarfile.open(archive, "r:xz") as tf:
            tf.extractall(dest_dir, filter="data")
    elif suffix.endswith(".tar.gz") or suffix.endswith(".tgz"):
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(dest_dir, filter="data")
    else:
        raise NodeRuntimeError(f"Unknown archive format: {archive.name}")

    # Find the single top-level dir - nodejs archives have one top folder
    # named like `node-v22.11.0-linux-x64`.
    children = [p for p in dest_dir.iterdir() if p.is_dir() and p.name.startswith("node-")]
    if not children:
        raise NodeRuntimeError("Extracted archive has no node-* top-level dir")
    return children[0]


_runtime: NodeRuntime | None = None


def get_node_runtime() -> NodeRuntime:
    """Return the process-wide NodeRuntime singleton."""
    global _runtime
    if _runtime is None:
        _runtime = NodeRuntime()
    return _runtime


def reset_node_runtime() -> None:
    """Reset the singleton (tests only)."""
    global _runtime
    _runtime = None

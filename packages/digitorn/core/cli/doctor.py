"""digitorn doctor - system health and prerequisites checker."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys

import typer
from rich.console import Console
from rich.table import Table

console = Console()


def doctor() -> None:
    """Check system prerequisites for Digitorn and MCP servers."""

    checks: list[tuple[str, str, bool, str]] = []

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 10)
    checks.append((
        "Python",
        py_ver,
        py_ok,
        "Digitorn requires Python 3.10+." if not py_ok else "",
    ))

    uv = shutil.which("uv")
    pip = shutil.which("pip")
    if uv:
        checks.append(("uv (pip)", _get_version(uv, "--version"), True, ""))
    elif pip:
        checks.append(("pip", _get_version(pip, "--version"), True, ""))
    else:
        checks.append(("pip/uv", "not found", False, "Install pip or uv for Python MCP servers."))

    # NodeRuntime - uses the same detection the daemon does, including
    # auto-installed LTS under ~/.local/share/digitorn/runtimes/.
    import asyncio as _aio
    from digitorn.core.runtime.node_runtime import (
        get_node_runtime, NodeRuntimeError, NODE_VERSION,
    )
    rt = get_node_runtime()
    rt.set_auto_install(False)  # doctor only reports; use `setup` to install
    try:
        info = _aio.run(rt.ensure_installed(auto_install=False))
        source_label = {
            "system": "system PATH",
            "version_manager": "nvm/fnm/volta",
            "auto_install": "auto-installed LTS",
        }.get(info.source, info.source)
        checks.append((
            "Node.js",
            f"v{info.version} ({source_label})",
            True,
            "",
        ))
        checks.append((
            "npm",
            info.npm_path or "not found",
            info.npm_path is not None,
            "Bundled with Node.js - reinstall if missing." if not info.npm_path else "",
        ))
        checks.append((
            "npx",
            info.npx_path or "not found",
            info.npx_path is not None,
            "Bundled with Node.js - reinstall if missing." if not info.npx_path else "",
        ))
    except NodeRuntimeError:
        checks.append((
            "Node.js",
            "not found",
            False,
            f"Run `digitorn setup` to auto-install Node v{NODE_VERSION} LTS, "
            "or install manually from https://nodejs.org/",
        ))

    git = shutil.which("git")
    if git:
        checks.append(("git", _get_version(git, "--version"), True, ""))
    else:
        checks.append(("git", "not found", True, "(optional)"))

    from pathlib import Path
    db_path = Path.home() / ".digitorn" / "digitorn.db"
    checks.append((
        "Database",
        str(db_path) if db_path.exists() else "will be created on first start",
        True,
        "",
    ))

    checks.append(("OS", f"{platform.system()} {platform.machine()}", True, ""))

    table = Table(title="Digitorn Doctor", border_style="cyan")
    table.add_column("Check", style="bold white", min_width=12)
    table.add_column("Status", min_width=20)
    table.add_column("", min_width=3)
    table.add_column("Hint", style="dim")

    all_ok = True
    for name, detail, ok, hint in checks:
        icon = "[green]OK[/green]" if ok else "[red]FAIL[/red]"
        if not ok:
            all_ok = False
        table.add_row(name, detail, icon, hint)

    console.print()
    console.print(table)
    console.print()

    if all_ok:
        console.print("[bold green]All checks passed.[/bold green] Ready to run MCP servers.")
    else:
        console.print("[bold yellow]Some checks failed.[/bold yellow] Fix the issues above for full MCP support.")
        raise typer.Exit(1)


def _get_version(cmd: str, flag: str) -> str:
    """Run a command with a flag and capture the first line of output."""
    try:
        result = subprocess.run(
            [cmd, flag], capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip().split("\n")[0]
    except Exception:
        return "unknown"

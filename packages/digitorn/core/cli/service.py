"""CLI commands for system service management."""

from __future__ import annotations

import platform

import typer
from rich.console import Console

console = Console()
service_cli = typer.Typer(name="service", help="System service management.")


def _get_backend():
    """Return the platform-specific service backend module."""
    os_name = platform.system()
    if os_name == "Windows":
        from digitorn.core.cli import _service_windows as backend
    elif os_name == "Linux":
        from digitorn.core.cli import _service_linux as backend
    elif os_name == "Darwin":
        from digitorn.core.cli import _service_macos as backend
    else:
        console.print(f"[red]Unsupported platform: {os_name}[/red]")
        raise typer.Exit(1)
    return backend


def install_service() -> None:
    """Install the system service (called by setup wizard)."""
    _get_backend().install()


def start_service() -> None:
    """Start the system service (called by setup wizard)."""
    _get_backend().start()


@service_cli.command("install")
def install_command() -> None:
    """Install Digitorn as a system service."""
    backend = _get_backend()
    try:
        backend.install()
        os_name = platform.system()
        labels = {"Windows": "Windows Service", "Linux": "systemd unit", "Darwin": "launchd agent"}
        console.print(f"[green]{labels.get(os_name, 'Service')} installed successfully.[/green]")
        console.print("[dim]Start with: digitorn service start[/dim]")
    except Exception as exc:
        console.print(f"[red]Installation failed: {exc}[/red]")
        raise typer.Exit(1)


@service_cli.command("uninstall")
def uninstall_command() -> None:
    """Remove the Digitorn system service."""
    backend = _get_backend()
    try:
        backend.uninstall()
        console.print("[green]Service removed.[/green]")
    except Exception as exc:
        console.print(f"[red]Uninstall failed: {exc}[/red]")
        raise typer.Exit(1)


@service_cli.command("start")
def start_command() -> None:
    """Start the Digitorn service."""
    backend = _get_backend()
    try:
        backend.start()
        console.print("[green]Service started.[/green]")
    except Exception as exc:
        console.print(f"[red]Start failed: {exc}[/red]")
        raise typer.Exit(1)


@service_cli.command("stop")
def stop_command() -> None:
    """Stop the Digitorn service."""
    backend = _get_backend()
    try:
        backend.stop()
        console.print("[green]Service stopped.[/green]")
    except Exception as exc:
        console.print(f"[red]Stop failed: {exc}[/red]")
        raise typer.Exit(1)


@service_cli.command("status")
def status_command() -> None:
    """Show the service status."""
    backend = _get_backend()
    info = backend.status()

    status_val = info.get("status", "unknown")
    colors = {
        "running": "green",
        "stopped": "yellow",
        "failed": "red",
        "not_installed": "dim",
        "starting": "cyan",
        "stopping": "cyan",
    }
    color = colors.get(status_val, "white")
    console.print(f"Service status: [{color}]{status_val}[/{color}]")

    if "pid" in info:
        console.print(f"  PID: {info['pid']}")


@service_cli.command("logs")
def logs_command() -> None:
    """Show recent service logs."""
    backend = _get_backend()
    output = backend.logs()
    console.print(output)

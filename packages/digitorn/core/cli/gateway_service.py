"""CLI commands for the gateway-go Windows service."""

from __future__ import annotations

import platform
from pathlib import Path

import typer
from rich.console import Console

console = Console()
gateway_service_cli = typer.Typer(
    name="gateway-service",
    help="Manage the gateway-go binary as a system service.",
)


def _get_backend():
    os_name = platform.system()
    if os_name == "Windows":
        from digitorn.core.cli import _gateway_service_windows as backend
        return backend
    console.print(
        f"[red]gateway-service is currently Windows-only "
        f"(detected: {os_name}).[/red]"
    )
    raise typer.Exit(1)


@gateway_service_cli.command("install")
def install_command(
    binary: Path = typer.Option(
        ..., "--binary", "-b",
        help="Absolute path to gateway-go.exe",
        exists=True, dir_okay=False, resolve_path=True,
    ),
    env_file: Path = typer.Option(
        Path.home() / ".digitorn" / "gateway.env",
        "--env-file",
        help="Path to gateway env file (loaded into the gateway process env).",
    ),
    log_path: Path = typer.Option(
        Path.home() / ".digitorn" / "logs" / "gateway.log",
        "--log-path",
        help="Path to gateway stdout/stderr log file.",
    ),
) -> None:
    """Install gateway-go as the DigitornGateway Windows Service."""
    backend = _get_backend()
    try:
        backend.install(
            binary=binary,
            env_file=env_file if env_file and env_file.exists() else None,
            log_path=log_path,
            extra_env=None,
        )
        console.print("[green]DigitornGateway service installed.[/green]")
        console.print(f"  binary:   {binary}")
        if env_file and env_file.exists():
            console.print(f"  env_file: {env_file}")
        if log_path:
            console.print(f"  log_path: {log_path}")
        console.print("[dim]Start with: digitorn gateway-service start[/dim]")
    except Exception as exc:
        console.print(f"[red]Install failed: {exc}[/red]")
        raise typer.Exit(1)


@gateway_service_cli.command("uninstall")
def uninstall_command() -> None:
    """Remove the DigitornGateway Windows Service."""
    backend = _get_backend()
    try:
        backend.uninstall()
        console.print("[green]Service removed.[/green]")
    except Exception as exc:
        console.print(f"[red]Uninstall failed: {exc}[/red]")
        raise typer.Exit(1)


@gateway_service_cli.command("start")
def start_command() -> None:
    """Start the DigitornGateway service."""
    backend = _get_backend()
    try:
        backend.start()
        console.print("[green]Service started.[/green]")
    except Exception as exc:
        console.print(f"[red]Start failed: {exc}[/red]")
        raise typer.Exit(1)


@gateway_service_cli.command("stop")
def stop_command() -> None:
    """Stop the DigitornGateway service."""
    backend = _get_backend()
    try:
        backend.stop()
        console.print("[green]Service stopped.[/green]")
    except Exception as exc:
        console.print(f"[red]Stop failed: {exc}[/red]")
        raise typer.Exit(1)


@gateway_service_cli.command("status")
def status_command() -> None:
    """Show DigitornGateway service status."""
    backend = _get_backend()
    info = backend.status()
    status_val = info.get("status", "unknown")
    colors = {
        "running": "green", "stopped": "yellow", "failed": "red",
        "not_installed": "dim", "starting": "cyan", "stopping": "cyan",
    }
    color = colors.get(status_val, "white")
    console.print(f"Gateway service status: [{color}]{status_val}[/{color}]")


@gateway_service_cli.command("logs")
def logs_command() -> None:
    """Show recent gateway service logs."""
    backend = _get_backend()
    output = backend.logs()
    console.print(output)

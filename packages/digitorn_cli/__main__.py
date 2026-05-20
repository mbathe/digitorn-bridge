"""Entry point: python -m digitorn_cli"""

from __future__ import annotations


import logging

logger = logging.getLogger(__name__)
import typer

from digitorn_cli.run import run_cli
from digitorn_cli.app import app_cli
from digitorn_cli.secrets import secret_cli
from digitorn_cli.modules import modules_cli
from digitorn_cli.sessions import sessions_cli

cli = typer.Typer(
    name="digitorn",
    help="Digitorn CLI - terminal client for the Digitorn daemon.",
    no_args_is_help=True,
)

# Mount sub-commands
cli.add_typer(run_cli, name="run", help="Run an application.")
cli.add_typer(app_cli, name="app", help="Application YAML management.")
cli.add_typer(secret_cli, name="secret", help="Per-app secret management.")
cli.add_typer(modules_cli, name="modules", help="Module management.")
cli.add_typer(sessions_cli, name="sessions", help="Manage your sessions.")


@cli.command()
def login(
    daemon: str = typer.Option("http://127.0.0.1:8000", "--daemon", "-d"),
) -> None:
    """Authenticate with the Digitorn daemon."""
    from digitorn_cli.auth import prompt_login
    prompt_login(daemon)


@cli.command()
def logout() -> None:
    """Clear stored credentials."""
    from digitorn_cli.auth import clear_credentials
    clear_credentials()
    typer.echo("Logged out.")


@cli.command()
def version() -> None:
    """Show CLI version."""
    from digitorn_cli import __version__
    typer.echo(f"digitorn-cli {__version__}")


def _reset_terminal() -> None:
    """Reset terminal to sane state after TUI exit."""
    import sys
    try:
        # Disable mouse tracking + restore cursor + reset modes
        sys.stdout.write("\033[?1000l\033[?1003l\033[?1006l\033[?25h\033[?1049l")
        sys.stdout.flush()
    except Exception as exc:
        logger.debug("__main__ best-effort block failed: %s", exc)


def main() -> None:
    try:
        cli()
    except KeyboardInterrupt:
        typer.echo("\nInterrupted.", err=True)
        raise SystemExit(0)
    except SystemExit:
        raise
    except Exception as exc:
        # NEVER show a traceback to the user
        import sys
        error_type = type(exc).__name__
        message = str(exc)

        # Common errors with friendly messages
        if "connect" in message.lower() or "refused" in message.lower():
            typer.echo(f"\n  ✗ Cannot connect to daemon. Is it running?", err=True)
            typer.echo(f"    Start it with: digitorn start\n", err=True)
        elif "timeout" in message.lower() or "timed out" in message.lower():
            typer.echo(f"\n  ✗ Connection timed out. The daemon may be overloaded.", err=True)
        elif "401" in message or "auth" in message.lower():
            typer.echo(f"\n  ✗ Authentication failed. Try: digitorn-cli login", err=True)
        else:
            typer.echo(f"\n  ✗ {error_type}: {message}", err=True)

        # Debug: write full traceback to log file (not terminal)
        import traceback
        from pathlib import Path
        log_dir = Path.home() / ".digitorn"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "cli-error.log"
        with open(log_file, "a", encoding="utf-8") as f:
            import datetime
            f.write(f"\n--- {datetime.datetime.now().isoformat()} ---\n")
            traceback.print_exc(file=f)
        typer.echo(f"    Details: {log_file}\n", err=True)

        raise SystemExit(1)


if __name__ == "__main__":
    main()

"""CLI command: digitorn run <app_id_or_path> [message]

Runs a Digitorn application by app_id (deployed) or YAML path.
All execution goes through the daemon — the CLI is a pure HTTP client.
All rendering goes through the Textual TUI — no Rich CLI rendering.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

run_cli = typer.Typer(
    name="run",
    help="Run a Digitorn application.",
    no_args_is_help=False,
)


def _resolve_input(
    message: str | None,
    input_file: Path | None,
    image: Path | None,
    json_input: str | None,
    stdin: bool,
) -> str | None:
    """Resolve user input from CLI arguments.

    Priority: message > input_file > image > json_input > stdin.
    Returns None if no input provided (interactive mode).
    """
    if message:
        return message
    if input_file:
        if not input_file.exists():
            typer.echo(f"Error: Input file not found: {input_file}", err=True)
            raise typer.Exit(1)
        return input_file.read_text(encoding="utf-8")
    if image:
        if not image.exists():
            typer.echo(f"Error: Image file not found: {image}", err=True)
            raise typer.Exit(1)
        return f"[image: {image}]"
    if json_input:
        json_path = Path(json_input)
        if json_path.exists():
            return json_path.read_text(encoding="utf-8")
        return json_input
    if stdin:
        return sys.stdin.read()
    return None


@run_cli.callback(invoke_without_command=True)
def run(
    app: str = typer.Argument(
        ..., help="App ID (deployed) or path to app YAML file."
    ),
    message: Annotated[
        str | None, typer.Argument(help="Input message (one_shot mode).")
    ] = None,
    input_file: Annotated[
        Path | None, typer.Option("--input", "-i", help="Read input from a file.")
    ] = None,
    image: Annotated[
        Path | None, typer.Option("--image", help="Image file as input.")
    ] = None,
    json_input: Annotated[
        str | None, typer.Option("--json", "-j", help="JSON string or file as input.")
    ] = None,
    stdin: bool = typer.Option(False, "--stdin", help="Read input from stdin."),
    session: Annotated[
        str | None, typer.Option("--session", "-s", help="Resume a previous session by ID.")
    ] = None,
    daemon: str = typer.Option(
        "http://127.0.0.1:8000", "--daemon", "-d", help="Daemon URL."
    ),
) -> None:
    """Run a Digitorn application via the daemon."""
    from digitorn_cli.auth import get_auth_headers

    app_path = Path(app).expanduser().resolve()
    is_yaml = app_path.suffix in (".yaml", ".yml")

    # Authenticate before TUI takes the terminal
    try:
        auth_headers = get_auth_headers(daemon)
    except typer.Exit:
        raise

    # Resolve input (message, file, stdin, etc.)
    user_input = _resolve_input(message, input_file, image, json_input, stdin)

    # Everything goes through the Textual TUI
    try:
        from digitorn_cli.tui import launch_tui
        launch_tui(
            daemon_url=daemon,
            app_id=app if not is_yaml else None,
            app_path=app_path if is_yaml else None,
            session_id=session,
            auth_headers=auth_headers,
            initial_message=user_input or None,
            exit_on_complete=bool(user_input),
        )
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        # Always reset terminal after TUI exits (prevents mouse tracking garbage)
        import sys as _sys
        try:
            _sys.stdout.write(
                "\033[?1000l"   # Disable mouse click tracking
                "\033[?1003l"   # Disable mouse movement tracking
                "\033[?1006l"   # Disable SGR mouse mode
                "\033[?1015l"   # Disable urxvt mouse mode
                "\033[?25h"     # Show cursor
            )
            _sys.stdout.flush()
        except Exception:
            pass

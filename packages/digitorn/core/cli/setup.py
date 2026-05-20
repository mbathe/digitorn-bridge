"""CLI command: digitorn setup - Interactive installation wizard."""

from __future__ import annotations

import platform
import sys
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

console = Console()


CORE_MODULES = [
    "hello",
    "llm_provider",
    "http",
    "context_builder",
    "index",
]

MODULE_CATEGORIES: dict[str, dict[str, str]] = {
    "AI & RAG": {
        "rag": "Retrieval-augmented generation with hybrid search & citations",
        "vector": "Vector store management (Qdrant)",
        "memory": "Conversation memory & context recall",
        "agent_spawn": "Spawn specialist sub-agents",
    },
    "Data": {
        "database": "SQL/NoSQL database queries",
        "spreadsheet": "Excel spreadsheet generation",
        "cache": "Caching layer for tool results",
        "queue": "Async task queue",
    },
    "System": {
        "shell": "System command execution",
        "filesystem": "File & directory operations",
        "git": "Git repository operations",
        "notebook": "Jupyter notebook manipulation",
    },
    "Web & Communication": {
        "browser": "Web scraping via Playwright",
        "web": "HTTP/HTML content extraction",
        "channels": "Multi-channel input (webhook, cron, email, etc.)",
    },
    "Automation": {
        "cron_native": "Scheduled task execution",
        "mcp": "MCP server integration",
        "lsp": "Language server protocol",
    },
    "Document Generation": {
        "pdf": "PDF document generation",
        "presentation": "PowerPoint presentation generation",
    },
}


def _step_prerequisites() -> None:
    """Step 0: Verify and install system prerequisites (Node.js)."""
    import asyncio as _aio

    from digitorn.core.runtime.node_runtime import (
        NODE_VERSION,
        NodeRuntimeError,
        get_node_runtime,
    )

    console.print()
    console.print("[bold]Prerequisites - Node.js[/bold]")
    console.print()

    rt = get_node_runtime()
    rt.set_auto_install(False)
    try:
        info = _aio.run(rt.ensure_installed(auto_install=False))
        source_label = {
            "system": "system PATH",
            "version_manager": "nvm/fnm/volta",
            "auto_install": "digitorn runtime cache",
        }.get(info.source, info.source)
        console.print(
            f"  [green]Node.js v{info.version} found ({source_label})[/green]"
        )
        return
    except NodeRuntimeError:
        pass

    console.print("  [yellow]Node.js not found.[/yellow]")
    console.print(
        "  Node.js is required for most MCP servers and for the app preview "
        "dev-server pipeline."
    )
    console.print()
    install = Confirm.ask(
        f"  Auto-download Node v{NODE_VERSION} LTS "
        "(~30 MB) to ~/.local/share/digitorn/runtimes?",
        default=True,
    )
    if not install:
        console.print(
            "  [dim]Skipped. Install Node.js manually from "
            "https://nodejs.org/ before using Node-dependent features.[/dim]"
        )
        return

    console.print("  Downloading...", end=" ", highlight=False)
    try:
        rt.set_auto_install(True)
        info = _aio.run(rt.ensure_installed(auto_install=True))
        console.print(f"[green]OK[/green] v{info.version}")
    except Exception as exc:
        console.print(f"[red]FAILED[/red] ({exc})")
        console.print(
            "  [dim]Install manually from https://nodejs.org/ and re-run setup.[/dim]"
        )


def _step_database(config_data: dict) -> None:
    """Step 1: Choose database backend."""
    console.print()
    console.print("[bold]Step 1/4 - Database[/bold]")
    console.print()
    console.print("  [bold]1[/bold] SQLite (local, zero config) - recommended for development")
    console.print("  [bold]2[/bold] PostgreSQL (production, multi-user)")
    console.print()

    choice = Prompt.ask("  Choose", choices=["1", "2"], default="1")

    if choice == "2":
        console.print()
        pg_host = Prompt.ask("  PostgreSQL host", default="localhost")
        pg_port = Prompt.ask("  PostgreSQL port", default="5432")
        pg_db = Prompt.ask("  Database name", default="digitorn")
        pg_user = Prompt.ask("  Username", default="digitorn_user")
        pg_pass = Prompt.ask("  Password", password=True)

        db_url = f"postgresql+asyncpg://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}"

        console.print()
        console.print("  Testing connection...", end=" ")

        import asyncio

        try:
            import asyncpg

            conn = asyncio.get_event_loop().run_until_complete(
                asyncpg.connect(
                    host=pg_host,
                    port=int(pg_port),
                    database=pg_db,
                    user=pg_user,
                    password=pg_pass,
                    timeout=5,
                )
            )
            asyncio.get_event_loop().run_until_complete(conn.close())
            console.print("[bold green]OK[/bold green]")
        except Exception as exc:
            if "asyncpg" in sys.modules:
                import asyncpg as _apg

                if isinstance(exc, _apg.InvalidCatalogNameError):
                    console.print("[yellow]database not found - will be created on first start[/yellow]")
                else:
                    console.print(f"[yellow]connection failed: {exc}[/yellow]")
                    console.print("  [dim]The database will be created when connectivity is restored.[/dim]")
            else:
                console.print(f"[yellow]asyncpg not installed - skipping test[/yellow]")

        config_data["database"] = {"url": db_url}
    else:
        db_path = str(Path.home() / ".digitorn" / "digitorn.db")
        config_data["database"] = {"url": f"sqlite+aiosqlite:///{db_path}"}
        console.print(f"  [green]SQLite database: {db_path}[/green]")


def _step_server(config_data: dict) -> None:
    """Step 2: Configure server host & port."""
    console.print()
    console.print("[bold]Step 2/4 - Server[/bold]")
    console.print()

    host = Prompt.ask("  Host", default="127.0.0.1")
    port = Prompt.ask("  Port", default="8000")

    try:
        port_int = int(port)
        if not (1024 <= port_int <= 65535):
            raise ValueError
    except ValueError:
        console.print("[red]  Invalid port. Using default 8000.[/red]")
        port_int = 8000

    config_data.setdefault("server", {})
    config_data["server"]["host"] = host
    config_data["server"]["port"] = port_int

    console.print(f"  [green]Server will listen on {host}:{port_int}[/green]")


def _step_modules(config_data: dict) -> None:
    """Step 3: Select modules to enable."""
    console.print()
    console.print("[bold]Step 3/4 - Modules[/bold]")
    console.print()

    # Show core modules (always enabled)
    core_list = ", ".join(CORE_MODULES)
    console.print(f"  [dim]Core (always enabled):[/dim] {core_list}")
    console.print()

    # Ask: install all or select?
    install_all = Confirm.ask("  Install all additional modules?", default=True)

    if install_all:
        config_data.setdefault("modules", {})
        config_data["modules"]["load_all"] = True
        config_data["modules"]["disabled"] = []
        total = sum(len(mods) for mods in MODULE_CATEGORIES.values())
        console.print(f"  [green]All {total} additional modules enabled.[/green]")
        return

    # Interactive module selection per category
    enabled_modules: list[str] = list(CORE_MODULES)

    for category, modules in MODULE_CATEGORIES.items():
        console.print(f"\n  [bold cyan]{category}[/bold cyan]")
        for mod_id, description in modules.items():
            install = Confirm.ask(f"    {mod_id} - {description}", default=False)
            if install:
                enabled_modules.append(mod_id)

    config_data.setdefault("modules", {})
    config_data["modules"]["load_all"] = False
    config_data["modules"]["enabled"] = enabled_modules

    console.print()
    extra = [m for m in enabled_modules if m not in CORE_MODULES]
    if extra:
        console.print(f"  [green]Selected: {', '.join(extra)}[/green]")
    else:
        console.print("  [dim]No additional modules selected.[/dim]")


def _step_service(config_data: dict) -> bool:
    """Step 4: Install system service."""
    console.print()
    console.print("[bold]Step 4/4 - System Service[/bold]")
    console.print()

    os_name = platform.system()
    service_names = {
        "Windows": "Windows Service",
        "Linux": "systemd unit",
        "Darwin": "launchd agent",
    }
    service_label = service_names.get(os_name, "system service")

    install = Confirm.ask(
        f"  Install Digitorn as a {service_label} (runs in background, starts at boot)?",
        default=True,
    )

    if install:
        console.print(f"  [green]Will install {service_label}.[/green]")

    return install


def _write_config(config_data: dict) -> Path:
    """Write configuration to ~/.digitorn/config.yaml."""
    config_path = Path.home() / ".digitorn" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump(config_data, default_flow_style=False))
    return config_path


def _install_service() -> None:
    """Install and start the system service."""
    try:
        from digitorn.core.cli.service import install_service, start_service

        console.print()
        console.print("  Installing service...", end=" ")
        install_service()
        console.print("[bold green]OK[/bold green]")

        start_now = Confirm.ask("  Start the service now?", default=True)
        if start_now:
            console.print("  Starting...", end=" ")
            start_service()
            console.print("[bold green]OK[/bold green]")
    except Exception as exc:
        console.print(f"[yellow]Service installation failed: {exc}[/yellow]")
        console.print("  [dim]You can install the service later with: digitorn service install[/dim]")


def setup_command(
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Use defaults without prompting (for automated installs).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing configuration.",
    ),
) -> None:
    """Interactive setup wizard for Digitorn."""
    config_path = Path.home() / ".digitorn" / "config.yaml"

    if config_path.exists() and not force:
        console.print(f"[yellow]Configuration already exists at {config_path}[/yellow]")
        overwrite = Confirm.ask("  Overwrite?", default=False)
        if not overwrite:
            console.print("[dim]Setup cancelled.[/dim]")
            raise typer.Exit()

    console.print()
    console.print(
        Panel(
            "[bold cyan]Digitorn[/bold cyan] - Installation Setup",
            border_style="cyan",
        )
    )

    config_data: dict = {}
    install_svc = False

    if non_interactive:
        # Use all defaults
        db_path = str(Path.home() / ".digitorn" / "digitorn.db")
        config_data = {
            "database": {"url": f"sqlite+aiosqlite:///{db_path}"},
            "server": {"host": "127.0.0.1", "port": 8000},
            "modules": {"load_all": True},
        }
        install_svc = True
    else:
        _step_prerequisites()
        _step_database(config_data)
        _step_server(config_data)
        _step_modules(config_data)
        install_svc = _step_service(config_data)

    # Write config
    path = _write_config(config_data)
    console.print()
    console.print(f"  Configuration saved to [cyan]{path}[/cyan]")

    # Install service if requested
    if install_svc:
        _install_service()

    # Summary
    console.print()
    console.print(Panel("[bold green]Setup complete![/bold green]", border_style="green"))
    console.print()
    if not install_svc:
        console.print("  Start the daemon manually with: [cyan]digitorn start[/cyan]")
        console.print("  Or install as a service later:  [cyan]digitorn service install[/cyan]")
    console.print()

"""Slash command registry and handlers for the Digitorn CLI.

All slash commands (``/help``, ``/tools``, ``/tasks``, etc.) are defined here.
Handlers receive a :class:`CLISession` and work identically in standalone and
daemon modes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

from rich.console import Console
from rich.table import Table

from digitorn_cli.session import CLISession


class QuitSignal(Exception):
    """Raised by /quit to cleanly exit the conversation loop."""


@dataclass
class SlashCommand:
    """A single slash command."""

    name: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    handler: Callable[..., None] = field(default=lambda s, a, c: None)


class SlashCommandRegistry:
    """Registry of slash commands with dispatch."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}
        self._alias_map: dict[str, str] = {}
        self._register_defaults()

    def register(self, cmd: SlashCommand) -> None:
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self._alias_map[alias] = cmd.name

    def get(self, name: str) -> SlashCommand | None:
        if name in self._commands:
            return self._commands[name]
        canonical = self._alias_map.get(name)
        if canonical:
            return self._commands.get(canonical)
        return None

    def all_names(self) -> list[str]:
        """All command names and aliases, for tab completion."""
        names = list(self._commands.keys())
        names.extend(self._alias_map.keys())
        return sorted(names)

    def dispatch(self, line: str, session: CLISession, console: Console) -> bool:
        """Parse and dispatch a slash command line.

        Returns True if the line was handled, False if not a valid command.
        Raises QuitSignal for /quit.
        """
        parts = line.strip().split(None, 2)
        if not parts:
            return False

        cmd_name = parts[0].lower()
        cmd = self.get(cmd_name)
        if cmd is None:
            console.print(f"  [dim]Unknown command: {cmd_name}. Type /help for a list.[/dim]")
            return True

        args = " ".join(parts[1:]) if len(parts) > 1 else ""
        try:
            cmd.handler(session, args, console)
        except QuitSignal:
            raise
        except Exception as exc:
            console.print(f"  [red]Command error: {exc}[/red]")

        return True

    def _register_defaults(self) -> None:
        """Register all built-in slash commands."""
        self.register(SlashCommand(
            name="/help", aliases=["/h", "/?"],
            description="List all slash commands",
            handler=_cmd_help,
        ))
        self.register(SlashCommand(
            name="/quit", aliases=["/exit", "/q"],
            description="Exit the conversation",
            handler=_cmd_quit,
        ))
        self.register(SlashCommand(
            name="/status", aliases=["/s"],
            description="Show session status, active tasks, watchers",
            handler=_cmd_status,
        ))
        self.register(SlashCommand(
            name="/tools", aliases=["/t"],
            description="Search tools or list categories (/tools list, /tools exec)",
            handler=_cmd_tools,
        ))
        self.register(SlashCommand(
            name="/tasks", aliases=["/bg"],
            description="List background tasks (/tasks cancel <id>)",
            handler=_cmd_tasks,
        ))
        self.register(SlashCommand(
            name="/watchers", aliases=["/w"],
            description="List watchers (/watchers stop|pause|resume <id>)",
            handler=_cmd_watchers,
        ))
        self.register(SlashCommand(
            name="/mcp",
            description="List MCP servers (/mcp health [server_id])",
            handler=_cmd_mcp,
        ))
        self.register(SlashCommand(
            name="/session",
            description="Session info (/session history|clear|list|resume)",
            handler=_cmd_session,
        ))
        self.register(SlashCommand(
            name="/compact",
            description="Trigger context compaction (standalone only)",
            handler=_cmd_compact,
        ))
        self.register(SlashCommand(
            name="/model", aliases=["/m"],
            description="Show model and context window info",
            handler=_cmd_model,
        ))
        self.register(SlashCommand(
            name="/connect", aliases=["/auth"],
            description="Connect an MCP server via OAuth (/connect <server_id>)",
            handler=_cmd_connect,
        ))
        self.register(SlashCommand(
            name="/disconnect",
            description="Revoke OAuth and disconnect an MCP server (/disconnect <server_id>)",
            handler=_cmd_disconnect,
        ))
        self.register(SlashCommand(
            name="/memory", aliases=["/mem"],
            description="Show agent memory (facts, goals, todos, episodes)",
            handler=_cmd_memory,
        ))
        self.register(SlashCommand(
            name="/cost", aliases=["/usage"],
            description="Show token usage for this session",
            handler=_cmd_cost,
        ))
        self.register(SlashCommand(
            name="/diff", aliases=["/changes"],
            description="Show all file changes (git diff + checkpoints)",
            handler=_cmd_diff,
        ))
        self.register(SlashCommand(
            name="/doctor", aliases=["/check"],
            description="Check system health (provider, modules, MCP)",
            handler=_cmd_doctor,
        ))
        self.register(SlashCommand(
            name="/rewind",
            description="Restore files to a previous checkpoint",
            handler=_cmd_rewind,
        ))


def _cmd_help(session: CLISession, args: str, console: Console) -> None:
    """List all available slash commands."""
    registry = _cmd_help._registry  # type: ignore[attr-defined]
    if registry is None:
        console.print("  [dim]No commands registered.[/dim]")
        return

    console.print()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold cyan", min_width=20)
    table.add_column(style="dim")

    for cmd in sorted(registry._commands.values(), key=lambda c: c.name):
        aliases = f" ({', '.join(cmd.aliases)})" if cmd.aliases else ""
        table.add_row(f"{cmd.name}{aliases}", cmd.description)

    console.print(table)
    console.print()

_cmd_help._registry = None  # type: ignore[attr-defined]


def _cmd_quit(session: CLISession, args: str, console: Console) -> None:
    raise QuitSignal()


def _cmd_status(session: CLISession, args: str, console: Console) -> None:
    """Show session status overview."""
    status = session.get_status()

    console.print()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim white", min_width=16)
    table.add_column(style="white")

    table.add_row("Mode", status.get("mode", "?"))
    table.add_row("App", status.get("app_id", "?"))
    table.add_row("Session", status.get("session_id", "?")[:16])

    if "daemon_url" in status:
        table.add_row("Daemon", status["daemon_url"])
    if "agent_id" in status:
        table.add_row("Agent", status["agent_id"])
    if "tool_injection" in status:
        table.add_row("Tool mode", status["tool_injection"])

    tools = status.get("total_tools", 0)
    cats = status.get("total_categories", 0)
    if tools:
        table.add_row("Tools", f"{tools} across {cats} modules")

    active_tasks = status.get("active_tasks", 0)
    active_watchers = status.get("active_watchers", 0)
    table.add_row("Tasks", f"{active_tasks} running" if active_tasks else "none")
    table.add_row("Watchers", f"{active_watchers} active" if active_watchers else "none")

    msgs = status.get("messages")
    if msgs is not None:
        table.add_row("Messages", str(msgs))

    console.print(table)
    console.print()


def _cmd_tools(session: CLISession, args: str, console: Console) -> None:
    """Handle /tools [list|exec|<query>]."""
    parts = args.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""

    if subcmd == "list":
        _tools_list(session, console)
    elif subcmd == "exec":
        _tools_exec(session, parts[1] if len(parts) > 1 else "", console)
    elif subcmd:
        _tools_search(session, args.strip(), console)
    else:
        _tools_list(session, console)


def _tools_list(session: CLISession, console: Console) -> None:
    """List all tool categories."""
    categories = session.list_categories()
    if not categories:
        console.print("  [dim]No tool categories found.[/dim]")
        return

    console.print()
    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column("Category", style="bold cyan")
    table.add_column("Tools", style="white", justify="right")

    for cat in categories:
        if isinstance(cat, dict):
            table.add_row(
                str(cat.get("id", cat.get("name", "?"))),
                str(cat.get("count", cat.get("tools_count", "?"))),
            )
        elif isinstance(cat, str):
            table.add_row(cat, "")

    console.print(table)
    console.print()
    console.print("  [dim]Use /tools <query> to search, or /tools exec <name> <json> to execute[/dim]")
    console.print()


def _tools_search(session: CLISession, query: str, console: Console) -> None:
    """Search tools by query."""
    results = session.search_tools(query)
    if not results:
        console.print(f"  [dim]No tools found for \"{query}\".[/dim]")
        return

    console.print()
    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column("Tool", style="bold cyan")
    table.add_column("Description", style="dim", max_width=60)
    table.add_column("Score", style="yellow", justify="right")

    for r in results[:15]:
        if isinstance(r, dict):
            table.add_row(
                str(r.get("name", r.get("fqn", "?"))),
                str(r.get("description", ""))[:60],
                str(r.get("score", ""))[:5],
            )

    console.print(table)
    console.print()


def _tools_exec(session: CLISession, args: str, console: Console) -> None:
    """Execute a tool directly: /tools exec <name> <json_params>."""
    parts = args.strip().split(None, 1)
    if not parts:
        console.print("  [dim]Usage: /tools exec <tool_name> {\"param\": \"value\"}[/dim]")
        return

    name = parts[0]
    params: dict[str, Any] = {}
    if len(parts) > 1:
        try:
            params = json.loads(parts[1])
        except json.JSONDecodeError:
            console.print("  [red]Invalid JSON params.[/red]")
            return

    console.print(f"  [cyan]> Executing {name}...[/cyan]")
    result = session.execute_tool(name, params)

    console.print()
    if isinstance(result, dict) and result.get("error"):
        console.print(f"  [red]Error: {result['error']}[/red]")
    else:
        try:
            formatted = json.dumps(result, indent=2, ensure_ascii=False, default=str)
            if len(formatted) > 2000:
                formatted = formatted[:2000] + "\n... (truncated)"
            from rich.syntax import Syntax
            console.print(Syntax(formatted, "json", theme="monokai"))
        except Exception:
            console.print(f"  {result}")
    console.print()


def _cmd_tasks(session: CLISession, args: str, console: Console) -> None:
    """Handle /tasks [cancel <id>]."""
    parts = args.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""

    if subcmd == "cancel":
        task_id = parts[1].strip() if len(parts) > 1 else ""
        if not task_id:
            console.print("  [dim]Usage: /tasks cancel <task_id>[/dim]")
            return
        ok = session.cancel_task(task_id)
        if ok:
            console.print(f"  [green]Task {task_id} cancelled.[/green]")
        else:
            console.print(f"  [red]Failed to cancel task {task_id}.[/red]")
        return

    tasks = session.list_tasks()
    if not tasks:
        console.print("  [dim]No background tasks.[/dim]")
        return

    console.print()
    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column("ID", style="bold", max_width=12)
    table.add_column("Tool", style="cyan")
    table.add_column("Status", style="yellow")
    table.add_column("Result", style="dim", max_width=40)

    for t in tasks:
        if not isinstance(t, dict):
            continue
        status = t.get("status", "?")
        status_style = {"running": "yellow", "completed": "green", "failed": "red"}.get(status, "dim")
        result_str = ""
        if status == "completed":
            result_str = str(t.get("result", ""))[:40]
        elif status == "failed":
            result_str = str(t.get("error", ""))[:40]

        table.add_row(
            str(t.get("task_id", t.get("id", "?")))[:12],
            str(t.get("tool", t.get("name", "?"))),
            f"[{status_style}]{status}[/{status_style}]",
            result_str,
        )

    console.print(table)
    console.print()


def _cmd_watchers(session: CLISession, args: str, console: Console) -> None:
    """Handle /watchers [stop|pause|resume <id>]."""
    parts = args.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""
    target_id = parts[1].strip() if len(parts) > 1 else ""

    if subcmd in ("stop", "pause", "resume") and target_id:
        action_map = {
            "stop": session.stop_watcher,
            "pause": session.pause_watcher,
            "resume": session.resume_watcher,
        }
        ok = action_map[subcmd](target_id)
        verb = {"stop": "stopped", "pause": "paused", "resume": "resumed"}[subcmd]
        if ok:
            console.print(f"  [green]Watcher {target_id} {verb}.[/green]")
        else:
            console.print(f"  [red]Failed to {subcmd} watcher {target_id}.[/red]")
        return

    if subcmd in ("stop", "pause", "resume"):
        console.print(f"  [dim]Usage: /watchers {subcmd} <watcher_id>[/dim]")
        return

    watchers = session.list_watchers()
    if not watchers:
        console.print("  [dim]No watchers.[/dim]")
        return

    console.print()
    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column("ID", style="bold", max_width=12)
    table.add_column("Tool", style="cyan")
    table.add_column("Status", style="yellow")
    table.add_column("Checks", style="dim", justify="right")
    table.add_column("Interval", style="dim", justify="right")

    for w in watchers:
        if not isinstance(w, dict):
            continue
        status = w.get("status", "?")
        status_style = {"running": "green", "paused": "yellow", "stopped": "red"}.get(status, "dim")
        table.add_row(
            str(w.get("watcher_id", w.get("id", "?")))[:12],
            str(w.get("tool", w.get("name", "?"))),
            f"[{status_style}]{status}[/{status_style}]",
            str(w.get("checks", w.get("check_count", "?"))),
            f"{w.get('interval', '?')}s",
        )

    console.print(table)
    console.print()
    console.print("  [dim]Use /watchers stop|pause|resume <id>[/dim]")
    console.print()


def _cmd_mcp(session: CLISession, args: str, console: Console) -> None:
    """Handle /mcp [health [server_id]]."""
    parts = args.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""

    if subcmd == "health":
        server_id = parts[1].strip() if len(parts) > 1 else None
        health = session.mcp_health(server_id)
        console.print()
        if isinstance(health, dict) and health.get("error"):
            console.print(f"  [red]{health['error']}[/red]")
        elif isinstance(health, dict):
            for k, v in health.items():
                console.print(f"  [dim]{k}:[/dim] {v}")
        else:
            console.print(f"  {health}")
        console.print()
        return

    servers = session.list_mcp_servers()
    if not servers:
        console.print("  [dim]No MCP servers connected.[/dim]")
        return

    console.print()
    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column("Server", style="bold cyan")
    table.add_column("Status", style="yellow")
    table.add_column("Tools", style="dim", justify="right")

    for s in servers:
        if not isinstance(s, dict):
            continue
        status = s.get("status", s.get("id", "?"))
        table.add_row(
            str(s.get("server_id", s.get("id", "?"))),
            str(status),
            str(s.get("tools_count", s.get("count", "?"))),
        )

    console.print(table)
    console.print()
    console.print("  [dim]Use /mcp health [server_id] for connectivity check[/dim]")
    console.print()


def _cmd_session(session: CLISession, args: str, console: Console) -> None:
    """Handle /session [history|clear|list|resume <id>]."""
    parts = args.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""

    if subcmd == "history":
        messages = session.get_session_history()
        if not messages:
            console.print("  [dim]No messages in history.[/dim]")
            return
        console.print()
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "?")
            content = str(msg.get("content", ""))
            if len(content) > 120:
                content = content[:117] + "..."
            role_style = {
                "user": "bold white",
                "assistant": "cyan",
                "system": "dim yellow",
            }.get(role, "dim")
            console.print(f"  [{role_style}]{role}:[/{role_style}] {content}")
        console.print()
        return

    if subcmd == "clear":
        ok = session.clear_session()
        if ok:
            console.print("  [green]Session cleared.[/green]")
        else:
            console.print("  [red]Failed to clear session.[/red]")
        return

    if subcmd == "list":
        if session.mode != "daemon":
            console.print("  [dim]Session list is only available in daemon mode.[/dim]")
            return
        sessions = session.list_sessions()
        if not sessions:
            console.print("  [dim]No active sessions.[/dim]")
            return
        console.print()
        table = Table(show_header=True, box=None, padding=(0, 2))
        table.add_column("Session ID", style="bold")
        table.add_column("Created", style="dim")
        table.add_column("Messages", style="cyan", justify="right")
        for s in sessions:
            if not isinstance(s, dict):
                continue
            table.add_row(
                str(s.get("session_id", s.get("id", "?")))[:16],
                str(s.get("created_at", s.get("created", "")))[:19],
                str(s.get("message_count", s.get("messages", "?"))),
            )
        console.print(table)
        console.print()
        console.print("  [dim]Use /session resume <session_id> to switch[/dim]")
        console.print()
        return

    if subcmd == "resume":
        if session.mode != "daemon":
            console.print("  [dim]Session resume is only available in daemon mode.[/dim]")
            return
        new_id = parts[1].strip() if len(parts) > 1 else ""
        if not new_id:
            console.print("  [dim]Usage: /session resume <session_id>[/dim]")
            return
        session.session_id = new_id  # type: ignore[misc]
        console.print(f"  [green]Switched to session {new_id[:16]}[/green]")
        return

    console.print()
    console.print(f"  [dim]Session ID:[/dim] {session.session_id}")
    console.print(f"  [dim]App ID:[/dim]     {session.app_id}")
    console.print(f"  [dim]Mode:[/dim]       {session.mode}")
    console.print()
    console.print("  [dim]Subcommands: history, clear, list, resume <id>[/dim]")
    console.print()


def _cmd_compact(session: CLISession, args: str, console: Console) -> None:
    """Trigger manual context compaction (standalone only)."""
    if session.mode != "standalone":
        console.print("  [dim]Context compaction is automatic in daemon mode.[/dim]")
        return

    console.print("  [dim]Context compaction is managed automatically by the hook runner.[/dim]")
    console.print("  [dim]The agent's context will be compacted when it approaches limits.[/dim]")


def _cmd_model(session: CLISession, args: str, console: Console) -> None:
    """Show model and context window info."""
    info = session.get_model_info()
    console.print()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim white", min_width=16)
    table.add_column(style="white")

    for k, v in info.items():
        table.add_row(k, str(v))

    console.print(table)
    console.print()


def _cmd_connect(session: CLISession, args: str, console: Console) -> None:
    """Handle /connect [server_id] — trigger OAuth flow for an MCP server.

    Without arguments: lists servers that need OAuth.
    With a server_id: starts the OAuth flow for that server.
    """
    import asyncio
    import logging

    logger = logging.getLogger(__name__)

    server_id = args.strip() if args else ""

    if session.mode == "daemon":
        _connect_daemon(session, server_id, console, logger)
    else:
        _connect_standalone(session, server_id, console, logger)


def _connect_daemon(
    session: CLISession, server_id: str, console: Console, logger: Any,
) -> None:
    """OAuth connect via daemon API."""
    from digitorn_cli.auth import daemon_request

    daemon_url = getattr(session, "daemon_url", "http://127.0.0.1:8000")
    app_id = session.app_id

    try:
        resp = daemon_request(
            "get",
            f"{daemon_url}/api/apps/{app_id}/mcp/pending-oauth",
            daemon=daemon_url,
            timeout=10.0,
        )
        if resp.status_code != 200:
            console.print(f"  [red]Error fetching pending OAuth: {resp.status_code}[/red]")
            return
        pending = resp.json().get("data", {}).get("pending", [])
    except Exception as exc:
        console.print(f"  [red]Error: {exc}[/red]")
        return

    if not pending:
        console.print("  [dim]All MCP servers are connected (no pending OAuth).[/dim]")
        return

    if not server_id:
        console.print()
        console.print("  [yellow]MCP servers requiring OAuth:[/yellow]")
        for s in pending:
            console.print(f"    \u2022 [bold cyan]{s['server_id']}[/bold cyan] ({s['provider']})")
        console.print()
        console.print("  [dim]Use /connect <server_id> to authorize.[/dim]")
        console.print()
        return

    target = None
    for s in pending:
        if s["server_id"] == server_id:
            target = s
            break

    if target is None:
        all_servers = session.list_mcp_servers()
        found = any(
            s.get("server_id", s.get("id")) == server_id
            for s in all_servers if isinstance(s, dict)
        )
        if found:
            console.print(f"  [green]\u2713 Server '{server_id}' is already connected.[/green]")
        else:
            console.print(f"  [red]Server '{server_id}' not found.[/red]")
            console.print("  [dim]Available servers needing OAuth:[/dim]")
            for s in pending:
                console.print(f"    \u2022 {s['server_id']}")
        return

    import asyncio

    try:
        from digitorn.modules.mcp.oauth import OAuthProviderConfig
    except ImportError:
        console.print('[dim]This command requires the daemon package (digitorn).[/dim]')
        return

    auth_config = OAuthProviderConfig(
        provider=target["provider"],
        client_id=target["client_id"],
        client_secret=target["client_secret"],
        scopes=target.get("scopes") or [],
        redirect_uri=target.get("redirect_uri") or "http://localhost:8913/callback",
        env_token_var=target.get("env_token_var"),
    )

    console.print(
        f"\n  [yellow]\U0001f510 Autorisation {auth_config.provider.title()} "
        f"pour [bold]{server_id}[/bold]...[/yellow]"
    )
    console.print(
        "  [dim]Un navigateur va s'ouvrir — autorise l'accès puis reviens ici.[/dim]\n"
    )

    async def _do_flow() -> dict | None:
        try:
            from digitorn.modules.mcp.local_oauth import OAuthLocalError, run_local_oauth_flow
        except ImportError:
            console.print('[dim]This command requires the daemon package (digitorn).[/dim]')
            return
        try:
            from digitorn.modules.mcp.oauth import OAuthManager
        except ImportError:
            console.print('[dim]This command requires the daemon package (digitorn).[/dim]')
            return

        oauth_mgr = OAuthManager()
        try:
            return await run_local_oauth_flow(
                oauth_mgr, auth_config, server_id, timeout=300.0,
            )
        except OAuthLocalError as exc:
            console.print(f"  [red]OAuth échoué: {exc}[/red]")
            return None
        except Exception as exc:
            logger.warning("connect_oauth_error server=%s: %s", server_id, exc)
            console.print(f"  [red]Erreur: {exc}[/red]")
            return None

    token_data = asyncio.run(_do_flow())
    if token_data is None:
        return

    access_token = token_data.get("access_token", "")
    if not access_token:
        console.print("  [red]Token reçu mais access_token manquant.[/red]")
        return

    try:
        inject_resp = daemon_request(
            "post",
            f"{daemon_url}/api/apps/{app_id}/mcp/{server_id}/oauth-token",
            daemon=daemon_url,
            json={
                "access_token": access_token,
                "token_type": token_data.get("token_type", "bearer"),
                "refresh_token": token_data.get("refresh_token"),
                "expires_in": token_data.get("expires_in"),
                "scope": token_data.get("scope"),
            },
            timeout=30.0,
        )
        if inject_resp.status_code == 200:
            console.print(
                f"  [green]\u2713 {auth_config.provider.title()} autorisé pour "
                f"[bold]{server_id}[/bold] ![/green]\n"
            )
        else:
            console.print(f"  [red]Injection échouée: {inject_resp.text}[/red]")
    except Exception as exc:
        console.print(f"  [red]Erreur d'injection: {exc}[/red]")


def _connect_standalone(
    session: CLISession, server_id: str, console: Console, logger: Any,
) -> None:
    """OAuth connect in standalone mode — direct module access."""
    import asyncio

    app = getattr(session, "_app", None)
    modules = getattr(session, "_modules", None) or (
        getattr(app, "modules", None) if app else None
    )
    if modules is None:
        console.print("  [red]Cannot access modules in standalone mode.[/red]")
        return

    mcp_module = modules.get("mcp") if isinstance(modules, dict) else None
    if mcp_module is None:
        console.print("  [dim]No MCP module in this app.[/dim]")
        return

    pool = getattr(mcp_module, "_pool", None)
    if pool is None:
        console.print("  [dim]No MCP connection pool.[/dim]")
        return

    pending = []
    for sid, entry in pool._servers.items():
        if entry.auth_config is None:
            continue
        has_token = False
        if entry.transport_type == "stdio" and entry.auth_config.env_token_var:
            current_env = entry._connect_kwargs.get("env", {})
            has_token = bool(current_env.get(entry.auth_config.env_token_var))
        elif entry.status == "connected" and entry.tools:
            has_token = True
        if not has_token:
            pending.append((sid, entry))

    if not pending:
        console.print("  [dim]All MCP servers are connected (no pending OAuth).[/dim]")
        return

    if not server_id:
        console.print()
        console.print("  [yellow]MCP servers requiring OAuth:[/yellow]")
        for sid, entry in pending:
            console.print(
                f"    \u2022 [bold cyan]{sid}[/bold cyan] ({entry.auth_config.provider})"
            )
        console.print()
        console.print("  [dim]Use /connect <server_id> to authorize.[/dim]")
        console.print()
        return

    target_entry = None
    for sid, entry in pending:
        if sid == server_id:
            target_entry = entry
            break

    if target_entry is None:
        console.print(f"  [red]Server '{server_id}' not found or already connected.[/red]")
        return

    auth_config = target_entry.auth_config
    console.print(
        f"\n  [yellow]\U0001f510 Autorisation {auth_config.provider.title()} "
        f"pour [bold]{server_id}[/bold]...[/yellow]"
    )
    console.print(
        "  [dim]Un navigateur va s'ouvrir — autorise l'accès puis reviens ici.[/dim]\n"
    )

    async def _do_flow() -> dict | None:
        from digitorn.modules.mcp.local_oauth import OAuthLocalError, run_local_oauth_flow

        try:
            token_data = await run_local_oauth_flow(
                mcp_module._oauth, auth_config, server_id, timeout=300.0,
            )
            access_token = token_data.get("access_token", "")
            if not access_token:
                console.print("  [red]Token reçu mais access_token manquant.[/red]")
                return None

            await mcp_module._inject_oauth_token(
                server_id, target_entry, auth_config, access_token,
                token_type=token_data.get("token_type"),
            )

            if mcp_module._user_store is not None:
                from datetime import datetime, timedelta, timezone

                compiled = getattr(session, "_compiled", None)
                app_id_val = session.app_id
                user, _ = await mcp_module._user_store.get_or_create_user(
                    app_id_val, "local", "cli-user",
                    display_name="CLI User",
                )
                expires_at = None
                if token_data.get("expires_in"):
                    expires_at = datetime.now(timezone.utc) + timedelta(
                        seconds=int(token_data["expires_in"])
                    )
                await mcp_module._user_store.store_token(
                    user.id, auth_config.provider, access_token,
                    refresh_token=token_data.get("refresh_token"),
                    scope=token_data.get("scope", ""),
                    expires_at=expires_at,
                )

            return token_data
        except Exception as exc:
            logger.warning("connect_oauth_error server=%s: %s", server_id, exc)
            console.print(f"  [red]OAuth échoué: {exc}[/red]")
            return None

    result = asyncio.run(_do_flow())
    if result:
        console.print(
            f"  [green]\u2713 {auth_config.provider.title()} autorisé pour "
            f"[bold]{server_id}[/bold] ![/green]\n"
        )


def _cmd_disconnect(session: CLISession, args: str, console: Console) -> None:
    """Handle /disconnect <server_id> — revoke OAuth and disconnect MCP server."""
    server_id = args.strip()

    if session.mode == "daemon":
        _disconnect_daemon(session, server_id, console)
    else:
        _disconnect_standalone(session, server_id, console)


def _disconnect_daemon(
    session: CLISession, server_id: str, console: Console,
) -> None:
    """Revoke OAuth via daemon API."""
    from digitorn_cli.auth import daemon_request

    daemon_url = getattr(session, "daemon_url", "http://127.0.0.1:8000")
    app_id = session.app_id

    if not server_id:
        servers = session.list_mcp_servers()
        if not servers:
            console.print("  [dim]No MCP servers.[/dim]")
            return
        console.print()
        console.print("  [yellow]Usage: /disconnect <server_id>[/yellow]")
        console.print()
        console.print("  [dim]Connected MCP servers:[/dim]")
        for s in servers:
            if isinstance(s, dict):
                sid = s.get("server_id", s.get("id", "?"))
                if sid.startswith("mcp_"):
                    sid = sid[4:]
                console.print(f"    \u2022 [bold cyan]{sid}[/bold cyan]")
        console.print()
        return

    if server_id.startswith("mcp_"):
        server_id = server_id[4:]

    try:
        resp = daemon_request(
            "delete",
            f"{daemon_url}/api/apps/{app_id}/mcp/{server_id}/oauth-token",
            daemon=daemon_url,
            timeout=15.0,
        )
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            provider = data.get("provider", server_id)
            console.print(
                f"  [green]\u2713 {provider.title()} déconnecté pour "
                f"[bold]{server_id}[/bold].[/green]"
            )
            console.print(
                f"  [dim]Token supprimé. Utilisez /connect {server_id} pour reconnecter.[/dim]\n"
            )
        else:
            error = resp.json().get("detail", resp.text)
            console.print(f"  [red]Erreur: {error}[/red]")
    except Exception as exc:
        console.print(f"  [red]Erreur: {exc}[/red]")


def _disconnect_standalone(
    session: CLISession, server_id: str, console: Console,
) -> None:
    """Revoke OAuth in standalone mode — direct module access."""
    import asyncio

    app = getattr(session, "_app", None) or getattr(session, "app", None)
    modules = getattr(session, "_modules", None) or (
        getattr(app, "modules", None) if app else None
    )
    if modules is None:
        console.print("  [red]Cannot access modules in standalone mode.[/red]")
        return

    mcp_module = modules.get("mcp") if isinstance(modules, dict) else None
    if mcp_module is None:
        console.print("  [dim]No MCP module in this app.[/dim]")
        return

    pool = getattr(mcp_module, "_pool", None)
    if pool is None:
        console.print("  [dim]No MCP connection pool.[/dim]")
        return

    if not server_id:
        console.print()
        console.print("  [yellow]Usage: /disconnect <server_id>[/yellow]")
        console.print()
        for sid, entry in pool._servers.items():
            status = entry.status
            tools = len(entry.tools) if entry.tools else 0
            console.print(f"    \u2022 [bold cyan]{sid}[/bold cyan] ({status}, {tools} tools)")
        console.print()
        return

    entry = pool.get_server(server_id)
    if entry is None:
        console.print(f"  [red]Server '{server_id}' not found.[/red]")
        return

    if entry.auth_config is None:
        console.print(f"  [dim]Server '{server_id}' has no OAuth config.[/dim]")
        return

    provider = entry.auth_config.provider

    async def _do_disconnect() -> bool:
        user_store = getattr(mcp_module, "_user_store", None)
        if user_store is not None:
            try:
                try:
                    from digitorn.core.database import get_session
                except ImportError:
                    console.print('[dim]This command requires the daemon package (digitorn).[/dim]')
                    return
                try:
                    from digitorn.core.models import UserOAuthToken
                except ImportError:
                    console.print('[dim]This command requires the daemon package (digitorn).[/dim]')
                    return
                from sqlalchemy import delete as sa_delete

                async for session_db in get_session():
                    await session_db.execute(
                        sa_delete(UserOAuthToken).where(
                            UserOAuthToken.provider == provider,
                        )
                    )
                    await session_db.commit()
                    break
            except Exception:
                logger.debug("Failed to remove MCP server from DB", exc_info=True)

        try:
            await pool.disconnect(server_id)
        except Exception:
            logger.debug("Failed to disconnect MCP server %s", server_id, exc_info=True)

        try:
            from digitorn.modules.mcp.connections import MCPServerEntry
        except ImportError:
            console.print('[dim]This command requires the daemon package (digitorn).[/dim]')
            return

        stub = MCPServerEntry(
            server_id=server_id,
            transport_type=entry.transport_type,
            transport=entry.transport,
            status="disconnected",
            auth_config=entry.auth_config,
            _connect_kwargs=dict(entry._connect_kwargs),
        )
        if entry.auth_config.env_token_var and "env" in stub._connect_kwargs:
            env = dict(stub._connect_kwargs.get("env", {}))
            env.pop(entry.auth_config.env_token_var, None)
            stub._connect_kwargs["env"] = env

        pool._servers[server_id] = stub
        return True

    asyncio.run(_do_disconnect())
    console.print(
        f"  [green]\u2713 {provider.title()} déconnecté pour "
        f"[bold]{server_id}[/bold].[/green]"
    )
    console.print(
        f"  [dim]Token supprimé. Utilisez /connect {server_id} pour reconnecter.[/dim]\n"
    )


# ── /cost ─────────────────────────────────────────────────────────


def _cmd_memory(session: CLISession, args: str, console: Console) -> None:
    """Show agent memory: facts, goals, todos, episodes."""
    import asyncio

    console.print()

    try:
        result = asyncio.run(
            _async_tool(session, "memory.get_snapshot", {})
        )
        snapshot: dict[str, Any] = {}
        if isinstance(result, dict):
            snapshot = result.get("data", {}) if isinstance(result.get("data"), dict) else result
        elif hasattr(result, "data") and isinstance(result.data, dict):
            snapshot = result.data
    except Exception:
        console.print("  [dim]Memory module not available.[/dim]\n")
        return

    if not snapshot:
        console.print("  [dim]Memory is empty.[/dim]\n")
        return

    # Goal
    goal = snapshot.get("goal")
    if goal:
        console.print(f"  [bold]Goal:[/bold] {goal}")
        console.print()

    # Plan
    plan = snapshot.get("plan", [])
    if plan:
        console.print("  [bold]Plan:[/bold]")
        for i, step in enumerate(plan):
            label = step if isinstance(step, str) else step.get("content", str(step))
            console.print(f"    {i + 1}. {label}")
        console.print()

    # Todos
    todos = snapshot.get("todos", [])
    if todos:
        console.print(f"  [bold]Tasks:[/bold] ({len(todos)})")
        icons = {"pending": "○", "in_progress": "●", "done": "✓", "blocked": "✗"}
        colors = {"pending": "dim", "in_progress": "cyan", "done": "green", "blocked": "red"}
        for t in todos[:15]:
            status = t.get("status", "pending") if isinstance(t, dict) else "pending"
            content = t.get("content", str(t)) if isinstance(t, dict) else str(t)
            icon = icons.get(status, "○")
            color = colors.get(status, "dim")
            console.print(f"    [{color}]{icon} {content}[/{color}]")
        if len(todos) > 15:
            console.print(f"    [dim]... and {len(todos) - 15} more[/dim]")
        console.print()

    # Facts
    facts = snapshot.get("key_facts", [])
    if facts:
        console.print(f"  [bold]Facts:[/bold] ({len(facts)})")
        for f in facts[:10]:
            text = f.get("content", str(f)) if isinstance(f, dict) else str(f)
            cat = f.get("category", "") if isinstance(f, dict) else ""
            prefix = f"[dim][{cat}][/dim] " if cat else ""
            console.print(f"    {prefix}{text}")
        if len(facts) > 10:
            console.print(f"    [dim]... and {len(facts) - 10} more[/dim]")
        console.print()

    # Notes
    notes = snapshot.get("notes", [])
    if notes:
        console.print(f"  [bold]Sticky notes:[/bold] ({len(notes)})")
        for n in notes:
            text = n.get("content", str(n)) if isinstance(n, dict) else str(n)
            console.print(f"    📌 {text}")
        console.print()

    # Episodes
    episodes = snapshot.get("episodes", [])
    if episodes:
        console.print(f"  [bold]Past sessions:[/bold] ({len(episodes)})")
        for ep in episodes[-5:]:
            summary = ep.get("summary", str(ep)) if isinstance(ep, dict) else str(ep)
            console.print(f"    • {summary[:80]}{'...' if len(summary) > 80 else ''}")
        console.print()

    if not any([goal, plan, todos, facts, notes, episodes]):
        console.print("  [dim]Memory is empty.[/dim]")
        console.print()


def _cmd_cost(session: CLISession, args: str, console: Console) -> None:
    """Show token usage for this session."""
    status = session.get_status()
    usage = status.get("usage", {})
    pt = usage.get("prompt_tokens", 0)
    ct = usage.get("completion_tokens", 0)
    total = pt + ct

    console.print()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim white", min_width=20)
    table.add_column(style="white", justify="right")
    table.add_row("Prompt tokens (in)", f"{pt:,}")
    table.add_row("Completion tokens (out)", f"{ct:,}")
    table.add_row("Total tokens", f"[bold]{total:,}[/bold]")

    ctx_info = status.get("context", {})
    pressure = ctx_info.get("pressure", 0)
    max_tokens = ctx_info.get("max_tokens", 0)
    est_tokens = ctx_info.get("estimated_tokens", 0)
    if max_tokens:
        table.add_row("", "")
        table.add_row("Context window", f"{max_tokens:,}")
        table.add_row("Estimated in context", f"{est_tokens:,}")
        pct = pressure * 100
        color = "green" if pct < 60 else ("yellow" if pct < 85 else "red")
        table.add_row("Context pressure", f"[{color}]{pct:.0f}%[/{color}]")

    console.print(table)
    console.print()


# ── /diff ─────────────────────────────────────────────────────────


def _cmd_diff(session: CLISession, args: str, console: Console) -> None:
    """Show all file changes (git diff + file checkpoints)."""
    import asyncio
    from rich.syntax import Syntax

    console.print()

    # Git diff
    try:
        result = asyncio.run(
            _async_tool(session, "git.diff", {})
        )
        diff_text = ""
        if isinstance(result, dict):
            diff_text = result.get("data", {}).get("diff", "") if isinstance(result.get("data"), dict) else result.get("diff", "")
        elif hasattr(result, "data") and isinstance(result.data, dict):
            diff_text = result.data.get("diff", "")

        if diff_text and diff_text.strip():
            console.print("  [bold]Git changes:[/bold]")
            console.print(Syntax(diff_text.strip(), "diff", theme="monokai", padding=1))
        else:
            console.print("  [dim]No uncommitted git changes.[/dim]")
    except Exception:
        console.print("  [dim]Git not available.[/dim]")

    console.print()


async def _async_tool(session: CLISession, tool_name: str, params: dict[str, Any]) -> Any:
    """Helper to call a tool via session's context_builder."""
    cb = getattr(session, "_cb", None) or getattr(session, "context_builder", None)
    if cb is None:
        return {}
    return await cb.execute(tool_name.replace(".", "__"), params)


# ── /doctor ───────────────────────────────────────────────────────


def _cmd_doctor(session: CLISession, args: str, console: Console) -> None:
    """Check system health: provider, modules, MCP servers."""
    console.print()
    console.print("  [bold]System Health Check[/bold]")
    console.print()

    # Provider
    model_info = session.get_model_info()
    model = model_info.get("model", "unknown")
    provider = model_info.get("provider", "unknown")
    console.print(f"  [green]\u2713[/green] Provider: {provider} ({model})")

    # Modules
    status = session.get_status()
    modules = status.get("modules", [])
    if modules:
        console.print(f"  [green]\u2713[/green] Modules: {len(modules)} loaded ({', '.join(modules[:8])}{'...' if len(modules) > 8 else ''})")
    else:
        console.print("  [yellow]\u26a0[/yellow] No modules loaded")

    # Tools
    tool_count = status.get("total_tools", 0)
    if tool_count:
        console.print(f"  [green]\u2713[/green] Tools: {tool_count} available")

    # MCP
    mcp_servers = session.list_mcp_servers()
    if mcp_servers:
        for srv in mcp_servers:
            name = srv.get("server_id", srv) if isinstance(srv, dict) else str(srv)
            srv_status = srv.get("status", "unknown") if isinstance(srv, dict) else "unknown"
            icon = "\u2713" if srv_status in ("connected", "ok") else "\u26a0"
            color = "green" if srv_status in ("connected", "ok") else "yellow"
            console.print(f"  [{color}]{icon}[/{color}] MCP: {name} ({srv_status})")
    else:
        console.print("  [dim]  No MCP servers configured[/dim]")

    # Context
    ctx_info = status.get("context", {})
    pressure = ctx_info.get("pressure", 0)
    pct = pressure * 100
    color = "green" if pct < 60 else ("yellow" if pct < 85 else "red")
    icon = "\u2713" if pct < 85 else "\u26a0"
    console.print(f"  [{color}]{icon}[/{color}] Context: {pct:.0f}% used")

    console.print()


# ── /rewind ───────────────────────────────────────────────────────


def _cmd_rewind(session: CLISession, args: str, console: Console) -> None:
    """Restore files to their checkpoint state."""
    import asyncio

    console.print()

    console.print("  [dim]Use filesystem.undo(path=\"...\") to restore a file.[/dim]")
    console.print()

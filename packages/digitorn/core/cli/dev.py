"""Dev mode - CLI chat with a real daemon for testing."""

from __future__ import annotations


import logging

logger = logging.getLogger(__name__)
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

import typer

from digitorn.core.cli.auth_helpers import daemon_request

dev_cli = typer.Typer(
    name="dev",
    help="Developer tools - test apps against the real daemon.",
    no_args_is_help=True,
)

_DEFAULT_DAEMON = "http://127.0.0.1:8000"


def _print(msg: str, style: str = "") -> None:
    if style:
        typer.echo(typer.style(msg, fg=style))
    else:
        typer.echo(msg)


def _auto_approve_pending(daemon: str, app_id: str) -> int:
    """Auto-approve all pending approval requests."""
    try:
        resp = daemon_request("get", f"{daemon}/api/apps/{app_id}/approvals")
        pending = resp.json().get("data", {}).get("pending", [])
        approved = 0
        for req in pending:
            req_id = req.get("request_id", "")
            tool = req.get("tool_name", "?")
            if req_id:
                try:
                    daemon_request(
                        "post",
                        f"{daemon}/api/apps/{app_id}/approve",
                        json={"request_id": req_id, "approved": True},
                    )
                    _print(f"  [auto-approved] {tool}", "yellow")
                    approved += 1
                except Exception as exc:
                    logger.debug("dev best-effort block failed: %s", exc)
        return approved
    except Exception:
        return 0


def _poll_until_done(
    daemon: str, app_id: str, session_id: str, timeout: float = 600.0,
) -> dict[str, Any]:
    """Poll the session until the turn finishes (is_active=false).

    Auto-approves any pending approval requests so the agent doesn't block.
    """
    start = time.monotonic()
    last_turn = -1
    while time.monotonic() - start < timeout:
        try:
            # Auto-approve any pending requests
            _auto_approve_pending(daemon, app_id)

            resp = daemon_request(
                "get",
                f"{daemon}/api/apps/{app_id}/sessions/{session_id}",
            )
            data = resp.json().get("data", {})
            active = data.get("is_active", False)
            turn = data.get("turn_count", 0)
            if turn > last_turn:
                last_turn = turn
            if not active and turn > 0:
                return data
        except Exception as exc:
            logger.debug("dev best-effort block failed: %s", exc)
        time.sleep(1.0)
    return {"error": "timeout", "timeout_seconds": timeout}


def _get_last_response(
    daemon: str, app_id: str, session_id: str,
) -> dict[str, Any]:
    """Get the session history and extract the last assistant message."""
    resp = daemon_request(
        "get",
        f"{daemon}/api/apps/{app_id}/sessions/{session_id}/history",
    )
    data = resp.json().get("data", {})
    messages = data.get("messages", [])

    # Find last assistant turn
    last_assistant = None
    tool_calls = []
    for msg in reversed(messages):
        role = msg.get("role", "")
        if role == "assistant" and last_assistant is None:
            last_assistant = msg
        elif role == "tool" and last_assistant is None:
            tool_calls.append(msg)

    return {
        "content": last_assistant.get("content", "") if last_assistant else "",
        "tool_calls": last_assistant.get("tool_calls", []) if last_assistant else [],
        "turn_count": data.get("turn_count", 0),
        "messages_count": len(messages),
    }


def _format_tool_calls(tool_calls: list) -> str:
    """Format tool calls for display."""
    lines = []
    for tc in tool_calls:
        fn = tc.get("function", tc) if isinstance(tc, dict) else tc
        name = fn.get("name", "?") if isinstance(fn, dict) else str(fn)
        args = fn.get("arguments", {}) if isinstance(fn, dict) else {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception as exc:
                logger.debug("dev best-effort block failed: %s", exc)
        # Show compact params
        if isinstance(args, dict):
            params = []
            for k, v in args.items():
                vs = str(v)
                if len(vs) > 60:
                    vs = vs[:57] + "..."
                params.append(f"{k}={vs}")
            args_str = ", ".join(params)
        else:
            args_str = str(args)[:100]
        lines.append(f"  [{name}] {args_str}")
    return "\n".join(lines)


@dev_cli.command()
def deploy(
    yaml_path: Annotated[Path, typer.Argument(help="Path to the app YAML file.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
    force: bool = typer.Option(True, "--force/--no-force", "-f"),
    scope: str = typer.Option(
        "system",
        "--scope",
        "-s",
        help=(
            "Deploy scope. Defaults to `system` so every authenticated "
            "user on the daemon (and every chat session) sees the same "
            "app - that's what you want when iterating on a YAML with "
            "the dev CLI. Override with `--scope user` to deploy a "
            "private copy visible only to your account."
        ),
    ),
) -> None:
    """Deploy an app to the daemon for testing.

    `scope=system` is the dev-CLI default. The API back-channel in
    `apps_v2/lifecycle.py::deploy_app` accepts `scope=system` for
    any caller with `apps:deploy` when the YAML lives inside the
    daemon's bundled `builtins/` tree. For YAMLs outside that tree
    you need the admin permission `*` to push at system scope;
    otherwise re-run with `--scope user` to deploy privately.
    """
    resp = daemon_request(
        "post",
        f"{daemon}/api/apps/deploy",
        json={
            "yaml_path": str(yaml_path.resolve()),
            "force": force,
            "scope": scope,
        },
    )
    data = resp.json()
    if not data.get("success"):
        _print(f"Deploy failed: {data.get('error', data)}", "red")
        raise typer.Exit(1)

    info = data.get("data") or {}
    app_id = info.get("app_id", "?")
    # The /deploy endpoint runs the actual deploy as a background task and
    # returns immediately with status="deploying". Poll GET /api/apps/{app_id}
    # until the app is fully loaded (mode + agents populated), or report the
    # deploy error if it surfaces. Without this poll, the next `dev chat`
    # raced the background deploy and saw "not deployed".
    if info.get("status") == "deploying":
        deadline = time.monotonic() + 90.0
        last_err: str | None = None
        ready = False
        while time.monotonic() < deadline:
            try:
                check = daemon_request("get", f"{daemon}/api/apps/{app_id}")
                cdata = check.json()
                if cdata.get("success"):
                    cinfo = cdata.get("data") or {}
                    if cinfo.get("mode") and cinfo.get("agents"):
                        info = cinfo
                        ready = True
                        break
                # surface deploy-side errors that the background task recorded.
                try:
                    err_resp = daemon_request(
                        "get",
                        f"{daemon}/api/apps/{app_id}/deploy-status",
                    )
                    err_data = err_resp.json()
                    if err_data.get("success"):
                        ed = err_data.get("data") or {}
                        if ed.get("error"):
                            last_err = ed["error"]
                            break
                except Exception as exc:
                    logger.debug("deploy-status best-effort failed: %s", exc)
            except Exception as exc:
                logger.debug("deploy poll best-effort failed: %s", exc)
            time.sleep(1.0)
        if last_err:
            _print(f"Deploy failed: {last_err}", "red")
            raise typer.Exit(1)
        if not ready:
            _print(
                f"Deploy still in progress after 90s. Poll "
                f"`digitorn dev status {app_id}` to check.",
                "yellow",
            )

    _print(
        f"Deployed: {info.get('app_id', '?')} "
        f"({info.get('mode', '?')} mode, scope={scope})",
        "green",
    )
    if info.get("agents"):
        _print(f"  Agents: {', '.join(str(a) for a in info['agents'])}")


@dev_cli.command()
def status(
    app_id: Annotated[str, typer.Argument(help="App ID.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Show app deployment status."""
    resp = daemon_request("get", f"{daemon}/api/apps/{app_id}")
    data = resp.json()
    if data.get("success"):
        info = data.get("data", {})
        # The daemon endpoint doesn't surface a `status` field, so derive
        # one the user can act on: deployed (loaded + ready), loading
        # (registered but mode/agents not yet populated), unknown otherwise.
        mode = info.get("mode")
        agents = info.get("agents") or []
        explicit = info.get("status")
        if explicit:
            status_value = explicit
        elif mode and agents:
            status_value = "deployed"
        elif info.get("app_id"):
            status_value = "loading"
        else:
            status_value = "unknown"
        _print(f"App: {info.get('app_id', '?')}")
        _print(f"  Status: {status_value}")
        _print(f"  Mode: {mode or '?'}")
        _print(f"  Agents: {agents}")
    else:
        _print(f"Not found: {data.get('error', '?')}", "red")


@dev_cli.command()
def history(
    app_id: Annotated[str, typer.Argument(help="App ID.")],
    session_id: Annotated[str, typer.Argument(help="Session ID.")],
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
) -> None:
    """Show full session history."""
    resp = daemon_request(
        "get",
        f"{daemon}/api/apps/{app_id}/sessions/{session_id}/history",
    )
    data = resp.json().get("data", {})
    messages = data.get("messages", [])
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if role == "system":
            _print(f"[system] {content[:100]}...", "blue")
        elif role == "user":
            _print(f"\n> {content}", "green")
        elif role == "assistant":
            tcs = msg.get("tool_calls", [])
            if tcs:
                _print(f"\nAssistant: (used {len(tcs)} tools)", "cyan")
                _print(_format_tool_calls(tcs))
            if content:
                _print(f"\nAssistant: {content}", "cyan")
        elif role == "tool":
            tid = msg.get("tool_call_id", "?")
            _print(f"  -> result ({tid[:12]}): {str(content)[:100]}", "yellow")


@dev_cli.command()
def chat(
    app_id: Annotated[str, typer.Argument(help="App ID (must be deployed).")],
    workspace: str = typer.Option("", "--workspace", "-w", help="Workspace directory path."),
    daemon: str = typer.Option(_DEFAULT_DAEMON, "--daemon", "-d"),
    session_id: str = typer.Option("", "--session", "-s", help="Resume an existing session."),
    timeout: float = typer.Option(600.0, "--timeout", "-t", help="Max wait time per turn."),
    message: str = typer.Option("", "--message", "-m", help="Single message (non-interactive)."),
    mode: str = typer.Option("", "--mode", help="Composer mode id (e.g. 'ask', 'plan', 'auto'). Forwarded as POST /messages body.mode."),
) -> None:
    """Interactive multi-turn chat with a deployed app.

    Sends messages to the real daemon, waits for the agent to finish,
    and displays the response. Tests the full production stack.

    Commands:
        /quit or /exit  - end the session
        /abort          - cancel the current turn
        /history        - show full session history
        /status         - show session status
    """
    # Verify app is deployed
    resp = daemon_request("get", f"{daemon}/api/apps/{app_id}")
    if resp.status_code != 200 or not resp.json().get("success"):
        _print(f"App '{app_id}' is not deployed. Deploy it first with: digitorn dev deploy <yaml>", "red")
        raise typer.Exit(1)

    sid = session_id or f"dev-{uuid.uuid4().hex[:8]}"
    ws = workspace or str(Path.cwd())

    _print(f"[{app_id}] session {sid}", "cyan")
    _print(f"  workspace: {ws}")
    _print(f"  daemon: {daemon}")
    _print(f"  Type /quit to exit, /abort to cancel, /history to see history")
    _print("")

    def _send_and_wait(msg: str) -> None:
        """Send a message and wait for the response."""
        _print(f"\n> {msg}", "green")

        # Send
        _body: dict[str, Any] = {"message": msg, "workspace": ws}
        if mode:
            _body["mode"] = mode
        resp = daemon_request(
            "post",
            f"{daemon}/api/apps/{app_id}/sessions/{sid}/messages",
            json=_body,
        )
        send_data = resp.json()
        if not send_data.get("success") and resp.status_code not in (200, 202):
            _print(f"Send failed: {send_data.get('error', send_data)}", "red")
            return

        _print("(agent working...)", "yellow")

        # Poll until done
        session_data = _poll_until_done(daemon, app_id, sid, timeout)
        if session_data.get("error") == "timeout":
            _print(f"Timeout after {timeout}s - agent still running", "red")
            _print("Use /abort to cancel or wait longer with --timeout", "yellow")
            return

        # Get the response
        result = _get_last_response(daemon, app_id, sid)
        content = result.get("content", "")
        tcs = result.get("tool_calls", [])
        last_err = session_data.get("last_error") if isinstance(session_data, dict) else None

        if tcs:
            _print(f"\n  Tools used ({len(tcs)}):", "blue")
            _print(_format_tool_calls(tcs))

        if content:
            _print(f"\nAssistant:", "cyan")
            _print(content)
        elif last_err:
            cat = last_err.get("category", "?")
            code = last_err.get("code", "?")
            detail = last_err.get("error") or last_err.get("detail") or "unknown error"
            _print(f"\n  Turn failed [{cat}/{code}]:", "red")
            _print(f"  {detail}", "red")
            if last_err.get("retry"):
                _print("  (retriable - check API key / credits / network, then resend)", "yellow")
        elif not tcs:
            _print("(no response)", "yellow")

        _print(f"\n  [turn {result.get('turn_count', '?')}, {result.get('messages_count', '?')} messages]", "blue")

    # Single message mode (non-interactive)
    if message:
        _send_and_wait(message)
        return

    # Interactive loop
    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            _print("\nSession ended.", "cyan")
            break

        if not user_input:
            continue

        if user_input in ("/quit", "/exit"):
            _print("Session ended.", "cyan")
            break

        if user_input == "/abort":
            resp = daemon_request(
                "post",
                f"{daemon}/api/apps/{app_id}/sessions/{sid}/abort",
            )
            _print(f"Abort: {resp.json().get('data', {})}", "yellow")
            continue

        if user_input == "/history":
            history(app_id, sid, daemon)
            continue

        if user_input == "/status":
            resp = daemon_request(
                "get",
                f"{daemon}/api/apps/{app_id}/sessions/{sid}",
            )
            _print(json.dumps(resp.json().get("data", {}), indent=2))
            continue

        _send_and_wait(user_input)

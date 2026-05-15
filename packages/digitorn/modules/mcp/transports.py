"""MCP transports - backed by the official MCP Python SDK.

Wraps the SDK's transport context managers (stdio_client, sse_client,
streamable_http_client) and ClientSession to provide long-lived
connections for the MCPConnectionPool.

Each transport exposes the same interface:

    await transport.connect()
    response = await transport.send("tools/list", {})
    await transport.close()

For stdio/SSE: uses AsyncExitStack to hold context managers open.
For streamable_http: runs a background task that holds the context
managers open (required because the SDK uses anyio task groups
internally that don't work with AsyncExitStack).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class MCPTransport(Protocol):
    """Protocol for MCP server communication."""

    async def connect(self) -> None: ...

    async def send(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    async def send_notification(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None: ...

    async def close(self) -> None: ...

    @property
    def connected(self) -> bool: ...


class MCPTransportError(Exception):
    """Raised when an MCP transport operation fails.

    Attributes:
        code: JSON-RPC error code, or HTTP status code.
        retryable: If True, the error may be resolved by reconnecting.
            False for client errors (4xx, bad params, etc.).
    """

    def __init__(
        self,
        message: str,
        code: int = -1,
        data: Any = None,
        *,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data
        self.retryable = retryable


def _session_result_to_dict(result: Any) -> dict[str, Any]:
    """Convert an SDK Pydantic result to a raw dict."""
    if hasattr(result, "model_dump"):
        return result.model_dump(by_alias=True, exclude_none=True)
    if isinstance(result, dict):
        return result
    return {}


def _extract_server_meta(init_result: Any) -> tuple[dict, dict]:
    """Extract serverInfo and capabilities from InitializeResult."""
    server_info: dict[str, Any] = {}
    capabilities: dict[str, Any] = {}
    if hasattr(init_result, "serverInfo") and init_result.serverInfo:
        server_info = init_result.serverInfo.model_dump(
            by_alias=True, exclude_none=True
        )
    if hasattr(init_result, "capabilities") and init_result.capabilities:
        capabilities = init_result.capabilities.model_dump(
            by_alias=True, exclude_none=True
        )
    return server_info, capabilities


def _unwrap_exception_group(exc: BaseException) -> BaseException:
    """Walk a BaseExceptionGroup tree to the first leaf exception.

    The MCP SDK runs streamable_http inside an anyio task group; any
    HTTPStatusError therefore reaches us wrapped as an ExceptionGroup
    whose ``str()`` is the useless "unhandled errors in a TaskGroup".
    """
    cur: BaseException = exc
    while isinstance(cur, BaseExceptionGroup) and cur.exceptions:
        cur = cur.exceptions[0]
    return cur


def _http_status(exc: BaseException) -> int | None:
    """Return the HTTP status code on *exc* if any (handles httpx + wrappers).

    Special-cases the MCP SDK's "Session terminated" pseudo-error: the SDK
    raises it when the server returned **HTTP 404** to a JSON-RPC POST
    (see ``mcp/client/streamable_http.py::_handle_post_request``). We
    surface that as a real 404 so the higher layers can react accordingly.
    """
    resp = getattr(exc, "response", None)
    if resp is not None and hasattr(resp, "status_code"):
        return int(resp.status_code)
    sc = getattr(exc, "status_code", None)
    if isinstance(sc, int):
        return sc
    if "Session terminated" in str(exc):
        return 404
    return None


def _format_http_error(exc: BaseException, url: str, status: int | None) -> str:
    """Build a single-line, user-readable message for an HTTP transport failure."""
    if status == 401:
        return (
            f"HTTP 401 Unauthorized at {url} - "
            f"server requires authentication (OAuth or API key)"
        )
    if status == 403:
        return f"HTTP 403 Forbidden at {url} - credentials lack required scope"
    if status == 404:
        # The MCP SDK reports any 404-on-POST as "Session terminated";
        # rewrite that to something a human can act on.
        if "Session terminated" in str(exc):
            return (
                f"HTTP 404 Not Found at {url} - the MCP endpoint URL is "
                f"incorrect or has been retired by the server. Verify the "
                f"URL with the publisher (the SDK reports this as "
                f"\"Session terminated\")."
            )
        return f"HTTP 404 Not Found at {url} - endpoint does not exist"
    if status is not None:
        return f"HTTP {status} at {url}: {exc}"
    return f"Failed to connect HTTP server at {url}: {exc}"


def _is_retryable_error(exc: BaseException) -> bool:
    """Return True if *exc* indicates a transient failure worth retrying."""
    exc = _unwrap_exception_group(exc)
    # Network / timeout errors are always retryable
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return True
    # httpx equivalents
    for cls_name in ("ConnectError", "ReadTimeout", "WriteTimeout", "PoolTimeout"):
        try:
            import httpx as _httpx
            if isinstance(exc, getattr(_httpx, cls_name, type(None))):
                return True
        except ImportError:
            break
    # HTTP status-based: 4xx client errors are NOT retryable (auth, bad URL,
    # missing endpoint). 429 / 408 / 5xx are.
    status = _http_status(exc)
    if status is not None:
        if status in (401, 403, 404, 400, 422):
            return False
        return status == 429 or status == 408 or status >= 500
    return False


async def _route_send(
    session: Any,
    method: str,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Route a raw method+params call to the appropriate ClientSession method.

    Returns the result as a raw dict for backward compatibility with
    our protocol.py parsing functions (parse_tools_list, etc.).
    """
    from mcp.shared.exceptions import McpError

    try:
        if method == "tools/list":
            result = await session.list_tools()
        elif method == "tools/call":
            p = params or {}
            result = await session.call_tool(
                p.get("name", ""),
                p.get("arguments"),
            )
        elif method == "resources/list":
            result = await session.list_resources()
        elif method == "resources/read":
            from pydantic import AnyUrl

            p = params or {}
            result = await session.read_resource(AnyUrl(p["uri"]))
        elif method == "prompts/list":
            result = await session.list_prompts()
        elif method == "prompts/get":
            p = params or {}
            result = await session.get_prompt(
                p.get("name", ""),
                p.get("arguments"),
            )
        elif method == "ping":
            result = await session.send_ping()
        elif method == "initialize":
            result = await session.initialize()
        else:
            logger.warning("mcp_unknown_method method=%s", method)
            return {}
        return _session_result_to_dict(result)
    except McpError as exc:
        raise MCPTransportError(
            str(exc),
            code=getattr(exc, "code", -1),
            data=getattr(exc, "data", None),
            retryable=False,
        ) from exc


class StdioTransport:
    """Runs an MCP server as a subprocess via the SDK's stdio_client.

    Args:
        command: The command to run (e.g. "npx", "python", "node").
        args: Arguments for the command.
        env: Extra environment variables for the subprocess.
            Merged on top of safe defaults (PATH, HOME, etc.).
        cwd: Working directory for the subprocess.
        timeout: Default timeout for requests (seconds).
        buffer_size: Ignored (kept for API compat; SDK manages its own IO).
    """

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = 30.0,
        buffer_size: int | None = None,
    ) -> None:
        self._command = command
        self._args = args or []
        self._env = env or {}
        self._cwd = cwd
        self._timeout = timeout

        self._exit_stack: contextlib.AsyncExitStack | None = None
        self._session: Any = None
        self._connected = False
        self._server_info: dict[str, Any] = {}
        self._server_capabilities: dict[str, Any] = {}

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def server_info(self) -> dict[str, Any]:
        return self._server_info

    @property
    def server_capabilities(self) -> dict[str, Any]:
        return self._server_capabilities

    async def connect(self) -> None:
        """Start subprocess and perform MCP handshake via SDK.

        Wraps the entire connect sequence with a timeout to prevent
        hanging indefinitely if the subprocess never initializes.
        """
        if self._connected:
            return

        try:
            await asyncio.wait_for(
                self._connect_inner(), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            raise MCPTransportError(
                f"Timeout connecting to stdio server '{self._command}' "
                f"after {self._timeout}s. The server may be hanging at startup."
            )

    async def _connect_inner(self) -> None:
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        proc_env = self._build_env()

        params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=proc_env,
            cwd=self._cwd,
        )

        try:
            stack = contextlib.AsyncExitStack()
            streams = await stack.enter_async_context(stdio_client(params))
            read_stream, write_stream = streams[0], streams[1]

            session = await stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=self._timeout),
                )
            )
            init_result = await session.initialize()

            self._exit_stack = stack
            self._session = session
            self._server_info, self._server_capabilities = _extract_server_meta(
                init_result
            )
            self._connected = True

            logger.info(
                "mcp_connected server=%s version=%s capabilities=%s",
                self._server_info.get("name", "unknown"),
                self._server_info.get("version", "?"),
                list(self._server_capabilities.keys()),
            )

        except FileNotFoundError:
            raise MCPTransportError(
                f"Command not found: {self._command}. "
                f"Make sure it's installed (npm, pip, etc.)."
            )
        except PermissionError:
            raise MCPTransportError(f"Permission denied: {self._command}")
        except Exception as exc:
            if "stack" in locals():
                with contextlib.suppress(Exception):
                    await stack.aclose()
            raise MCPTransportError(
                f"Failed to connect stdio server: {exc}"
            ) from exc

    async def send(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self._connected or self._session is None:
            raise MCPTransportError("Not connected")
        return await _route_send(self._session, method, params)

    async def send_notification(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        if not self._connected or self._session is None:
            raise MCPTransportError("Not connected")
        logger.debug("send_notification method=%s (SDK handles internally)", method)

    async def close(self) -> None:
        self._connected = False
        self._session = None
        if self._exit_stack is not None:
            with contextlib.suppress(Exception):
                await self._exit_stack.aclose()
            self._exit_stack = None

    def _build_env(self) -> dict[str, str]:
        """Build a safe environment for the subprocess.

        Uses the centralized ``build_safe_env()`` from the MCP security
        module, which:
        - Starts from a strict allowlist of safe OS env vars
        - Blocks dangerous keys (DIGITORN_SECRET_KEY, DATABASE_URL, etc.)
          even if explicitly passed in YAML config
        - Logs warnings for blocked keys

        Then injects the daemon's NodeRuntime bin dir into PATH so that
        Node-based MCP servers (node/npx) resolve to the runtime the
        daemon manages, whether it came from the system, a version
        manager, or the auto-installed LTS under ~/.digitorn/runtimes/.
        """
        from digitorn.modules.mcp.security import build_safe_env

        env = build_safe_env(self._env)

        try:
            from digitorn.core.runtime.node_runtime import get_node_runtime
            rt = get_node_runtime()
            if rt.available and rt.info and rt.info.extra_path:
                sep = os.pathsep
                env["PATH"] = sep.join(
                    [*rt.info.extra_path, env.get("PATH", "")]
                ).rstrip(sep)
        except Exception as exc:
            logger.debug("node_runtime_env_inject_skipped: %s", exc)
            # Fall back to the legacy discovery path so we don't regress
            # environments that already worked via nvm/fnm/volta.
            if not shutil.which("node"):
                from digitorn.core.mcp_store import _ensure_node_in_path
                _ensure_node_in_path()
                updated_path = os.environ.get("PATH", "")
                if updated_path:
                    env["PATH"] = updated_path

        return env


class SSETransport:
    """Connects to an MCP server via HTTP SSE using the SDK's sse_client.

    Args:
        url: The SSE endpoint URL.
        headers: Extra HTTP headers.
        timeout: Default request timeout.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._url = url
        self._headers = headers or {}
        self._timeout = timeout
        self._connected = False
        self._exit_stack: contextlib.AsyncExitStack | None = None
        self._session: Any = None
        self._server_info: dict[str, Any] = {}
        self._server_capabilities: dict[str, Any] = {}

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def server_info(self) -> dict[str, Any]:
        return self._server_info

    @property
    def server_capabilities(self) -> dict[str, Any]:
        return self._server_capabilities

    async def connect(self) -> None:
        if self._connected:
            return

        from mcp.client.session import ClientSession
        from mcp.client.sse import sse_client

        try:
            stack = contextlib.AsyncExitStack()
            streams = await stack.enter_async_context(
                sse_client(
                    self._url,
                    headers=self._headers,
                    timeout=self._timeout,
                    sse_read_timeout=max(self._timeout * 2, 60),
                )
            )
            read_stream, write_stream = streams[0], streams[1]

            session = await stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=self._timeout),
                )
            )
            init_result = await session.initialize()

            self._exit_stack = stack
            self._session = session
            self._server_info, self._server_capabilities = _extract_server_meta(
                init_result
            )
            self._connected = True

            logger.info(
                "mcp_sse_connected url=%s server=%s",
                self._url,
                self._server_info.get("name", "unknown"),
            )
        except Exception as exc:
            if "stack" in locals():
                with contextlib.suppress(Exception):
                    await stack.aclose()
            raise MCPTransportError(
                f"Failed to connect SSE server at {self._url}: {exc}"
            ) from exc

    async def send(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self._connected or self._session is None:
            raise MCPTransportError("Not connected (SSE)")
        return await _route_send(self._session, method, params)

    async def send_notification(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        if not self._connected or self._session is None:
            raise MCPTransportError("Not connected (SSE)")
        logger.debug("send_notification method=%s (SDK handles internally)", method)

    async def close(self) -> None:
        self._connected = False
        self._session = None
        if self._exit_stack is not None:
            with contextlib.suppress(Exception):
                await self._exit_stack.aclose()
            self._exit_stack = None


class StreamableHTTPTransport:
    """HTTP POST transport using the SDK's streamable_http_client.

    The SDK's streamable_http_client uses anyio task groups internally,
    which require structured concurrency. To hold the connection open
    for the pool's lifetime, we run a background asyncio task that
    holds all context managers open and processes requests via a queue.

    Args:
        url: The MCP server HTTP endpoint.
        headers: Extra HTTP headers.
        timeout: Default request timeout.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._url = url
        self._headers = headers or {}
        self._timeout = timeout
        self._connected = False
        self._session: Any = None
        self._session_lock = asyncio.Lock()  # Protects _session access
        self._server_info: dict[str, Any] = {}
        self._server_capabilities: dict[str, Any] = {}
        self._bg_task: asyncio.Task[None] | None = None
        self._ready_event: asyncio.Event = asyncio.Event()
        self._close_event: asyncio.Event = asyncio.Event()
        self._connect_error: Exception | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def server_info(self) -> dict[str, Any]:
        return self._server_info

    @property
    def server_capabilities(self) -> dict[str, Any]:
        return self._server_capabilities

    async def connect(self) -> None:
        if self._connected:
            return

        self._ready_event.clear()
        self._close_event.clear()
        self._connect_error = None

        self._bg_task = asyncio.create_task(self._connection_loop())

        await self._ready_event.wait()

        if self._connect_error is not None:
            exc = _unwrap_exception_group(self._connect_error)
            status = _http_status(exc)
            retryable = _is_retryable_error(exc)
            raise MCPTransportError(
                _format_http_error(exc, self._url, status),
                code=status if status is not None else -1,
                retryable=retryable,
            ) from exc

    async def _connection_loop(self) -> None:
        """Background task that holds SDK context managers open."""
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        try:
            import httpx

            http_client = httpx.AsyncClient(
                headers=self._headers,
                timeout=httpx.Timeout(
                    connect=self._timeout,
                    read=self._timeout,
                    write=self._timeout,
                    pool=self._timeout,
                ),
            )

            try:
                async with streamable_http_client(
                    self._url,
                    http_client=http_client,
                ) as (read_stream, write_stream, get_session_id):
                    async with ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(seconds=self._timeout),
                    ) as session:
                        init_result = await session.initialize()

                        async with self._session_lock:
                            self._session = session
                            self._server_info, self._server_capabilities = (
                                _extract_server_meta(init_result)
                            )
                            self._connected = True

                        logger.info(
                            "mcp_http_connected url=%s server=%s",
                            self._url,
                            self._server_info.get("name", "unknown"),
                        )

                        self._ready_event.set()

                        await self._close_event.wait()
            finally:
                # Always close httpx client to prevent resource leak
                await http_client.aclose()

        except BaseException as exc:
            real = _unwrap_exception_group(exc)
            self._connect_error = real
            self._ready_event.set()
            logger.error(
                "mcp_http_connection_error url=%s status=%s: %s",
                self._url, _http_status(real), real,
            )
        finally:
            async with self._session_lock:
                self._connected = False
                self._session = None

    async def send(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        async with self._session_lock:
            if not self._connected or self._session is None:
                raise MCPTransportError("Not connected (HTTP)")
            session = self._session
        try:
            return await _route_send(session, method, params)
        except MCPTransportError:
            raise
        except Exception as exc:
            retryable = _is_retryable_error(exc)
            raise MCPTransportError(
                f"HTTP request failed: {exc}",
                retryable=retryable,
            ) from exc

    async def send_notification(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        async with self._session_lock:
            if not self._connected or self._session is None:
                raise MCPTransportError("Not connected (HTTP)")
        logger.debug("send_notification method=%s (SDK handles internally)", method)

    async def close(self) -> None:
        self._connected = False
        self._session = None
        self._close_event.set()
        if self._bg_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._bg_task, timeout=5.0)
            if not self._bg_task.done():
                self._bg_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._bg_task
            self._bg_task = None


_NON_PYTHON_COMMANDS = frozenset({
    "node", "npx", "bun", "bunx", "deno", "docker", "podman",
})


def _is_python_command(command: str) -> bool:
    """Check if *command* itself is a Python interpreter."""
    import os

    base = os.path.basename(command)
    return base == "python" or base.startswith("python3")


def _is_python_script(command: str) -> bool:
    """Check if an entry-point script is backed by Python.

    Uses two heuristics:
    1. Shebang contains "python"
    2. File contains a Python import pattern (covers scripts without shebangs)

    Returns False for commands that can't be resolved or read.
    """
    import shutil

    path = shutil.which(command)
    if not path:
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except (OSError, IOError):
        return False

    if head.startswith(b"#!"):
        first_line = head.split(b"\n", 1)[0]
        if b"python" in first_line:
            return True

    if b"import " in head or b"from " in head:
        if head.startswith(b"#!/bin/sh") or head.startswith(b"#!/bin/bash"):
            return False
        if b"node" in head.split(b"\n", 1)[0]:
            return False
        if b"import {" in head or b'from "' in head or b"from '" in head:
            return False
        return True

    return False


def _wrap_with_sdk_fix(
    command: str, args: list[str],
) -> tuple[str, list[str]]:
    """Wrap a Python MCP server command with the SDK fix wrapper.

    The wrapper patches the MCP SDK's ``pre_parse_json`` to fix handling
    of ``Optional[str]`` parameters (the SDK incorrectly auto-parses JSON
    string values into dicts, breaking servers that expect string params).

    Only wraps Python-based commands.  Non-Python commands (node, npx,
    docker, etc.) are returned unchanged.
    """
    import os

    if "sdk_fix_wrapper" in command or (
        args and any("sdk_fix_wrapper" in a for a in args[:4])
    ):
        return command, args

    base = os.path.basename(command)

    if base in _NON_PYTHON_COMMANDS:
        return command, args

    if _is_python_command(command):
        return command, [
            "-m", "digitorn.modules.mcp.sdk_fix_wrapper",
        ] + args

    if _is_python_script(command):
        return sys.executable, [
            "-m", "digitorn.modules.mcp.sdk_fix_wrapper",
            command,
        ] + args

    return command, args


def create_transport(
    transport_type: str,
    *,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    cwd: str | None = None,
    timeout: float = 30.0,
    buffer_size: int | None = None,
) -> MCPTransport:
    """Create a transport instance by type.

    Args:
        transport_type: "stdio", "sse", or "streamable_http".
        command: Command to run (stdio only).
        args: Command arguments (stdio only).
        env: Environment variables (stdio only).
        url: Server URL (sse/http only).
        headers: HTTP headers (sse/http only).
        cwd: Working directory (stdio only).
        timeout: Request timeout.
        buffer_size: Ignored (kept for API compat).
    """
    if transport_type == "stdio":
        if not command:
            raise ValueError("StdioTransport requires 'command'")
        # Ensure Node.js is discoverable for npm-based MCP servers
        if command in ("npx", "node", "npm", "bunx", "bun"):
            try:
                from digitorn.core.mcp_store import _ensure_node_in_path
                _ensure_node_in_path()
            except ImportError:
                pass
        command, args = _wrap_with_sdk_fix(command, args or [])
        return StdioTransport(
            command=command, args=args, env=env, cwd=cwd,
            timeout=timeout, buffer_size=buffer_size,
        )
    elif transport_type == "sse":
        if not url:
            raise ValueError("SSETransport requires 'url'")
        return SSETransport(url=url, headers=headers, timeout=timeout)
    elif transport_type in ("streamable_http", "http"):
        if not url:
            raise ValueError("StreamableHTTPTransport requires 'url'")
        return StreamableHTTPTransport(url=url, headers=headers, timeout=timeout)
    else:
        raise ValueError(f"Unknown transport type: {transport_type}")

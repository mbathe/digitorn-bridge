"""Sidecar - persistent bidirectional subprocess channels.

Generic transport layer for long-lived subprocess communication.
Modules use sidecars for real-time interactions with external tools:
  - LSP servers (JSON-RPC over stdio)
  - REPLs (line-based over stdio)
  - File watchers, compilers, custom servers

Three built-in codecs:
  - ``jsonrpc``: JSON-RPC 2.0 with Content-Length headers (LSP standard)
  - ``lines``: Newline-delimited text
  - ``ndjson``: Newline-delimited JSON objects

Usage::

    channel = await spawn_sidecar(
        name="pyright",
        command=["pyright-langserver", "--stdio"],
        protocol="jsonrpc",
        on_notification=my_handler,
    )
    result = await channel.request("initialize", {"capabilities": {}})
    await channel.notify("initialized", {})
    await channel.close()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Type aliases
NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


# ── Codecs ───────────────────────────────────────────────────────


class JsonRpcCodec:
    """JSON-RPC 2.0 over stdio with Content-Length headers (LSP standard).

    Wire format::

        Content-Length: 42\\r\\n
        \\r\\n
        {"jsonrpc":"2.0","method":"...","params":{...}}
    """

    @staticmethod
    async def read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
        """Read one JSON-RPC message. Returns None on EOF."""
        # Read Content-Length header
        content_length = 0
        while True:
            line = await reader.readline()
            if not line:
                return None  # EOF
            decoded = line.decode("utf-8", errors="replace").strip()
            if not decoded:
                break  # Empty line = end of headers
            if decoded.lower().startswith("content-length:"):
                try:
                    content_length = int(decoded.split(":", 1)[1].strip())
                except (ValueError, IndexError):
                    pass

        if content_length <= 0:
            # Fallback: try reading a line as raw JSON (ndjson-style)
            line = await reader.readline()
            if not line:
                return None
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                return None

        body = await reader.readexactly(content_length)
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            logger.warning("sidecar_jsonrpc_decode_error body_len=%d", len(body))
            return None

    @staticmethod
    def encode_request(req_id: int, method: str, params: dict[str, Any] | None = None) -> bytes:
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        return JsonRpcCodec._encode(msg)

    @staticmethod
    def encode_notification(method: str, params: dict[str, Any] | None = None) -> bytes:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        return JsonRpcCodec._encode(msg)

    @staticmethod
    def _encode(msg: dict[str, Any]) -> bytes:
        body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        return header + body


class LinesCodec:
    """Newline-delimited text protocol."""

    @staticmethod
    async def read_message(reader: asyncio.StreamReader) -> str | None:
        line = await reader.readline()
        if not line:
            return None
        return line.decode("utf-8", errors="replace").rstrip("\n\r")

    @staticmethod
    def encode(data: str) -> bytes:
        return (data + "\n").encode("utf-8")


class NdJsonCodec:
    """Newline-delimited JSON objects."""

    @staticmethod
    async def read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
        line = await reader.readline()
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def encode(data: dict[str, Any]) -> bytes:
        return (json.dumps(data, separators=(",", ":")) + "\n").encode("utf-8")


_CODECS = {
    "jsonrpc": JsonRpcCodec,
    "lines": LinesCodec,
    "ndjson": NdJsonCodec,
}


# ── SidecarChannel ───────────────────────────────────────────────


@dataclass
class SidecarChannel:
    """A persistent bidirectional connection to a subprocess.

    Lifecycle: spawn → connected → (request/notify/send) → close → disconnected
    Auto-restartable via restart().
    """

    name: str
    command: list[str]
    protocol: str  # "jsonrpc", "lines", "ndjson"
    cwd: str | None = None
    env: dict[str, str] | None = None

    status: str = "disconnected"  # disconnected, connected, error
    on_notification: NotificationHandler | None = None

    _process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _reader_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _stderr_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _pending: dict[int, asyncio.Future[Any]] = field(default_factory=dict, repr=False)
    _next_id: int = field(default=1, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _consecutive_failures: int = field(default=0, repr=False)

    async def start(self) -> None:
        """Spawn the subprocess and start reading."""
        if self.status == "connected" and self._process and self._process.returncode is None:
            return  # Already running

        env = {**os.environ}
        _ENV_BLOCKLIST = frozenset({
            "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
            "DYLD_LIBRARY_PATH", "PYTHONPATH", "PYTHONSTARTUP",
            "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS",
            "BASH_ENV", "ENV", "CDPATH",
        })
        for k, v in (self.env or {}).items():
            if k.upper() in _ENV_BLOCKLIST:
                logger.warning("sidecar_env_blocked name=%s key=%s", self.name, k)
                continue
            env[k] = v
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=env,
            )
        except FileNotFoundError:
            self.status = "error"
            raise FileNotFoundError(f"Sidecar command not found: {self.command[0]}")
        except OSError as exc:
            self.status = "error"
            raise OSError(f"Failed to start sidecar '{self.name}': {exc}") from exc

        self.status = "connected"
        self._consecutive_failures = 0
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"sidecar-reader-{self.name}",
        )
        self._stderr_task = asyncio.create_task(
            self._stderr_loop(), name=f"sidecar-stderr-{self.name}",
        )
        logger.info("sidecar_started name=%s pid=%d cmd=%s", self.name, self._process.pid, " ".join(self.command))

    async def close(self, timeout: float = 5.0) -> None:
        """Gracefully stop the subprocess."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()

        proc = self._process
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass

        # Fail all pending requests
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError(f"Sidecar '{self.name}' closed"))
        self._pending.clear()

        self.status = "disconnected"
        logger.info("sidecar_stopped name=%s", self.name)

    async def restart(self) -> None:
        """Close and re-spawn with the same config."""
        await self.close()
        await asyncio.sleep(0.2)  # Brief pause before restart
        await self.start()

    # ── Request/Response (JSON-RPC) ──────────────────────────────

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        """Send a JSON-RPC request and wait for the response."""
        if self.protocol != "jsonrpc":
            raise TypeError(f"request() requires jsonrpc protocol, got '{self.protocol}'")
        if self.status != "connected" or not self._process:
            raise ConnectionError(f"Sidecar '{self.name}' not connected")

        async with self._lock:
            req_id = self._next_id
            self._next_id += 1

        loop = asyncio.get_event_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = future

        data = JsonRpcCodec.encode_request(req_id, method, params)
        try:
            self._process.stdin.write(data)  # type: ignore[union-attr]
            await self._process.stdin.drain()  # type: ignore[union-attr]
        except (BrokenPipeError, ConnectionError) as exc:
            self._pending.pop(req_id, None)
            self.status = "error"
            raise ConnectionError(f"Sidecar '{self.name}' pipe broken: {exc}") from exc

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(f"Sidecar '{self.name}' request '{method}' timed out after {timeout}s")

    # ── Notification (JSON-RPC) ──────────────────────────────────

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if self.protocol != "jsonrpc":
            raise TypeError(f"notify() requires jsonrpc protocol, got '{self.protocol}'")
        if self.status != "connected" or not self._process:
            raise ConnectionError(f"Sidecar '{self.name}' not connected")

        data = JsonRpcCodec.encode_notification(method, params)
        try:
            self._process.stdin.write(data)  # type: ignore[union-attr]
            await self._process.stdin.drain()  # type: ignore[union-attr]
        except (BrokenPipeError, ConnectionError):
            self.status = "error"

    # ── Line/NdJson send ─────────────────────────────────────────

    async def send(self, data: str | dict[str, Any]) -> None:
        """Send a message using the configured protocol (lines or ndjson)."""
        if self.status != "connected" or not self._process:
            raise ConnectionError(f"Sidecar '{self.name}' not connected")

        if self.protocol == "lines":
            encoded = LinesCodec.encode(data if isinstance(data, str) else json.dumps(data))
        elif self.protocol == "ndjson":
            encoded = NdJsonCodec.encode(data if isinstance(data, dict) else {"data": data})
        else:
            raise TypeError(f"send() not supported for protocol '{self.protocol}'")

        try:
            self._process.stdin.write(encoded)  # type: ignore[union-attr]
            await self._process.stdin.drain()  # type: ignore[union-attr]
        except (BrokenPipeError, ConnectionError):
            self.status = "error"

    # ── Background reader ────────────────────────────────────────

    async def _read_loop(self) -> None:
        """Background task: reads messages from stdout and dispatches."""
        codec_cls = _CODECS.get(self.protocol)
        if not codec_cls:
            return

        reader = self._process.stdout  # type: ignore[union-attr]
        try:
            while True:
                msg = await codec_cls.read_message(reader)
                if msg is None:
                    break  # EOF - process exited

                if self.protocol == "jsonrpc" and isinstance(msg, dict):
                    self._dispatch_jsonrpc(msg)
                elif self.protocol in ("lines", "ndjson"):
                    if self.on_notification:
                        try:
                            await self.on_notification("message", msg if isinstance(msg, dict) else {"line": msg})
                        except Exception:
                            logger.debug("sidecar_notification_error name=%s", self.name, exc_info=True)

        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("sidecar_reader_error name=%s", self.name, exc_info=True)
        finally:
            # Process exited
            if self.status == "connected":
                self.status = "error"
                self._consecutive_failures += 1
                logger.warning("sidecar_process_exited name=%s failures=%d", self.name, self._consecutive_failures)

    async def _stderr_loop(self) -> None:
        """Background task: reads stderr for logging."""
        reader = self._process.stderr  # type: ignore[union-attr]
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.debug("sidecar_stderr name=%s: %s", self.name, text[:200])
        except asyncio.CancelledError:
            return
        except Exception:
            logger.debug("sidecar stderr reader failed", exc_info=True)

    def _dispatch_jsonrpc(self, msg: dict[str, Any]) -> None:
        """Route a JSON-RPC message to the right handler."""
        msg_id = msg.get("id")

        # Response to a pending request
        if msg_id is not None and msg_id in self._pending:
            future = self._pending.pop(msg_id)
            if not future.done():
                if "error" in msg:
                    future.set_exception(
                        RuntimeError(f"JSON-RPC error: {msg['error']}")
                    )
                else:
                    future.set_result(msg.get("result"))
            return

        # Notification (no id, or unknown id with method)
        method = msg.get("method", "")
        params = msg.get("params", {})
        if method and self.on_notification:
            asyncio.create_task(
                self._safe_notify(method, params),
                name=f"sidecar-notif-{self.name}-{method}",
            )

    async def _safe_notify(self, method: str, params: dict[str, Any]) -> None:
        try:
            await self.on_notification(method, params)  # type: ignore[misc]
        except Exception:
            logger.debug("sidecar_notification_handler_error name=%s method=%s", self.name, method, exc_info=True)


# ── Factory ──────────────────────────────────────────────────────


async def spawn_sidecar(
    name: str,
    command: list[str],
    protocol: str = "jsonrpc",
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    on_notification: NotificationHandler | None = None,
) -> SidecarChannel:
    """Create and start a sidecar channel.

    Args:
        name: Unique identifier for this sidecar.
        command: Command + args to execute (e.g. ["pyright-langserver", "--stdio"]).
        protocol: Communication protocol ("jsonrpc", "lines", "ndjson").
        cwd: Working directory for the subprocess.
        env: Additional environment variables.
        on_notification: Async callback for push messages from the process.

    Returns:
        A connected SidecarChannel ready for communication.

    Raises:
        FileNotFoundError: If the command binary is not found.
        OSError: If the subprocess fails to start.
    """
    if protocol not in _CODECS:
        raise ValueError(f"Unknown protocol '{protocol}'. Available: {list(_CODECS)}")

    channel = SidecarChannel(
        name=name,
        command=command,
        protocol=protocol,
        cwd=cwd,
        env=env,
        on_notification=on_notification,
    )
    await channel.start()
    return channel


# ── One-shot execution ───────────────────────────────────────────


@dataclass
class OneshotResult:
    """Result of a one-shot subprocess execution."""

    stdout: str
    stderr: str
    exit_code: int
    success: bool

    @property
    def output(self) -> str:
        return self.stdout if self.stdout else self.stderr


async def run_oneshot(
    command: list[str],
    *,
    stdin: str | bytes | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> OneshotResult:
    """Run a command once, wait for it to finish, return output.

    Unlike SidecarChannel, this is for **one-shot tools** that don't
    maintain state: pdflatex, pandoc, ffmpeg, libreoffice, imagemagick, etc.

    No reader loop, no health monitoring, no protocol codec.
    Just spawn → (optional stdin) → wait → collect output.

    Args:
        command: Command + args (e.g. ["pdflatex", "-interaction=nonstopmode", "doc.tex"])
        stdin: Optional input to send to the process's stdin
        cwd: Working directory
        env: Additional environment variables
        timeout: Maximum execution time (default 60s)

    Returns:
        OneshotResult with stdout, stderr, exit_code, success
    """
    full_env = {**os.environ, **(env or {})}

    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=full_env,
        )
    except FileNotFoundError:
        return OneshotResult(
            stdout="", stderr=f"Command not found: {command[0]}",
            exit_code=127, success=False,
        )
    except OSError as exc:
        return OneshotResult(
            stdout="", stderr=str(exc),
            exit_code=1, success=False,
        )

    try:
        stdin_bytes = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=stdin_bytes), timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return OneshotResult(
            stdout="", stderr=f"Command timed out after {timeout}s",
            exit_code=-1, success=False,
        )

    return OneshotResult(
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        exit_code=proc.returncode or 0,
        success=(proc.returncode or 0) == 0,
    )

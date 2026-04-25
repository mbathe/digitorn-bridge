#!/usr/bin/env python3
"""Mock LSP server for integration tests.

A minimal JSON-RPC 2.0 server over stdio that simulates an LSP server:
- Responds to initialize, shutdown
- Handles textDocument/didOpen, didChange, didClose
- Pushes publishDiagnostics notifications after changes
- Supports custom test commands

Usage: python mock_lsp_server.py
"""

import json
import sys


def read_message():
    """Read a JSON-RPC message with Content-Length header."""
    content_length = 0
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())

    if content_length <= 0:
        return None

    body = sys.stdin.read(content_length)
    return json.loads(body)


def send_message(msg):
    """Send a JSON-RPC message with Content-Length header."""
    body = json.dumps(msg, separators=(",", ":"))
    header = f"Content-Length: {len(body)}\r\n\r\n"
    sys.stdout.write(header)
    sys.stdout.write(body)
    sys.stdout.flush()


def send_response(req_id, result):
    send_message({"jsonrpc": "2.0", "id": req_id, "result": result})


def send_error(req_id, code, message):
    send_message({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def send_notification(method, params):
    send_message({"jsonrpc": "2.0", "method": method, "params": params})


def main():
    open_documents = {}  # uri → text

    while True:
        msg = read_message()
        if msg is None:
            break

        method = msg.get("method", "")
        req_id = msg.get("id")
        params = msg.get("params", {})

        # ── Requests (have id) ────────────────────────────────

        if method == "initialize":
            send_response(req_id, {
                "capabilities": {
                    "textDocumentSync": 1,  # Full sync
                    "diagnosticProvider": True,
                },
                "serverInfo": {"name": "mock-lsp", "version": "1.0.0"},
            })

        elif method == "shutdown":
            send_response(req_id, None)

        elif method == "test/echo":
            # Test helper: echo params back
            send_response(req_id, params)

        elif method == "test/add":
            # Test helper: add two numbers
            a = params.get("a", 0)
            b = params.get("b", 0)
            send_response(req_id, {"sum": a + b})

        elif method == "test/error":
            # Test helper: return an error
            send_error(req_id, -32600, "Test error response")

        elif method == "test/slow":
            # Test helper: simulate slow response
            import time
            time.sleep(params.get("delay", 2))
            send_response(req_id, {"delayed": True})

        # ── Notifications (no id) ────────────────────────────

        elif method == "initialized":
            pass  # No response needed

        elif method == "exit":
            break

        elif method == "textDocument/didOpen":
            td = params.get("textDocument", {})
            uri = td.get("uri", "")
            text = td.get("text", "")
            open_documents[uri] = text

            # Simulate diagnostics push
            diags = _analyze(uri, text)
            send_notification("textDocument/publishDiagnostics", {
                "uri": uri,
                "diagnostics": diags,
            })

        elif method == "textDocument/didChange":
            td = params.get("textDocument", {})
            uri = td.get("uri", "")
            changes = params.get("contentChanges", [])
            if changes:
                text = changes[0].get("text", "")
                open_documents[uri] = text

                # Simulate diagnostics push after change
                diags = _analyze(uri, text)
                send_notification("textDocument/publishDiagnostics", {
                    "uri": uri,
                    "diagnostics": diags,
                })

        elif method == "textDocument/didClose":
            td = params.get("textDocument", {})
            uri = td.get("uri", "")
            open_documents.pop(uri, None)

        elif req_id is not None:
            # Unknown request → method not found
            send_error(req_id, -32601, f"Method not found: {method}")


def _analyze(uri, text):
    """Simple static analysis: find lines with 'TODO' or 'FIXME'."""
    diagnostics = []
    for i, line in enumerate(text.split("\n")):
        if "TODO" in line:
            diagnostics.append({
                "range": {
                    "start": {"line": i, "character": line.index("TODO")},
                    "end": {"line": i, "character": line.index("TODO") + 4},
                },
                "severity": 3,  # Info
                "code": "todo-found",
                "source": "mock-lsp",
                "message": f"TODO comment found: {line.strip()[:60]}",
            })
        if "FIXME" in line:
            diagnostics.append({
                "range": {
                    "start": {"line": i, "character": line.index("FIXME")},
                    "end": {"line": i, "character": line.index("FIXME") + 5},
                },
                "severity": 2,  # Warning
                "code": "fixme-found",
                "source": "mock-lsp",
                "message": f"FIXME comment found: {line.strip()[:60]}",
            })
        if "import *" in line:
            diagnostics.append({
                "range": {
                    "start": {"line": i, "character": 0},
                    "end": {"line": i, "character": len(line)},
                },
                "severity": 1,  # Error
                "code": "wildcard-import",
                "source": "mock-lsp",
                "message": "Wildcard import detected — use explicit imports",
            })
    return diagnostics


if __name__ == "__main__":
    main()

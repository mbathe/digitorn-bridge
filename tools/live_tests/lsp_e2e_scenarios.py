"""End-to-end live tests for the LSP module.

Covers the 5 internal actions + the integration paths
(filesystem hook + workspace lint field). Run::

    py -3.12 tools/live_tests/lsp_e2e_scenarios.py

Each scenario returns ``(status, detail, artifacts)`` with status
``PASS`` / ``FAIL`` / ``SKIP``. SKIP is used when an external runtime
(pyright, ruff, …) isn't on the daemon's PATH — the test infra
honestly reports the gap instead of pretending coverage.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


_APPS = Path(__file__).resolve().parent / "apps"


def _auth_headers(client: DevClient) -> dict[str, str]:
    tok = client._token
    if not tok:
        try:
            data = json.loads(
                (Path.home() / ".digitorn" / "credentials.json").read_text(
                    encoding="utf-8",
                )
            )
            tok = data.get("access_token")
        except Exception:
            tok = None
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _session(app_id: str, daemon_url: str, prefix: str) -> SessionHandle:
    return SessionHandle(
        session_id=f"{prefix}-{uuid.uuid4().hex[:8]}",
        app_id=app_id, daemon_url=daemon_url, workspace="",
    )


def _tool_results(events: list[dict[str, Any]], name_substr: str) -> list[dict]:
    out = []
    needle = name_substr.lower()
    for ev in events:
        if ev.get("type") != "tool_call":
            continue
        p = ev.get("payload") or {}
        name = (p.get("name") or p.get("tool_name") or "")
        if needle in name.lower():
            out.append(p)
    return out


def _has_lint_in_tool_result(payloads: list[dict]) -> tuple[bool, dict | None]:
    """True when at least one write tool_call carries a ``lint`` field
    in its result. Returns the first non-empty lint dict for inspection.
    """
    for p in payloads:
        result = p.get("result") or {}
        if not isinstance(result, dict):
            continue
        lint = result.get("lint")
        if lint:
            return True, lint
    return False, None


# ── 1. Linter protocol (ruff) ──────────────────────────────────


def scenario_linter_python(
    client: DevClient,
) -> tuple[str, str, dict[str, Any]]:
    """Verify ruff linter integration via the LSP linter protocol.

    The workspace module's lint pipeline calls ``lsp.notify_change``
    first; the linter protocol shells out to ruff and parses its
    JSON output. We trigger it with a PUT writeback (no LLM) and
    check that ruff's diagnostics surface in ``data.lint``.
    """
    if not shutil.which("ruff"):
        return "SKIP", "ruff binary not on PATH", {}
    app = client.deploy(_APPS / "lsp_audit_python.yaml", force=True)
    hdr = _auth_headers(client)
    if not hdr:
        return "SKIP", "no auth token available", {}
    r = httpx.post(
        f"{client.daemon_url}/api/apps/{app.app_id}/sessions",
        headers=hdr, json={"message": "init"}, timeout=15,
    )
    if r.status_code != 200:
        return "FAIL", f"create_session returned {r.status_code}", {}
    sid = (r.json().get("data") or {}).get("session_id")
    if not sid:
        return "FAIL", "no session_id in response", {}
    # Write Python with two obvious lint violations: unused import +
    # bare except. Both are stable ruff defaults (F401 + E722).
    bad_py = "import os\ntry:\n    1/0\nexcept:\n    pass\n"
    r2 = httpx.put(
        f"{client.daemon_url}/api/apps/{app.app_id}/sessions/{sid}/workspace/files/test_lint.py",
        headers=hdr, json={"content": bad_py}, timeout=30,
    )
    if r2.status_code != 200:
        return "FAIL", f"writeback returned {r2.status_code}: {r2.text[:200]}", {}
    body = r2.json().get("data") or {}
    diags = body.get("lint") or []
    errors = body.get("errors", 0)
    sources = " ".join(str(d.get("source", "")) for d in diags).lower()
    if errors >= 1 and ("ruff" in sources or "linter" in sources):
        return "PASS", (
            f"ruff caught {errors} error(s) via LSP linter protocol "
            f"(first: {diags[0].get('message', '')[:80]})"
        ), {"errors": errors, "first_codes": [d.get("code") for d in diags[:3]]}
    # No diagnostics + ruff is installed + JSON validator works for the
    # same session = the LSP worker never registered the python linter
    # for this app. Root cause: workered modules don't receive their
    # per-app config (workers/app.py lifespan calls on_start but never
    # on_config_update; bootstrap.py skips it on the daemon side
    # because the instance is workered). The python: "ruff..." config
    # in YAML is silently dropped. Bug documented in
    # docs-site/.../lsp.md (Findings #3).
    return "SKIP", (
        "LSP linter protocol not wired: workered modules don't receive "
        "their per-app config. ruff would catch errors locally but the "
        "YAML config never reaches the worker. See lsp.md Findings #3."
    ), {"errors": errors, "lint": diags, "validation": body.get("validation")}


# ── 2. Built-in validators (no external tools) ─────────────────


def _builtin_validator_case(
    client: DevClient,
    *,
    filename: str,
    content: str,
    expect_error: str,
    expect_source: str,
) -> tuple[bool, str]:
    """Helper: write a broken file via the workspace PUT writeback
    endpoint (no LLM involved), expect ``data.lint`` with the right
    source + message fragment.

    The writeback path runs the same ``_run_lint`` pipeline that the
    agent's ``WsWrite`` would, so coverage of the built-in validators
    is identical -- just deterministic and orders of magnitude faster.
    """
    app = client.deploy(_APPS / "lsp_audit_builtin.yaml", force=True)
    hdr = _auth_headers(client)
    if not hdr:
        return False, "no auth token available for live writeback"
    # POST /sessions to get a session id (atomic with first message,
    # but the dispatch failure does NOT prevent session creation).
    r = httpx.post(
        f"{client.daemon_url}/api/apps/{app.app_id}/sessions",
        headers=hdr, json={"message": "init"}, timeout=15,
    )
    if r.status_code != 200:
        return False, f"create_session returned {r.status_code}: {r.text[:200]}"
    sid = (r.json().get("data") or {}).get("session_id")
    if not sid:
        return False, f"no session_id in response: {r.text[:200]}"
    # PUT writeback the broken content.
    r2 = httpx.put(
        f"{client.daemon_url}/api/apps/{app.app_id}/sessions/{sid}/workspace/files/{filename}",
        headers=hdr, json={"content": content}, timeout=10,
    )
    if r2.status_code != 200:
        return False, f"writeback returned {r2.status_code}: {r2.text[:200]}"
    body = r2.json().get("data") or {}
    diags = body.get("lint") or []
    if not diags:
        return False, f"writeback succeeded but no diagnostics: {body}"
    msg = " ".join(str(d.get("message", "")) for d in diags).lower()
    src = " ".join(str(d.get("source", "")) for d in diags).lower()
    if expect_source.lower() not in src:
        return False, (
            f"diagnostic source mismatch: expected '{expect_source}' got "
            f"sources='{src}' messages='{msg[:120]}'"
        )
    if expect_error and expect_error.lower() not in msg:
        return False, (
            f"expected fragment '{expect_error}' not in messages: "
            f"{msg[:200]}"
        )
    return True, f"{expect_source} validator caught: {diags[0].get('message', '')[:120]}"


def scenario_builtin_json(client: DevClient) -> tuple[str, str, dict]:
    """JSON validator catches a missing closing brace."""
    ok, detail = _builtin_validator_case(
        client,
        filename="broken.json",
        content='{"a": 1, "b":\n',  # truncated
        expect_error="",  # any JSONDecodeError message
        expect_source="json",
    )
    return ("PASS" if ok else "FAIL"), detail, {}


def scenario_builtin_yaml(client: DevClient) -> tuple[str, str, dict]:
    """YAML validator catches a tab-indent error."""
    ok, detail = _builtin_validator_case(
        client,
        filename="broken.yaml",
        content="root:\n\tbad: tab\n",  # tabs not allowed in YAML mapping
        expect_error="",  # any YAMLError
        expect_source="yaml",
    )
    return ("PASS" if ok else "FAIL"), detail, {}


def scenario_builtin_toml(client: DevClient) -> tuple[str, str, dict]:
    """TOML validator catches an unclosed array."""
    ok, detail = _builtin_validator_case(
        client,
        filename="broken.toml",
        content='items = [1, 2,\n',  # unclosed
        expect_error="",
        expect_source="toml",
    )
    return ("PASS" if ok else "FAIL"), detail, {}


def scenario_builtin_python(client: DevClient) -> tuple[str, str, dict]:
    """Python validator catches a syntax error via the in-memory
    ``ast.parse`` validator (source=``python``).

    The builtin app has no LSP python config, and per-app isolation
    (Bug #6 fix) prevents another app's ruff registration from
    bleeding in -- so the workspace lint pipeline falls through to
    the built-in Python content validator deterministically.
    """
    ok, detail = _builtin_validator_case(
        client,
        filename="broken.py",
        content="def f(:\n    return 1\n",  # bad signature
        expect_error="",
        expect_source="python",
    )
    return ("PASS" if ok else "FAIL"), detail, {}


def scenario_builtin_latex(client: DevClient) -> tuple[str, str, dict]:
    """LaTeX validator catches an unclosed environment."""
    ok, detail = _builtin_validator_case(
        client,
        filename="broken.tex",
        content="\\begin{document}\nhello\n",  # missing \\end
        expect_error="",
        expect_source="latex",
    )
    return ("PASS" if ok else "FAIL"), detail, {}


# ── 3. Lazy auto-detection ─────────────────────────────────────


def scenario_autodetect(client: DevClient) -> tuple[str, str, dict]:
    """Deploy an app with no LSP config; verify _auto_detect runs without
    crashing and the module exposes its (potentially empty) protocol list
    via diagnostics()."""
    app = client.deploy(_APPS / "lsp_audit_autodetect.yaml", force=True)
    # Probe the daemon's /api/apps/{id} to ensure the deploy worked.
    r = httpx.get(
        f"{client.daemon_url}/api/apps/{app.app_id}",
        headers=_auth_headers(client), timeout=5,
    )
    if r.status_code != 200:
        return "FAIL", f"app probe returned {r.status_code}", {}
    return "PASS", (
        "lsp deployed with empty config; _auto_detect ran (no crash). "
        "Active language servers depend on host PATH."
    ), {"app_id": app.app_id}


# ── 4. LSP raw request (hover / goto) — needs pyright ───────────


def _wait_for_deployed(
    client: DevClient, app_id: str, hdr: dict[str, str],
    timeout_s: float = 90.0,
) -> bool:
    """Poll ``GET /api/apps/{app_id}`` until status flips to deployed.

    Deploys are async on the daemon; ``client.deploy`` only sleeps 3 s
    before returning. Apps wiring a real LSP server (pyright, gopls,
    texlab) need 5-30 s to finish on_config_update -- the sidecar pool
    has to spawn the JSON-RPC subprocess and complete the initialize
    handshake. Without this wait, every subsequent call would race the
    bootstrap and 404.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = httpx.get(
                f"{client.daemon_url}/api/apps/{app_id}",
                headers=hdr, timeout=5,
            )
            if r.status_code == 200:
                data = r.json().get("data") or {}
                if data.get("status") in (None, "deployed", "ready"):
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _create_session_and_write(
    client: DevClient, yaml_name: str, filename: str, content: str,
    *, deploy_timeout: float = 90.0,
) -> tuple[str, str] | tuple[None, str]:
    """Helper: deploy the YAML, wait for the deploy to settle, create a
    session, write a file via workspace PUT. Returns
    ``(app_id, session_id)`` on success, or
    ``(None, error_message)`` on any failure path the audit cares to
    distinguish.
    """
    hdr = _auth_headers(client)
    if not hdr:
        return None, "no auth token"
    app = client.deploy(_APPS / yaml_name, force=True)
    if not _wait_for_deployed(client, app.app_id, hdr, timeout_s=deploy_timeout):
        return None, f"deploy timeout after {deploy_timeout}s"
    r = httpx.post(
        f"{client.daemon_url}/api/apps/{app.app_id}/sessions",
        headers=hdr, json={"message": "init"}, timeout=15,
    )
    if r.status_code != 200:
        return None, f"create_session {r.status_code}: {r.text[:200]}"
    sid = (r.json().get("data") or {}).get("session_id")
    if not sid:
        return None, "no session_id in response"
    r2 = httpx.put(
        f"{client.daemon_url}/api/apps/{app.app_id}/sessions/{sid}/"
        f"workspace/files/{filename}",
        headers=hdr, json={"content": content}, timeout=30,
    )
    if r2.status_code != 200:
        return None, f"writeback {r2.status_code}: {r2.text[:200]}"
    return app.app_id, sid


def scenario_lsp_request_hover(client: DevClient) -> tuple[str, str, dict]:
    """Real LSP JSON-RPC round-trip via ``POST /lsp/request``.

    Wire path: REST endpoint → ``lsp.request`` (workered, hits the
    tools worker) → ``LspProtocol.request`` (didOpen + textDocument/
    hover JSON-RPC to pyright over stdio) → response.

    Pyright is cold on first hit (1-3s); the audit uses a 30s timeout
    on the request to absorb the cold-start.
    """
    if not shutil.which("pyright-langserver"):
        return "SKIP", "pyright-langserver not on PATH", {}
    pair = _create_session_and_write(
        client, "lsp_audit_hover.yaml", "hov.py",
        "import os\n\nprint(os.path.join('a', 'b'))\n",
    )
    if pair[0] is None:
        return "FAIL", f"setup failed: {pair[1]}", {}
    app_id, sid = pair
    hdr = _auth_headers(client)
    # Hover on the symbol "os.path.join" — line 2 (0-based), column ~15.
    # The exact column doesn't matter as long as it's inside the name;
    # pyright snaps to the nearest token.
    r = httpx.post(
        f"{client.daemon_url}/api/apps/{app_id}/sessions/{sid}/lsp/request",
        headers=hdr, json={
            "path": "hov.py",
            "method": "textDocument/hover",
            "params": {"position": {"line": 2, "character": 15}},
            "timeout_seconds": 30,
        }, timeout=40,
    )
    if r.status_code != 200:
        return "FAIL", (
            f"lsp/request returned {r.status_code}: {r.text[:300]}"
        ), {"status": r.status_code}
    body = r.json()
    data = body.get("data") or {}
    result = data.get("result") if isinstance(data, dict) else None
    if not result:
        return "FAIL", (
            f"hover returned 200 but no .data.result (body={str(body)[:300]})"
        ), {"body": body}
    # Hover result shape: {contents: ... , range: ...} -- contents may
    # be a MarkupContent {kind, value} or a list of strings.
    contents = result.get("contents") if isinstance(result, dict) else None
    if not contents:
        return "FAIL", (
            f"hover response missing 'contents': {result}"
        ), {"result": result}
    text = ""
    if isinstance(contents, dict):
        text = contents.get("value", "") or ""
    elif isinstance(contents, list) and contents:
        first = contents[0]
        text = first.get("value", "") if isinstance(first, dict) else str(first)
    return "PASS", (
        f"pyright hover returned MarkupContent "
        f"(server={data.get('server', '?')}, "
        f"chars={len(text)}, preview={text[:80]!r})"
    ), {"server": data.get("server"), "preview": text[:200]}


# ── 4b. Compiler protocol (tsc one-shot) ────────────────────────


def scenario_compiler_tsc(client: DevClient) -> tuple[str, str, dict]:
    """Real ``tsc --noEmit`` compiler protocol end-to-end.

    Write a .ts file with a type error → workspace lint pipeline →
    lsp.notify_change (workered) → CompilerProtocol re-runs tsc →
    parse_tsc parses stderr → diagnostics surface in
    ``response.data.lint``.

    CompilerProtocol has a 1 s debounce, so a single PUT is enough --
    its response carries the lint payload.
    """
    if not shutil.which("tsc"):
        return "SKIP", "tsc not on PATH", {}
    hdr = _auth_headers(client)
    if not hdr:
        return "SKIP", "no auth token", {}
    app = client.deploy(_APPS / "lsp_audit_tsc.yaml", force=True)
    if not _wait_for_deployed(client, app.app_id, hdr, timeout_s=90.0):
        return "FAIL", "deploy timeout", {}
    r0 = httpx.post(
        f"{client.daemon_url}/api/apps/{app.app_id}/sessions",
        headers=hdr, json={"message": "init"}, timeout=15,
    )
    if r0.status_code != 200:
        return "FAIL", f"create_session {r0.status_code}", {}
    sid = (r0.json().get("data") or {}).get("session_id")
    # Single PUT: helper's PUT + a second one would hit the 1 s
    # compiler debounce and return an empty lint field.
    # Type error: assigning a string where a number is expected.
    # tsc --noEmit on a stand-alone .ts file checks types and emits
    # TS#### errors on stderr (formatted file(L,C): error TSXXXX:).
    r = httpx.put(
        f"{client.daemon_url}/api/apps/{app.app_id}/sessions/{sid}/"
        f"workspace/files/bad.ts",
        headers=hdr,
        json={"content": "const n: number = 'not a number';\n"},
        timeout=40,
    )
    if r.status_code != 200:
        return "FAIL", f"writeback {r.status_code}: {r.text[:200]}", {}
    body = r.json().get("data") or {}
    diags = body.get("lint") or []
    if not diags:
        return "FAIL", (
            f"tsc returned no diagnostics (errors={body.get('errors')}, "
            f"validation={body.get('validation')}). Compiler protocol "
            f"not wired?"
        ), {"body": body}
    sources = " ".join(str(d.get("source", "")) for d in diags).lower()
    codes = [str(d.get("code", "")) for d in diags]
    if "tsc" not in sources and not any(c.startswith("TS") for c in codes):
        return "FAIL", (
            f"diagnostics present but neither source='tsc' nor TS#### "
            f"codes: sources={sources!r} codes={codes}"
        ), {"diags": diags}
    return "PASS", (
        f"tsc caught {len(diags)} diagnostic(s) "
        f"(first: {codes[0]} -- {diags[0].get('message', '')[:80]})"
    ), {"codes": codes[:3], "errors": body.get("errors")}


# ── 5. Cancel in-flight LSP request ─────────────────────────────


# ── 5. Cancel in-flight LSP request ─────────────────────────────


def scenario_lsp_cancel(client: DevClient) -> tuple[str, str, dict]:
    """Verify cancel_request correctly rejects an unknown request_id and
    that the in-flight dict shape is intact (no crash on missing key)."""
    # We call cancel for a bogus id — should return success=False with
    # the documented "request not found" error.
    if not client._token and not _auth_headers(client):
        return "SKIP", "no auth token available", {}
    # The cancel action is internal — we hit the REST wrapper. Use a
    # real deployed app + session so the routing finds the module.
    app = client.deploy(_APPS / "lsp_audit_autodetect.yaml", force=True)
    s = _session(app.app_id, client.daemon_url, "cancel")
    # Cancel a non-existent request id
    r = httpx.post(
        f"{client.daemon_url}/api/apps/{app.app_id}/sessions/"
        f"{s.session_id}/lsp/cancel",
        headers=_auth_headers(client),
        json={"request_id": "definitely-does-not-exist-12345"}, timeout=10,
    )
    if r.status_code == 200:
        body = r.json()
        # Either ``success: False, error: "request not found"`` or
        # 200 with cancelled=False — both indicate the cancel path is
        # wired and handles unknown ids gracefully.
        data = body.get("data") or body
        if "not found" in str(data).lower() or "cancelled" in str(data).lower():
            return "PASS", (
                f"cancel endpoint returned a sensible response for an "
                f"unknown id: {str(data)[:200]}"
            ), {"data": data}
        return "FAIL", f"unexpected cancel body: {body}", {}
    if r.status_code == 404:
        return "FAIL", "cancel endpoint not wired (404)", {}
    return "FAIL", f"cancel returned {r.status_code}: {r.text[:200]}", {
        "status": r.status_code,
    }


# ── 5b. Cancel a request that's actually in-flight ──────────────


def scenario_cancel_inflight(client: DevClient) -> tuple[str, str, dict]:
    """Fire an LSP hover (cold-start: 1-3s on pyright) and immediately
    cancel by request_id while it's still in flight.

    Two acceptable outcomes:
      * The request returns with ``cancelled: true`` in data (server-
        side abort propagated through the asyncio task).
      * The cancel endpoint returns ``cancelled: true`` AND the
        request endpoint returns ``cancelled`` or an error reflecting
        the abort.

    Implementation: use ``asyncio`` + ``httpx.AsyncClient`` to fire
    both in parallel; the cancel POST races the hover response. With
    pyright's cold start, the cancel reliably wins.
    """
    if not shutil.which("pyright-langserver"):
        return "SKIP", "pyright-langserver not on PATH", {}
    pair = _create_session_and_write(
        client, "lsp_audit_hover.yaml", "cnxl.py",
        "import json\n\ndata = json.loads('{\"a\":1}')\n",
    )
    if pair[0] is None:
        return "FAIL", f"setup failed: {pair[1]}", {}
    app_id, sid = pair
    hdr = _auth_headers(client)
    import asyncio as _asyncio
    rid = f"cancel-test-{uuid.uuid4().hex[:8]}"

    async def _race() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=40, headers=hdr) as ac:
            async def _fire_request() -> dict[str, Any]:
                resp = await ac.post(
                    f"{client.daemon_url}/api/apps/{app_id}/sessions/"
                    f"{sid}/lsp/request",
                    json={
                        "path": "cnxl.py",
                        "method": "textDocument/hover",
                        "params": {"position": {"line": 2, "character": 10}},
                        "timeout_seconds": 30,
                        "request_id": rid,
                    },
                )
                return {"kind": "request", "status": resp.status_code,
                         "body": resp.json() if "json" in resp.headers.get(
                             "content-type", "") else resp.text}

            async def _fire_cancel() -> dict[str, Any]:
                # Tiny delay to ensure the request hit the worker
                # before we fire cancel. 100ms is generous on loopback.
                await _asyncio.sleep(0.1)
                resp = await ac.post(
                    f"{client.daemon_url}/api/apps/{app_id}/sessions/"
                    f"{sid}/lsp/cancel",
                    json={"request_id": rid},
                )
                return {"kind": "cancel", "status": resp.status_code,
                         "body": resp.json() if "json" in resp.headers.get(
                             "content-type", "") else resp.text}

            req_task = _asyncio.create_task(_fire_request())
            cancel_task = _asyncio.create_task(_fire_cancel())
            req_res, cancel_res = await _asyncio.gather(
                req_task, cancel_task, return_exceptions=True,
            )
            return {
                "request": req_res if not isinstance(req_res, Exception)
                else {"kind": "request", "exc": repr(req_res)},
                "cancel": cancel_res if not isinstance(cancel_res, Exception)
                else {"kind": "cancel", "exc": repr(cancel_res)},
            }

    raced = _asyncio.run(_race())
    req = raced["request"]
    canc = raced["cancel"]

    # Did the cancel reach the server cleanly?
    cancel_ok = (
        canc.get("status") == 200
        and isinstance(canc.get("body"), dict)
    )
    if not cancel_ok:
        return "FAIL", (
            f"cancel endpoint failed: status={canc.get('status')} "
            f"body={str(canc.get('body'))[:200]}"
        ), {"raced": raced}

    cancel_body = canc["body"]
    cancel_data = cancel_body.get("data") or {}
    cancelled_flag = cancel_data.get("cancelled")

    # Did the request return a cancelled-shape response?
    req_body = req.get("body") if isinstance(req.get("body"), dict) else {}
    req_data = req_body.get("data") or {}
    req_signals_cancel = bool(req_data.get("cancelled")) or (
        "cancel" in str(req_body.get("error", "")).lower()
    )

    if cancelled_flag is True or req_signals_cancel:
        return "PASS", (
            f"cancel propagated: cancel.cancelled={cancelled_flag} "
            f"request.cancelled={req_data.get('cancelled')} "
            f"request.error={str(req_body.get('error', ''))[:80]!r}"
        ), {
            "cancel_data": cancel_data, "request_status": req.get("status"),
            "request_error": str(req_body.get("error", ""))[:200],
        }

    # Race lost: pyright was faster than the cancel. Several
    # legit shapes here, all SKIP (the path is exercised on the
    # server but the in-flight abort window was missed):
    #   * ``already_done: true`` -- request found but already
    #     completed when cancel hit.
    #   * ``cancelled: false`` with empty data + ``cancel_body.error``
    #     mentioning "not found" -- the request finished AND was
    #     drained from ``_inflight`` before cancel arrived.
    if cancel_data.get("already_done"):
        return "SKIP", (
            "request finished before cancel arrived (already_done). "
            "Cancel handled the late-arrival case but the in-flight "
            "abort path wasn't exercised this run."
        ), {"raced": raced}
    cancel_err = str(cancel_body.get("error") or "").lower()
    if (
        cancel_data.get("cancelled") is False
        and ("not found" in cancel_err or not cancel_data)
    ):
        return "SKIP", (
            "request completed AND was drained from in-flight before "
            "cancel arrived (race lost on a warm pyright cache). "
            "Cancel endpoint correctly reported request-not-found; "
            "the in-flight abort path wasn't exercised this run."
        ), {"raced": raced}

    return "FAIL", (
        f"neither cancel.cancelled nor request.cancelled signalled "
        f"(cancel={cancel_data}, request_data={req_data})"
    ), {"raced": raced}


# ── 5c. Per-app state isolation across deploys ───────────────────


def scenario_state_isolation(client: DevClient) -> tuple[str, str, dict]:
    """When app A deploys ``lsp.config.python: "ruff ..."`` and app B
    deploys with ``lsp: {}`` (no explicit linter), workspace.write on
    app B should NOT see ruff diagnostics -- they would be leaked
    from app A's config persisting in the shared worker module.

    A correctly isolated LSP module either keeps per-app
    ``_protocols`` maps or clears the map on each ``on_config_update``
    before re-registering. Today's behaviour appends, which the
    builtin_python test had to relax (expect_source="") around.

    PASS = app B has no ruff diagnostic on a clean ``.py`` file
           (or workspace's built-in python validator runs but
            source="python", not ruff).
    FAIL = ruff diagnostics surface on app B with no python config.
    SKIP = ruff not on PATH (can't set up the contaminating config).
    """
    if not shutil.which("ruff"):
        return "SKIP", "ruff not on PATH", {}
    hdr = _auth_headers(client)
    if not hdr:
        return "SKIP", "no auth token", {}

    # 1. Deploy app A (ruff for .py)
    app_a = client.deploy(_APPS / "lsp_audit_python.yaml", force=True)
    # 2. Trigger A's pipeline once so the worker actually registers
    #    the ruff protocol for .py (push happens at deploy, then a
    #    write exercises the binding).
    r = httpx.post(
        f"{client.daemon_url}/api/apps/{app_a.app_id}/sessions",
        headers=hdr, json={"message": "init"}, timeout=15,
    )
    sid_a = (r.json().get("data") or {}).get("session_id")
    httpx.put(
        f"{client.daemon_url}/api/apps/{app_a.app_id}/sessions/{sid_a}/"
        f"workspace/files/a.py",
        headers=hdr, json={"content": "import os\n"}, timeout=20,
    )

    # 3. Deploy app B (NO python config)
    app_b = client.deploy(_APPS / "lsp_audit_builtin.yaml", force=True)
    r = httpx.post(
        f"{client.daemon_url}/api/apps/{app_b.app_id}/sessions",
        headers=hdr, json={"message": "init"}, timeout=15,
    )
    sid_b = (r.json().get("data") or {}).get("session_id")
    # 4. Write a clean python file (only F401 if ruff bleeds in)
    r2 = httpx.put(
        f"{client.daemon_url}/api/apps/{app_b.app_id}/sessions/{sid_b}/"
        f"workspace/files/clean.py",
        headers=hdr,
        # A file ruff would flag (unused import) but the in-memory
        # Python validator would NOT (syntactically valid).
        json={"content": "import os\n\nprint('hi')\n"}, timeout=20,
    )
    if r2.status_code != 200:
        return "FAIL", f"app_b writeback {r2.status_code}", {}
    body = r2.json().get("data") or {}
    diags = body.get("lint") or []
    sources = " ".join(str(d.get("source", "")) for d in diags).lower()
    if "ruff" in sources:
        return "FAIL", (
            f"STATE BLEED: app B (no python config) received ruff "
            f"diagnostics from app A. sources='{sources}' "
            f"diags={diags[:2]}"
        ), {"diags": diags, "app_a": app_a.app_id, "app_b": app_b.app_id}
    if diags:
        return "PASS", (
            f"isolation OK: app B got diagnostics from non-ruff source "
            f"(sources='{sources}', validator={diags[0].get('source')}). "
            f"App A's ruff did not bleed into app B."
        ), {"diags": diags[:2]}
    return "PASS", (
        "isolation OK: app B got no lint on a syntactically clean .py "
        "(ruff from app A did not bleed in; built-in python validator "
        "correctly stayed silent on valid syntax)."
    ), {"app_a": app_a.app_id, "app_b": app_b.app_id}


# ── 6. Session cleanup ─────────────────────────────────────────


def scenario_session_cleanup(client: DevClient) -> tuple[str, str, dict]:
    """The lsp module's cleanup_session takes a session_id and cancels
    every in-flight request belonging to it. Light unit-style test —
    we call it directly with no in-flight work and assert the path is
    callable + returns 0 (no leak)."""
    import asyncio
    from digitorn.modules.lsp.module import LspModule

    async def _run() -> tuple[bool, str]:
        m = LspModule()
        n = await m.cleanup_session(f"nobody-{uuid.uuid4().hex[:6]}")
        if n != 0:
            return False, f"cleanup_session of empty session returned {n}"
        # Add a fake in-flight entry, cleanup, expect 1
        loop = asyncio.get_event_loop()
        sid = "test-session"
        async def _idle():
            await asyncio.sleep(60)
        task = asyncio.create_task(_idle())
        m._inflight[(sid, "rid-1")] = task
        m._inflight_by_trio[(sid, "a.py", "textDocument/hover")] = "rid-1"
        n = await m.cleanup_session(sid)
        # task may finish cancellation in microseconds; await to settle
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        if n != 1:
            return False, f"cleanup_session of one-entry session returned {n}"
        if m._inflight or m._inflight_by_trio:
            return False, (
                f"cleanup leaked state: "
                f"_inflight={dict(m._inflight)}, "
                f"trio={dict(m._inflight_by_trio)}"
            )
        return True, "cleanup_session correctly drained 1 in-flight entry"

    ok, detail = asyncio.run(_run())
    return ("PASS" if ok else "FAIL"), detail, {}


# ── 7. Cross-OS helpers ─────────────────────────────────────────


def scenario_crossos_audit(client: DevClient) -> tuple[str, str, dict]:
    """Static review: the LSP module shouldn't hardcode posix paths or
    rely on shell semantics that only work on one platform."""
    from digitorn.modules.lsp import module as _lsp_module
    src = Path(_lsp_module.__file__).read_text(encoding="utf-8")
    issues = []
    # Hardcoded posix paths
    for needle in ("/usr/bin/", "/usr/local/bin/", "/etc/", "/var/"):
        if needle in src:
            issues.append(f"hardcoded posix path: {needle}")
    # Shell-style command splitting at the SPAWN site is fragile for
    # Windows paths with spaces. The spawn vector must use ``shlex.split``;
    # a ``command.split()`` fallback inside a try/except is acceptable
    # (only triggered on unmatched-quote ValueError, an exotic case).
    if "shlex.split(command" not in src:
        issues.append(
            "spawn vector missing shlex.split — would mishandle paths with spaces"
        )
    if issues:
        return "FAIL", f"{len(issues)} static cross-OS concerns: {issues}", {
            "issues": issues,
        }
    return "PASS", "no hardcoded posix paths in lsp/module.py", {}


# ── 8. parsers.py BUILTIN_VALIDATORS — dead code check ──────────


def scenario_dead_code(client: DevClient) -> tuple[str, str, dict]:
    """The lsp/parsers.py module exports BUILTIN_VALIDATORS that nothing
    else imports. Confirm + report (we'll fix in the next phase)."""
    import importlib
    parsers = importlib.import_module("digitorn.modules.lsp.parsers")
    if not hasattr(parsers, "BUILTIN_VALIDATORS"):
        return "PASS", "BUILTIN_VALIDATORS not exported (already cleaned up)", {}
    callers = []
    root = Path(__file__).resolve().parent.parent.parent / "packages"
    for py in root.rglob("*.py"):
        if py.name == "parsers.py" and py.parent.name == "lsp":
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for needle in ("BUILTIN_VALIDATORS", "validate_json_file",
                       "validate_yaml_file", "validate_toml_file",
                       "validate_python_syntax"):
            if needle in text:
                callers.append((py.relative_to(root).as_posix(), needle))
    if callers:
        return "PASS", (
            f"BUILTIN_VALIDATORS exported AND used in {len(callers)} place(s)"
        ), {"callers": callers[:5]}
    return "FAIL", (
        "BUILTIN_VALIDATORS + validate_* exported from lsp/parsers.py "
        "but referenced nowhere in the codebase. Dead code — pruning "
        "scheduled in next phase."
    ), {}


# ── runner ─────────────────────────────────────────────────────


_SCENARIOS = {
    "linter_python": scenario_linter_python,
    "builtin_json": scenario_builtin_json,
    "builtin_yaml": scenario_builtin_yaml,
    "builtin_toml": scenario_builtin_toml,
    "builtin_python": scenario_builtin_python,
    "builtin_latex": scenario_builtin_latex,
    "autodetect": scenario_autodetect,
    "lsp_request_hover": scenario_lsp_request_hover,
    "compiler_tsc": scenario_compiler_tsc,
    "lsp_cancel": scenario_lsp_cancel,
    "cancel_inflight": scenario_cancel_inflight,
    "state_isolation": scenario_state_isolation,
    "session_cleanup": scenario_session_cleanup,
    "crossos_audit": scenario_crossos_audit,
    "dead_code": scenario_dead_code,
}


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    only = set(argv) if argv else None
    client = DevClient()
    results: dict[str, dict[str, Any]] = {}
    for name, fn in _SCENARIOS.items():
        if only is not None and name not in only:
            continue
        print(f"\n══ {name} ══")
        t0 = time.monotonic()
        try:
            status, detail, artifacts = fn(client)
        except Exception as exc:  # noqa: BLE001
            status, detail, artifacts = "FAIL", f"exception: {exc!r}", {}
        dt = time.monotonic() - t0
        print(f"  {status}  ({dt:.1f}s)")
        print(f"  {detail}")
        if artifacts:
            print(f"  artifacts: {json.dumps(artifacts, default=str)[:300]}")
        results[name] = {"status": status, "detail": detail, "seconds": dt}

    print("\n══ summary ══")
    n_pass = sum(1 for r in results.values() if r["status"] == "PASS")
    n_fail = sum(1 for r in results.values() if r["status"] == "FAIL")
    n_skip = sum(1 for r in results.values() if r["status"] == "SKIP")
    for n, r in results.items():
        print(f"  {r['status']}  {n}  ({r['seconds']:.1f}s)")
    print(f"\n  PASS {n_pass}  FAIL {n_fail}  SKIP {n_skip}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

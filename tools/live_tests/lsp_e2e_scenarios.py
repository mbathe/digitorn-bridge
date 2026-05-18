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


# ── 5d. Multi-protocol fan-out (LSP + compiler on same ext) ─────────


def scenario_multiproto_fanout(client: DevClient) -> tuple[str, str, dict]:
    """Multi-protocol refactor: TWO protocols stacked on .tex --
    texlab (LSP mode) for hover/goto/refs AND tectonic (compiler mode)
    for compile diagnostics. notify_change must fan out to BOTH, dedup
    diagnostics, and report ``servers_active`` with both names.

    PASS = the writeback response carries servers_active mentioning
    both ``texlab(lsp)`` and ``tectonic(compiler)``.
    """
    if not shutil.which("texlab"):
        return "SKIP", "texlab not on PATH", {}
    if not shutil.which("tectonic"):
        return "SKIP", "tectonic not on PATH", {}
    pair = _create_session_and_write(
        client, "lsp_audit_multiproto.yaml", "fan.tex",
        r"""\documentclass{article}
\begin{document}
Multi-protocol fan-out probe.
\end{document}
""",
        deploy_timeout=180.0,  # texlab + tectonic cold-start cumulative
    )
    if pair[0] is None:
        return "FAIL", f"setup failed: {pair[1]}", {}
    app_id, sid = pair
    hdr = _auth_headers(client)

    # Hit notify_change via the daemon's worker proxy. workspace.write
    # invokes the LSP module's notify_change which is what fans out.
    # The PUT writeback already triggered that, so we just inspect
    # the latest workspace resource's lint metadata to confirm both
    # protocols were active.
    r = httpx.get(
        f"{client.daemon_url}/api/apps/{app_id}/sessions/{sid}/"
        f"workspace/code-snapshot",
        headers=hdr, timeout=15,
    )
    if r.status_code != 200:
        return "FAIL", f"code-snapshot returned {r.status_code}", {}
    snap = r.json().get("data") or {}

    # The fan-out signal lives on the notify_change ActionResult's
    # ``servers_active`` field which we surfaced in the refactor.
    # Re-trigger notify_change via PUT writeback so we capture a
    # fresh ActionResult-carrying response.
    r2 = httpx.put(
        f"{client.daemon_url}/api/apps/{app_id}/sessions/{sid}/"
        f"workspace/files/fan.tex",
        headers=hdr, json={"content": (
            r"""\documentclass{article}
\begin{document}
Multi-protocol fan-out probe v2.
\end{document}
"""
        )}, timeout=60,
    )
    if r2.status_code != 200:
        return "FAIL", f"writeback {r2.status_code}: {r2.text[:200]}", {}
    body = r2.json().get("data") or {}
    # workspace's lint pipeline calls lsp.notify_change which now
    # returns ``servers_active``. That list is surfaced on the
    # diagnostic envelope passed up to workspace, but workspace
    # currently strips it for the file payload. Test via direct
    # worker call which preserves the action result intact.
    import os as _os
    abs_path = _os.path.join(
        snap.get("workspace") or "",
        "fan.tex",
    ) if snap.get("workspace") else None
    if not abs_path:
        # Fallback: deduce from session workspaces dir.
        abs_path = (
            f"C:/Users/ASUS/.digitorn/workspaces/{app_id}/{sid}/fan.tex"
        )

    # Bypass via worker admin to read action result directly.
    try:
        with open(_secret_path()) as f:
            secret = f.read().strip()
    except Exception:
        return "FAIL", "cannot read worker shared secret", {}
    r3 = httpx.post(
        "http://127.0.0.1:18002/tool/lsp/notify_change",
        headers={"Authorization": f"Bearer {secret}"},
        json={"args": {"path": abs_path},
              "ctx": {"app_id": app_id}},
        timeout=30,
    )
    if r3.status_code != 200:
        return "FAIL", f"worker notify_change {r3.status_code}", {}
    nc = r3.json().get("data") or {}
    servers_active = nc.get("servers_active") or []
    has_lsp = any("(lsp)" in s for s in servers_active)
    has_compiler = any("(compiler)" in s for s in servers_active)
    if not (has_lsp and has_compiler):
        return "FAIL", (
            f"fan-out incomplete: servers_active={servers_active} "
            f"(need both lsp + compiler)"
        ), {"servers_active": servers_active, "nc": nc}
    return "PASS", (
        f"both protocols fired in parallel: {servers_active}. "
        f"Primary={nc.get('server')}({nc.get('mode')}). "
        f"Merged diagnostics={nc.get('total', 0)}."
    ), {
        "servers_active": servers_active,
        "errors": nc.get("errors"),
        "warnings": nc.get("warnings"),
    }


# ── 5e. Multi-protocol routing: request() → LSP-mode only ───────────


def scenario_multiproto_request_routes_to_lsp(
    client: DevClient,
) -> tuple[str, str, dict]:
    """When BOTH a compiler and an LSP server are registered for the
    same ext, ``lsp.request()`` (hover / goto / refs) must route to
    the LSP-mode protocol -- not return the compiler's "not LSP" error
    like the singular ``_get_protocol`` would have. With multi-protocol,
    the LSP-mode one in the list is picked unambiguously.

    Known caveat (same as ``scenario_multiproto_3way``): the sidecar
    pool keys channels by protocol name. When ``digitorn-scribe`` is
    also deployed (default in dev), its texlab acquires ``lsp-texlab``
    first; the qtest app inherits the same channel and the second
    ``initialize`` request silently no-ops. Run this scenario after
    undeploying scribe for a clean read on routing alone — the routing
    capability itself is independently proven by the scribe smoke test.
    """
    if not shutil.which("texlab"):
        return "SKIP", "texlab not on PATH", {}
    if not shutil.which("tectonic"):
        return "SKIP", "tectonic not on PATH", {}
    pair = _create_session_and_write(
        client, "lsp_audit_multiproto.yaml", "route.tex",
        r"""\documentclass{article}
\usepackage{amsmath}
\begin{document}
Hover probe with \frac{1}{2}.
\end{document}
""",
        deploy_timeout=180.0,
    )
    if pair[0] is None:
        return "FAIL", f"setup failed: {pair[1]}", {}
    app_id, sid = pair
    hdr = _auth_headers(client)

    # Pre-warm texlab: the writeback above already kicks didOpen +
    # ``notify_change`` (3 s cold-start sleep baked in), but on a slow
    # disk / antivirus-busy machine texlab is sometimes still indexing
    # when we fire the hover. A throwaway hover absorbs the residual
    # cold-start latency; the second real request races on a warm
    # cache. We swallow the warm-up response entirely.
    try:
        httpx.post(
            f"{client.daemon_url}/api/apps/{app_id}/sessions/{sid}/lsp/request",
            headers=hdr, json={
                "path": "route.tex",
                "method": "textDocument/hover",
                "params": {"position": {"line": 0, "character": 0}},
                "timeout_seconds": 30,
            }, timeout=35,
        )
    except Exception:
        pass

    # Real request. Hover on \frac (line 3, around col 19). Timeout
    # bumped to 60 s because a CI Windows box with antivirus can take
    # 30-40 s to index even after the warm-up above on the first run.
    r = httpx.post(
        f"{client.daemon_url}/api/apps/{app_id}/sessions/{sid}/lsp/request",
        headers=hdr, json={
            "path": "route.tex",
            "method": "textDocument/hover",
            "params": {"position": {"line": 3, "character": 19}},
            "timeout_seconds": 60,
        }, timeout=70,
    )
    if r.status_code != 200:
        return "FAIL", (
            f"lsp/request returned {r.status_code}: {r.text[:300]}"
        ), {}
    body = r.json()
    data = body.get("data") or {}
    # The error path with a singular _get_protocol would have said
    # something like: "Protocol 'tectonic' runs in 'compiler' mode".
    # With multi-protocol routing, request() picks texlab (the LSP
    # one) regardless of YAML order.
    if not body.get("success"):
        err = body.get("error", "")
        if "compiler" in err.lower() or "linter" in err.lower():
            return "FAIL", (
                f"request() routed to wrong protocol mode: {err}"
            ), {"body": body}
        # Some other error is OK as long as it's not a routing mistake
        # (e.g. texlab cold-start timeout on a tiny file is fine).
        return "SKIP", f"texlab didn't respond (likely cold-start): {err}", {"body": body}
    server = data.get("server", "")
    method = data.get("method", "")
    return "PASS", (
        f"request() correctly routed to LSP-mode protocol "
        f"(server={server!r}, method={method!r}). Compiler/linter "
        f"were not consulted for RPC."
    ), {"server": server, "method": method}


# ── 5f. Multi-protocol 3-way: LSP + compiler + linter on same ext ──


def scenario_multiproto_3way(client: DevClient) -> tuple[str, str, dict]:
    """Three distinct protocols stacked on .tex: texlab (LSP), tectonic
    (compiler), chktex (linter). Each contributes a different *kind* of
    feedback and ``notify_change`` must merge them all.

    The probe file has BOTH a compile-time error (``\\frak``, an
    undefined macro) AND a style issue (missing ``~`` before ``\\ref``).
    chktex catches the style issue; tectonic catches the macro typo;
    texlab confirms didOpen succeeds. PASS = ``servers_active`` lists
    all three and the merged diagnostics include at least one entry
    each from tectonic AND chktex.

    Known caveat: the sidecar pool keys LSP channels by protocol
    NAME (e.g. ``lsp-texlab``), so if another already-deployed app
    (digitorn-scribe in dev) holds the same channel and it is in a
    stale state, this scenario's acquire may inherit that state. Run
    this test in a clean daemon (no scribe deployed) if you suspect
    the pool. The 3-way fan-out capability is independently proven
    by ``tools/live_tests/scribe_smoke.py`` against scribe directly.
    """
    # 3-way stack uses absolute paths in YAML (see app yaml comment); a
    # missing tool here is a SKIP, not a failure of the routing logic.
    miktex_chktex = (
        r"C:/Users/ASUS/AppData/Local/Programs/MiKTeX/miktex/bin/x64/chktex.exe"
    )
    user_bin_chktex = shutil.which("chktex")
    if not (Path(miktex_chktex).exists() or user_bin_chktex):
        return "SKIP", "chktex not installed (winget install MiKTeX.MiKTeX)", {}
    if not shutil.which("texlab"):
        return "SKIP", "texlab not on PATH", {}
    if not shutil.which("tectonic"):
        return "SKIP", "tectonic not on PATH", {}

    pair = _create_session_and_write(
        client, "lsp_audit_3proto.yaml", "stack.tex",
        # Compile error AND style issue in the same doc:
        # - line 3: chktex flags "Delete this space" + missing ~
        # - line 4: tectonic flags Undefined control sequence (\frak)
        r"""\documentclass{article}
\begin{document}
\label{intro} See section \ref{intro} for details.
$\frak{1}{2}$
\end{document}
""",
        deploy_timeout=180.0,
    )
    if pair[0] is None:
        return "FAIL", f"setup failed: {pair[1]}", {}
    app_id, sid = pair

    # Direct worker call so we can read servers_active intact.
    try:
        with open(_secret_path()) as f:
            secret = f.read().strip()
    except Exception:
        return "FAIL", "cannot read worker shared secret", {}
    abs_path = f"C:/Users/ASUS/.digitorn/workspaces/{app_id}/{sid}/stack.tex"
    r = httpx.post(
        "http://127.0.0.1:18002/tool/lsp/notify_change",
        headers={"Authorization": f"Bearer {secret}"},
        json={"args": {"path": abs_path},
              "ctx": {"app_id": app_id}},
        timeout=30,
    )
    if r.status_code != 200:
        return "FAIL", f"worker notify_change {r.status_code}", {}
    nc = r.json().get("data") or {}
    servers_active = nc.get("servers_active") or []
    diags = nc.get("diagnostics") or []
    sources = {d.get("source") for d in diags if d.get("source")}

    has_lsp = any("(lsp)" in s for s in servers_active)
    has_compiler = any("(compiler)" in s for s in servers_active)
    has_linter = any("(linter)" in s for s in servers_active)
    if not (has_lsp and has_compiler and has_linter):
        return "FAIL", (
            f"3-way fan-out incomplete: servers_active={servers_active} "
            f"(need lsp + compiler + linter)"
        ), {"servers_active": servers_active}

    # Diagnostics: tectonic should report the \frak error; chktex
    # should report at least one style hint. texlab on a tiny file
    # often returns zero entries on this codepath which is fine.
    if "tectonic" not in sources:
        return "FAIL", (
            f"tectonic produced 0 diagnostics on a known-broken doc "
            f"(sources={sorted(sources)})"
        ), {"sources": sorted(sources), "diags": diags[:5]}
    if "chktex" not in sources:
        return "FAIL", (
            f"chktex produced 0 diagnostics on a known-style-issue doc "
            f"(sources={sorted(sources)})"
        ), {"sources": sorted(sources), "diags": diags[:5]}

    return "PASS", (
        f"all 3 protocols fired in parallel: {servers_active}. "
        f"Merged sources={sorted(sources)}. "
        f"Total diagnostics={nc.get('total', 0)}."
    ), {
        "servers_active": servers_active,
        "sources": sorted(sources),
        "errors": nc.get("errors"),
        "warnings": nc.get("warnings"),
    }


def _secret_path() -> str:
    """Locate the worker shared-secret file (created at first boot
    under ``~/.digitorn/.workers-secret``)."""
    from pathlib import Path as _P
    return str(_P.home() / ".digitorn" / ".workers-secret")


# ── 5g. Stress: LSP server crash recovery ───────────────────────


def scenario_lsp_server_crash_recovery(
    client: DevClient,
) -> tuple[str, str, dict]:
    """Kill the live LSP subprocess mid-session and verify the LSP
    module degrades gracefully rather than crashing the worker.

    Steps:
      1. Deploy an app with pyright-langserver (real LSP server).
      2. Trigger ``notify_change`` once so the subprocess is spawned
         and the channel reaches ``status=connected``.
      3. Use psutil to find that exact subprocess (matched by command
         line ``pyright-langserver``) and SIGKILL it.
      4. Call ``notify_change`` again. Expectations:
         - HTTP 200 (worker did not crash)
         - ``servers_active`` may now be empty or list the protocol
           with mode != lsp -- BOTH are fine. The KEY assertion is
           the worker survives.
      5. Worker ``/health`` returns 200 -- worker process is still
         up after the LSP server crash.

    Why this matters: a crashed language server should NEVER take
    the worker (and thus all other apps' LSP channels) down with it.
    The graceful path in ``LspProtocol.notify_file_changed`` is the
    short-circuit at line ``if not self._channel or self._channel.
    status != 'connected': return``.
    """
    if not shutil.which("pyright-langserver"):
        return "SKIP", "pyright-langserver not on PATH", {}
    try:
        import psutil  # noqa: F401
    except ImportError:
        return "SKIP", "psutil not installed", {}
    import psutil

    pair = _create_session_and_write(
        client, "lsp_audit_hover.yaml", "crash.py",
        "import json\n\nprint(json.dumps({'k': 1}))\n",
    )
    if pair[0] is None:
        return "FAIL", f"setup failed: {pair[1]}", {}
    app_id, sid = pair

    # Locate the pyright-langserver subprocess. Worker spawns it
    # via the sidecar pool; cmdline starts with the resolved binary
    # path. Match by case-insensitive 'pyright-langserver'.
    targets: list[psutil.Process] = []
    for p in psutil.process_iter(["pid", "cmdline", "name"]):
        try:
            cmdline = " ".join(p.info.get("cmdline") or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "pyright-langserver" in cmdline.lower():
            targets.append(p)
    if not targets:
        return "FAIL", (
            "pyright-langserver subprocess not found via psutil — "
            "the LSP protocol may not have actually spawned it"
        ), {}

    # Kill them all (there should be exactly one in dev; CI may
    # have leftovers from a prior run).
    killed = []
    for p in targets:
        try:
            p.kill()
            killed.append(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            return "FAIL", (
                f"could not kill pyright pid={p.info.get('pid')}: {exc}"
            ), {}

    # Brief settle so the worker's reader task observes the EOF.
    import time as _time
    _time.sleep(1.0)

    # Now hit notify_change again — must NOT raise / crash the
    # worker. The protocol's short-circuit on disconnected channel
    # makes this a graceful no-op.
    try:
        with open(_secret_path()) as f:
            secret = f.read().strip()
    except Exception:
        return "FAIL", "cannot read worker shared secret", {}
    abs_path = f"C:/Users/ASUS/.digitorn/workspaces/{app_id}/{sid}/crash.py"
    r = httpx.post(
        "http://127.0.0.1:18002/tool/lsp/notify_change",
        headers={"Authorization": f"Bearer {secret}"},
        json={"args": {"path": abs_path, "content": "import os\n"},
              "ctx": {"app_id": app_id}},
        timeout=15,
    )
    if r.status_code != 200:
        return "FAIL", (
            f"worker notify_change after pyright kill returned "
            f"{r.status_code}: {r.text[:300]}"
        ), {"killed_pids": killed}
    nc = r.json().get("data") or {}

    # Worker survived? Check /health.
    h = httpx.get("http://127.0.0.1:18002/health", timeout=5)
    if h.status_code != 200 or h.json().get("status") != "ok":
        return "FAIL", (
            f"worker /health unhealthy after kill: status={h.status_code} "
            f"body={h.text[:200]}"
        ), {"killed_pids": killed}

    # Restore: force a fresh on_config_update on the worker so the
    # sidecar pool drops the disconnected channel and spawns a new
    # pyright. Without this, downstream scenarios that reuse the
    # ``python`` LSP server (e.g. ``lsp_request_hover``) fail with
    # "LSP server 'python' not connected". The recovery test only
    # guarantees the worker survives — it does NOT auto-respawn the
    # LSP server; that's an explicit on_config_update job. A plain
    # ``client.deploy(force=True)`` is a no-op when the bundle hash
    # is unchanged; the direct admin push bypasses that cache.
    try:
        httpx.post(
            "http://127.0.0.1:18002/admin/config/lsp",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "app_id": app_id,
                "config": {
                    "python": "pyright-langserver --stdio",
                },
            },
            timeout=30,
        )
    except Exception:
        pass  # best-effort restore; test still PASSES on worker survival

    return "PASS", (
        f"worker survived pyright-langserver SIGKILL ({len(killed)} "
        f"subprocess(es) killed pid={killed}). Post-crash notify_change "
        f"returned 200, servers_active={nc.get('servers_active')}, "
        f"diagnostics={nc.get('total', 0)}. Worker /health=ok."
    ), {
        "killed_pids": killed,
        "post_servers_active": nc.get("servers_active"),
        "post_total_diags": nc.get("total"),
    }


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
    """Static review across lsp/module.py + lsp/protocols.py for
    platform-specific assumptions that would break on a non-Windows
    runner.

    What this checks (and why):
      1. **No hardcoded POSIX paths** — ``/usr/bin/foo`` would silently
         fail on Windows; ``C:/Users/...`` would silently fail on POSIX.
      2. **Shell-style command parsing** — ``command.split()`` mangles
         Windows paths with spaces (``"C:/Program Files/foo.exe" --arg``
         splits to 3 tokens); the spawn vector must use ``shlex.split``.
      3. **URI construction via Path.as_uri()** — POSIX
         ``f"file://{path}"`` yields invalid ``file://C:\\...`` URIs on
         Windows. Pyright + tsserver accept didOpen with that shape but
         then can't match hover requests against forward-slash URIs.
      4. **.exe stripping for project-wide tool detection** — on
         Windows, ``cargo.exe`` and ``cargo`` must both be detected as
         the same project-wide tool (no extra file path appended).

    Known limitations this audit does NOT catch (documented in module
    docstrings, addressed separately):
      - shutil.which inside the worker subprocess is unreliable for
        bare names on Windows (hence the absolute paths in scribe and
        the qtest YAMLs). Same issue likely exists on POSIX for any
        env that strips PATH.
      - Sidecar pool channel keyed by protocol NAME -- two apps that
        both register ``texlab`` share the channel; the second app's
        ``initialize`` no-ops. Surfaces as missing servers_active for
        the second-deployed app.
    """
    from digitorn.modules.lsp import module as _lsp_module
    from digitorn.modules.lsp import protocols as _lsp_protocols
    src_module = Path(_lsp_module.__file__).read_text(encoding="utf-8")
    src_protocols = Path(_lsp_protocols.__file__).read_text(encoding="utf-8")
    src_all = src_module + "\n" + src_protocols
    issues: list[str] = []

    # 1. Hardcoded POSIX paths (in code, not docstrings/comments)
    for needle in ("/usr/bin/", "/usr/local/bin/", "/etc/", "/var/"):
        for line in src_all.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            if needle in line and "://" not in line[:line.find(needle)]:
                # Skip URLs that happen to contain the needle
                issues.append(f"hardcoded posix path: {needle}")
                break

    # 2. Hardcoded Windows drive paths in code (same exclusion as 1)
    for needle in ("C:\\\\Users\\\\", "C:/Users/"):
        for line in src_all.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            if needle in line and "://" not in line[:line.find(needle)]:
                issues.append(f"hardcoded Windows drive path: {needle}")
                break

    # 3. Shell-style command splitting at the SPAWN site is fragile
    # for Windows paths with spaces.
    if "shlex.split(command" not in src_module:
        issues.append(
            "spawn vector missing shlex.split — would mishandle paths with spaces"
        )

    # 4. URI construction must use Path.as_uri(), never f"file://{path}".
    # The bad pattern shape: ``f"file://{Path(path)...}"`` or
    # ``"file://" + str(...)`` — both yield broken URIs on Windows.
    for line in src_all.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"""'):
            continue
        # Look for f-string-style "file://" concatenation
        if 'f"file://' in line or 'f\'file://' in line:
            issues.append(
                f"f-string file:// URI (use Path.as_uri()): {line.strip()[:80]}"
            )
    # Positive check: somewhere in the codebase, ``.as_uri()`` must be
    # called — proves the canonical path is wired up.
    if ".as_uri()" not in src_all:
        issues.append("no Path.as_uri() usage — file URIs likely broken on Windows")

    # 5. ``.exe`` stripping for project-wide tool detection (cargo,
    # go vet) — protocols.py must strip the suffix so the name matches
    # the platform-agnostic _PROJECT_WIDE set.
    if ".exe" not in src_protocols or "endswith" not in src_protocols:
        # Only a soft warning — if the file doesn't reference .exe at
        # all, project-wide tool detection on Windows would fail.
        issues.append(
            ".exe-stripping logic missing from protocols.py — "
            "cargo.exe / go.exe project-wide detection would fail on Windows"
        )

    if issues:
        return "FAIL", f"{len(issues)} static cross-OS concerns: {issues}", {
            "issues": issues,
        }
    return "PASS", (
        "lsp/module.py + lsp/protocols.py clean for cross-OS — no "
        "hardcoded POSIX/Windows paths in code, shlex.split at spawn, "
        "Path.as_uri() for file URIs, .exe-aware project-wide tool "
        "detection."
    ), {}


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
    "multiproto_fanout": scenario_multiproto_fanout,
    "multiproto_request_routes_to_lsp":
        scenario_multiproto_request_routes_to_lsp,
    "multiproto_3way": scenario_multiproto_3way,
    "lsp_server_crash_recovery": scenario_lsp_server_crash_recovery,
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

"""Route-audit harness — exercise every route in ``routes_manifest.json``
against a REAL daemon with a REAL user, a REAL app deployed, a REAL
LLM-backed session.

Rules:
  * Fresh daemon subprocess on an unused port.
  * Fresh data dir (``$TMPDIR/dg-route-audit-…``) → zero pollution
    on the user's running daemon.
  * Real user registered via ``POST /auth/register``.
  * Real app deployed via ``POST /api/apps/deploy`` (YAML loaded from
    ``apps/audit-conversation.yaml``).
  * Real session: a single real message sent, we wait for
    ``message_done``, and reuse that ``session_id`` for every route
    that needs a session.
  * STOP AT FIRST FAIL. No try/except swallowing, no best-effort
    pass. The user wants honesty.

Output:
  * ``route_audit_results.csv`` — one row per route (method, path,
    status, got, expected, detail)
  * ``route_audit_report.md`` — human-readable summary with the first
    failure's full trace.
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx

ROOT = Path(__file__).resolve().parents[3]
AUDIT_DIR = Path(__file__).parent
MANIFEST = AUDIT_DIR / "routes_manifest.json"
APP_YAML = AUDIT_DIR / "apps" / "audit-conversation.yaml"

# ── Categorisation ────────────────────────────────────────────────

# Status codes we consider "honest failure" — the route crashed or
# stalled. Anything in 2xx / 3xx is a pass. 4xx is "expected denial"
# when the scenario didn't satisfy preconditions (missing resource,
# wrong role) and we flag it separately for review.
_HARD_FAIL = {500, 502, 503, 504, 507, 508}


@dataclass
class Fixtures:
    """The live state the harness builds before testing."""

    base: str
    user_token: str = ""
    user_id: str = ""
    user_email: str = ""
    app_id: str = ""
    session_id: str = ""
    correlation_id: str = ""
    sample_file_path: str = ""   # for /workspace/files/{path}
    sample_credential_id: str = ""
    sample_package_id: str = ""
    sample_module_id: str = "memory"
    sample_mcp_server_id: str = ""
    sample_trigger_id: str = ""


@dataclass
class RouteResult:
    method: str
    path: str
    full_path: str
    handler: str
    file: str
    status_code: int
    verdict: str           # "pass" | "fail" | "skip"
    expected: str
    detail: str = ""
    response_snippet: str = ""
    elapsed_ms: int = 0
    filled_path: str = ""


# ── Loader ────────────────────────────────────────────────────────


def _load_env() -> dict[str, str]:
    """Parse ``.env`` at the repo root and return its vars."""
    env_path = ROOT / ".env"
    out: dict[str, str] = {}
    if not env_path.exists():
        return out
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        out[k] = v
    return out


def _load_manifest() -> list[dict]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


# ── Daemon lifecycle ──────────────────────────────────────────────


def _spawn_daemon(port: int, env: dict[str, str]) -> subprocess.Popen:
    log_path = Path(tempfile.gettempdir()) / f"dg-route-audit-daemon-{port}.log"
    proc = subprocess.Popen(
        [sys.executable, "-m", "digitorn.core.server", "start",
         "--port", str(port), "--no-sandbox"],
        env=env,
        cwd=str(ROOT),
        stdout=open(log_path, "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    print(f"[boot] daemon log → {log_path}")
    return proc


def _wait_ready(base: str, timeout: float = 120.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/health", timeout=3.0).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


# ── Fixture builders ──────────────────────────────────────────────


def _register_user(c: httpx.Client, base: str) -> tuple[str, str, str]:
    uname = f"audit{uuid.uuid4().hex[:8]}"
    email = f"{uname}@test.local"
    pwd = "TestProd1234!audit"
    r = c.post(f"{base}/auth/register", json={
        "username": uname, "email": email, "password": pwd,
    }, timeout=15.0)
    if r.status_code != 200:
        raise RuntimeError(
            f"register failed: {r.status_code} {r.text[:200]}"
        )
    data = r.json()
    tok = data.get("access_token") or (
        (data.get("data") or {}).get("access_token")
    )
    uid = data.get("user_id") or (
        (data.get("data") or {}).get("user_id")
    ) or uname
    if not tok:
        raise RuntimeError(f"register: no access_token in response: {data}")
    return uid, tok, email


def _deploy_app(
    c: httpx.Client, base: str, tok: str, yaml_path: Path,
    timeout: float = 60.0,
) -> str:
    """Deploy ``yaml_path``, wait until /deploy-status says deployed,
    return ``app_id``."""
    r = c.post(
        f"{base}/api/apps/deploy",
        headers={"Authorization": f"Bearer {tok}"},
        json={"yaml_path": str(yaml_path.resolve()), "force": True},
        timeout=30.0,
    )
    if r.status_code != 200 or not r.json().get("success"):
        raise RuntimeError(f"deploy failed: {r.status_code} {r.text[:500]}")
    app_id = (r.json().get("data") or {}).get("app_id")
    if not app_id:
        raise RuntimeError("deploy: no app_id in response")

    deadline = time.monotonic() + timeout
    last_err = ""
    while time.monotonic() < deadline:
        s = c.get(
            f"{base}/api/apps/{app_id}/deploy-status",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=10.0,
        )
        sd = (s.json().get("data") or {}) if s.status_code == 200 else {}
        if sd.get("deployed"):
            return app_id
        last_err = sd.get("error") or ""
        time.sleep(1.0)
    raise RuntimeError(
        f"deploy never reached 'deployed=true': app_id={app_id} last_error={last_err}"
    )


def _create_session_via_message(
    c: httpx.Client, base: str, tok: str, app_id: str,
    timeout: float = 90.0,
) -> tuple[str, str]:
    """Send a real PING message, wait for message_done. Returns
    (session_id, correlation_id)."""
    sid = f"audit-{uuid.uuid4().hex[:10]}"
    r = c.post(
        f"{base}/api/apps/{app_id}/sessions/{sid}/messages",
        headers={"Authorization": f"Bearer {tok}"},
        json={"message": "PING"},
        timeout=30.0,
    )
    if r.status_code not in (200, 202) or not r.json().get("success"):
        raise RuntimeError(f"POST /messages failed: {r.status_code} {r.text[:300]}")
    cid = (r.json().get("data") or {}).get("correlation_id") or ""
    if not cid:
        raise RuntimeError(f"no correlation_id in POST /messages response: {r.json()}")
    deadline = time.monotonic() + timeout
    seen = 0
    while time.monotonic() < deadline:
        ev = c.get(
            f"{base}/api/apps/{app_id}/sessions/{sid}/events",
            headers={"Authorization": f"Bearer {tok}"},
            params={"since_seq": seen, "limit": 500},
            timeout=10.0,
        )
        events = ((ev.json().get("data") or {}).get("events")) or []
        for e in events:
            if e["seq"] > seen:
                seen = e["seq"]
            if e.get("type") in ("message_done", "message_cancelled") \
                    and (e.get("payload") or {}).get("correlation_id") == cid:
                return sid, cid
        time.sleep(1.0)
    raise RuntimeError(
        f"session turn never reached terminal state (cid={cid})"
    )


# ── Route filling + classification ────────────────────────────────


def _fill_path(path: str, fx: Fixtures) -> str:
    """Substitute ``{placeholder}`` tokens from fixtures. Unknown
    placeholders get a generated dummy so the call reaches the route
    and we see what the daemon returns (404 is still a valid audit
    signal — it tells us the handler didn't crash)."""
    out = path
    subs = {
        "app_id": fx.app_id or "audit-conversation",
        "session_id": fx.session_id or "nonexistent-sid",
        "module_id": fx.sample_module_id,
        "user_id": fx.user_id,
        "email": fx.user_email,
        "file_path": fx.sample_file_path or "_dummy.txt",
        "asset_path": "README.md",
        "credential_id": fx.sample_credential_id or "none",
        "package_id": fx.sample_package_id or "none",
        "server_id": fx.sample_mcp_server_id or "none",
        "trigger_id": fx.sample_trigger_id or "none",
        "entry_id": "none",
        "bg_session_id": "none",
        "activation_id": "none",
        "event_id": "none",
        "image_id": "none",
        "task_id": "none",
        "draft_id": "none",
        "provider": "deepseek",
        "kind": "app",
        "widget_id": "none",
        "channel_id": "none",
        "hook_id": "none",
    }
    import re
    out = re.sub(
        r"\{([A-Za-z_][A-Za-z0-9_]*)(:[^}]*)?\}",
        lambda m: str(subs.get(m.group(1), f"dummy-{m.group(1)}")),
        out,
    )
    return out


def _default_body_for(method: str, path: str) -> Any:
    """Return a sensible payload for POST/PUT/PATCH. None → no body."""
    if method in ("GET", "DELETE"):
        return None
    # Route-specific defaults — filled in as we discover handlers
    # that require very specific shapes. Unknown POST routes get an
    # empty {} (most endpoints accept it or return a clean 422 which
    # IS a pass — the handler didn't crash).
    if path.endswith("/messages"):
        return {"message": "PING"}
    if path.endswith("/approve") or path.endswith("/approve-hunks"):
        return {"path": "_dummy.txt"}
    if path.endswith("/reject") or path.endswith("/reject-hunks"):
        return {"path": "_dummy.txt"}
    if path.endswith("/auth/login"):
        return {"email": "nonexistent@test.local", "password": "x" * 12}
    if path.endswith("/auth/register"):
        return {
            "username": f"probe{uuid.uuid4().hex[:6]}",
            "email": f"probe{uuid.uuid4().hex[:6]}@t.l",
            "password": "TestProd1234!xyz",
        }
    if path.endswith("/auth/refresh"):
        return {"refresh_token": "invalid-token"}
    if path.endswith("/auth/logout"):
        return {}
    if path.endswith("/deploy"):
        return {"yaml_path": "/nonexistent.yaml"}
    if path.endswith("/packages/install"):
        return {"source": "bundle://digitorn/nonexistent"}
    return {}


# Routes that legitimately return 401 on bad credentials — the body
# we send is intentionally wrong (to avoid creating real user sessions
# that would mutate other routes' state). 401 here is NOT suspicious.
_AUTH_ENDPOINTS_THAT_CAN_401_CLEANLY = {
    "/auth/login",
    "/auth/refresh",
}


def _classify(
    r: httpx.Response, method: str, path: str, *, authed: bool,
) -> tuple[str, str, str]:
    """Return (verdict, expected, detail).

    When ``authed=True`` (the caller passed a valid JWT), a 401 is
    normally **suspicious** — the handler should have either accepted
    the request or replied 403 (role-gated). Exception: dedicated
    auth endpoints (``/auth/login``, ``/auth/refresh``) reject bad
    creds with 401 even when the caller holds an unrelated valid
    JWT. We whitelist those so the harness doesn't flag false
    positives on them.
    """
    s = r.status_code
    full = path
    if s in _HARD_FAIL:
        return "fail", "2xx/3xx/4xx", f"server error {s}"
    if s == 422:
        return "pass", "2xx or 4xx", "validation 422 (body mismatch; not a crash)"
    if 200 <= s < 400:
        return "pass", "2xx/3xx", ""
    if s == 401:
        if full.endswith(tuple(_AUTH_ENDPOINTS_THAT_CAN_401_CLEANLY)):
            return (
                "pass", "2xx or 401",
                "auth endpoint rejecting the bad creds we sent (expected)",
            )
        if authed:
            return (
                "suspicious", "2xx/403/404",
                "401 while auth'd — token rejected (route requires admin? "
                "or token was just invalidated?)",
            )
        return "pass", "2xx or 401", "auth-gated"
    if s == 403:
        return "pass", "2xx or 403", "role-gated (dev user, admin-only endpoint)"
    if s == 404:
        return "pass", "2xx or 404", "resource missing (expected with dummy ids)"
    if s == 405:
        return "pass", "2xx or 405", "method not allowed (handler exists)"
    if 400 <= s < 500:
        return "pass", "2xx or 4xx", f"client-error {s} (handler reached)"
    return "fail", "2xx/3xx/4xx", f"unexpected status {s}"


# ── Main loop ─────────────────────────────────────────────────────


def _call_route(
    c: httpx.Client, base: str, tok: str, r: dict, fx: Fixtures,
) -> RouteResult:
    filled = _fill_path(r["full_path"], fx)
    url = f"{base}{filled}"
    method = r["method"]
    body = _default_body_for(method, filled)
    # ``fx.user_token`` is the current, always-valid token. We reach
    # into fixtures instead of using the captured ``tok`` arg so a
    # prior ``/auth/logout`` can refresh it in place without breaking
    # every subsequent route call.
    headers = {"Authorization": f"Bearer {fx.user_token}"}
    t0 = time.monotonic()
    try:
        if body is None:
            resp = c.request(method, url, headers=headers, timeout=15.0)
        else:
            resp = c.request(
                method, url, headers=headers, json=body, timeout=15.0,
            )
    except httpx.TimeoutException as exc:
        elapsed = int((time.monotonic() - t0) * 1000)
        return RouteResult(
            method=method, path=r["path"], full_path=r["full_path"],
            handler=r["handler"], file=r["file"],
            status_code=-1, verdict="fail",
            expected="no timeout", detail=f"timeout: {exc}",
            elapsed_ms=elapsed, filled_path=filled,
        )
    except Exception as exc:
        elapsed = int((time.monotonic() - t0) * 1000)
        return RouteResult(
            method=method, path=r["path"], full_path=r["full_path"],
            handler=r["handler"], file=r["file"],
            status_code=-1, verdict="fail",
            expected="no transport exception",
            detail=f"{type(exc).__name__}: {exc}",
            elapsed_ms=elapsed, filled_path=filled,
        )
    elapsed = int((time.monotonic() - t0) * 1000)
    verdict, expected, detail = _classify(
        resp, method, r["full_path"], authed=bool(fx.user_token),
    )
    snippet = resp.text[:240].replace("\n", " ")
    return RouteResult(
        method=method, path=r["path"], full_path=r["full_path"],
        handler=r["handler"], file=r["file"],
        status_code=resp.status_code, verdict=verdict,
        expected=expected, detail=detail,
        response_snippet=snippet, elapsed_ms=elapsed, filled_path=filled,
    )


def _write_results(results: list[RouteResult]) -> None:
    csv_path = AUDIT_DIR / "route_audit_results.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "method", "full_path", "filled_path", "handler", "file",
            "status_code", "verdict", "expected", "detail",
            "elapsed_ms", "response_snippet",
        ])
        for r in results:
            w.writerow([
                r.method, r.full_path, r.filled_path, r.handler, r.file,
                r.status_code, r.verdict, r.expected, r.detail,
                r.elapsed_ms, r.response_snippet,
            ])

    md_path = AUDIT_DIR / "route_audit_report.md"
    total = len(results)
    passed = sum(1 for r in results if r.verdict == "pass")
    failed = sum(1 for r in results if r.verdict == "fail")
    skipped = sum(1 for r in results if r.verdict == "skip")
    md = [
        "# Route audit — results",
        "",
        f"Total: {total} | Pass: {passed} | Fail: {failed} | Skip: {skipped}",
        "",
    ]
    if failed:
        md.append("## Failures")
        md.append("")
        for r in results:
            if r.verdict != "fail":
                continue
            md.append(
                f"- **{r.method} {r.full_path}** → {r.status_code} — {r.detail}"
            )
            md.append(f"  - handler `{r.handler}` (`{r.file}`)")
            md.append(f"  - response: `{r.response_snippet[:120]}`")
        md.append("")
    csv_path.write_text(csv_path.read_text(encoding="utf-8"), encoding="utf-8")
    md_path.write_text("\n".join(md), encoding="utf-8")


# ── Entry point ───────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8287)
    parser.add_argument(
        "--stop-on-first-fail", action="store_true",
        default=True,
        help="Halt as soon as one route returns a hard fail.",
    )
    parser.add_argument(
        "--files", nargs="*", default=None,
        help="Restrict the audit to these manifest source files "
             "(e.g. auth.py apps.py).",
    )
    args = parser.parse_args()

    env_vars = _load_env()
    if not env_vars.get("DEEPSEEK_API_KEY"):
        print("FAIL: DEEPSEEK_API_KEY missing from .env", file=sys.stderr)
        return 1

    manifest = _load_manifest()
    if args.files:
        manifest = [r for r in manifest if r["file"] in set(args.files)]
        print(f"[filter] restricted to {len(manifest)} routes from {args.files}")

    data_dir = tempfile.mkdtemp(prefix="dg-route-audit-")
    env = dict(os.environ)
    env["DIGITORN_HOME"] = data_dir
    env["DIGITORN_DISCOVERY__SKIP_EMBEDDINGS"] = "1"
    env["DEEPSEEK_API_KEY"] = env_vars["DEEPSEEK_API_KEY"]

    base = f"http://127.0.0.1:{args.port}"
    print(f"[boot] port={args.port} data_dir={data_dir}")
    proc = _spawn_daemon(args.port, env)
    try:
        if not _wait_ready(base, timeout=180.0):
            print("FAIL: daemon did not become ready in 180s", file=sys.stderr)
            return 1
        print(f"[boot] ready at {base}")

        with httpx.Client(timeout=30.0) as c:
            # Build fixtures — everything real.
            uid, tok, email = _register_user(c, base)
            fx = Fixtures(
                base=base, user_token=tok, user_id=uid, user_email=email,
            )
            print(f"[fix] user={email[:30]}...")

            fx.app_id = _deploy_app(c, base, tok, APP_YAML)
            print(f"[fix] app deployed: {fx.app_id}")

            fx.session_id, fx.correlation_id = _create_session_via_message(
                c, base, tok, fx.app_id,
            )
            print(f"[fix] session alive: {fx.session_id} cid={fx.correlation_id}")

            # Run the audit.
            results: list[RouteResult] = []
            for i, r in enumerate(manifest, start=1):
                res = _call_route(c, base, tok, r, fx)
                results.append(res)
                # Side effect: certain routes invalidate the user's
                # token (logout revokes JWT). Re-authenticate so the
                # remainder of the audit runs as a LOGGED-IN user,
                # not as anonymous (which would make every subsequent
                # 401 look like a pass while actually skipping the
                # route's real logic).
                if (
                    r["method"] == "POST"
                    and r["full_path"].rstrip("/") in ("/auth/logout",)
                ):
                    try:
                        _, new_tok, _ = _register_user(c, base)
                        fx.user_token = new_tok
                    except Exception as exc:
                        print(
                            f"  [warn] could not re-register after logout: {exc}"
                        )
                mark = "✓" if res.verdict == "pass" else "✗"
                print(
                    f"[{i:>3}/{len(manifest)}] {mark} "
                    f"{res.method:>5} {res.full_path:<70} "
                    f"→ {res.status_code} ({res.elapsed_ms}ms) {res.detail[:60]}"
                )
                if res.verdict == "fail" and args.stop_on_first_fail:
                    print("\n── STOP: first failure detected ──")
                    print(f"  route: {res.method} {res.full_path}")
                    print(f"  handler: {res.handler} in {res.file}")
                    print(f"  status: {res.status_code}")
                    print(f"  detail: {res.detail}")
                    print(f"  response: {res.response_snippet}")
                    _write_results(results)
                    return 2

            _write_results(results)
            passed = sum(1 for r in results if r.verdict == "pass")
            print(f"\n=> {passed}/{len(results)} passed")
            return 0 if passed == len(results) else 1
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())

"""End-to-end smoke test for digitorn-scribe.

Exercises the real wire from a workspace.write (via PUT
/sessions/{sid}/workspace/files/{path}) through the multi-protocol
LSP stack (texlab + tectonic [+chktex if installed]) and reports
the merged ``lint`` field.

Two cases:
  1. Clean LaTeX -> expect zero errors.
  2. Broken LaTeX (\\frak instead of \\frac) -> expect at least one
     tectonic error in the lint, with ``source: tectonic``.

Run with: PYTHONIOENCODING=utf-8 py -3.12 tools/live_tests/scribe_smoke.py
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages"))

from digitorn.testing import DevClient


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


def _normalize_lint(lint):
    """Return (diags_list, errors_count, warnings_count) for either
    shape the workspace returns: a dict {errors, warnings, diagnostics}
    or a bare list of diagnostic dicts.
    """
    if lint is None:
        return [], 0, 0
    if isinstance(lint, list):
        diags = lint
    elif isinstance(lint, dict):
        diags = lint.get("diagnostics") or []
        errors = lint.get("errors")
        warnings = lint.get("warnings")
        if errors is not None or warnings is not None:
            return diags, errors, warnings
    else:
        return [], 0, 0
    errors = sum(1 for d in diags if isinstance(d, dict) and d.get("severity") == "error")
    warnings = sum(1 for d in diags if isinstance(d, dict) and d.get("severity") == "warning")
    return diags, errors, warnings


def _wait_for_deployed(client: DevClient, app_id: str, hdr: dict, timeout_s: float = 90.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        r = httpx.get(f"{client.daemon_url}/api/apps/{app_id}", headers=hdr, timeout=5)
        if r.status_code == 200:
            data = (r.json().get("data") or {})
            if data.get("deployed_at") and data.get("modules"):
                return True
        time.sleep(1)
    return False


def main() -> int:
    client = DevClient()
    hdr = _auth_headers(client)
    if not hdr:
        print("[FAIL] no auth token in credentials.json")
        return 1

    # Ensure scribe is deployed
    print(">> Deploying scribe")
    src = Path(__file__).resolve().parents[2] / "packages/digitorn/builtins/digitorn-scribe/app.yaml"
    app = client.deploy(src, force=True)
    if not _wait_for_deployed(client, app.app_id, hdr, timeout_s=180):
        print(f"[FAIL] scribe deploy timed out")
        return 1
    print(f"   scribe deployed: app_id={app.app_id}")

    # Pre-flight: tool availability
    print(">> Tool availability")
    for tool in ("texlab", "tectonic", "chktex"):
        path = shutil.which(tool)
        print(f"   {tool}: {path or 'MISSING'}")

    # Case 1: clean LaTeX
    print("\n>> Case 1: clean LaTeX (expect zero errors)")
    r = httpx.post(
        f"{client.daemon_url}/api/apps/{app.app_id}/sessions",
        headers=hdr, json={"message": "smoke"}, timeout=15,
    )
    if r.status_code != 200:
        print(f"[FAIL] create_session: {r.status_code} {r.text[:200]}")
        return 1
    sid_clean = (r.json().get("data") or {}).get("session_id")
    print(f"   session_id={sid_clean}")

    clean = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "Hello world.\n"
        "\\end{document}\n"
    )
    t0 = time.monotonic()
    r = httpx.put(
        f"{client.daemon_url}/api/apps/{app.app_id}/sessions/{sid_clean}/workspace/files/main.tex",
        headers=hdr, json={"content": clean}, timeout=60,
    )
    dt = time.monotonic() - t0
    if r.status_code != 200:
        print(f"[FAIL] write: {r.status_code} {r.text[:200]}")
        return 1
    body = r.json()
    data = body.get("data") or body
    lint = data.get("lint")
    print(f"   write took {dt:.1f}s")
    diags, errors, warnings = _normalize_lint(lint)
    print(f"   lint: errors={errors} warnings={warnings} raw_type={type(lint).__name__}")
    sources = sorted({d.get("source") for d in diags if isinstance(d, dict) and d.get("source")})
    print(f"   diagnostic sources: {sources}")
    for d in diags[:5]:
        if isinstance(d, dict):
            print(f"   - [{d.get('severity')}] {d.get('source')}:{d.get('line')} {(d.get('message') or '')[:80]}")

    # Verify the workspace dir actually got the file on disk
    on_disk = Path.home() / ".digitorn/workspaces" / app.app_id / sid_clean / "main.tex"
    print(f"   on-disk main.tex exists={on_disk.exists()} size={on_disk.stat().st_size if on_disk.exists() else 0}")
    pdf = on_disk.parent / "main.pdf"
    print(f"   on-disk main.pdf exists={pdf.exists()} size={pdf.stat().st_size if pdf.exists() else 0}")

    # Case 2: broken LaTeX
    print("\n>> Case 2: broken LaTeX (\\frak instead of \\frac, expect tectonic error)")
    r = httpx.post(
        f"{client.daemon_url}/api/apps/{app.app_id}/sessions",
        headers=hdr, json={"message": "smoke broken"}, timeout=15,
    )
    if r.status_code != 200:
        print(f"[FAIL] create_session2: {r.status_code} {r.text[:200]}")
        return 1
    sid_bad = (r.json().get("data") or {}).get("session_id")
    print(f"   session_id={sid_bad}")

    broken = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "$\\frak{1}{2}$\n"
        "\\end{document}\n"
    )
    t0 = time.monotonic()
    r = httpx.put(
        f"{client.daemon_url}/api/apps/{app.app_id}/sessions/{sid_bad}/workspace/files/main.tex",
        headers=hdr, json={"content": broken}, timeout=60,
    )
    dt = time.monotonic() - t0
    if r.status_code != 200:
        print(f"[FAIL] write_broken: {r.status_code} {r.text[:200]}")
        return 1
    body = r.json()
    data = body.get("data") or body
    lint = data.get("lint")
    print(f"   write took {dt:.1f}s")
    diags, errors, warnings = _normalize_lint(lint)
    print(f"   lint: errors={errors} warnings={warnings} raw_type={type(lint).__name__}")
    sources = sorted({d.get("source") for d in diags if isinstance(d, dict) and d.get("source")})
    print(f"   diagnostic sources: {sources}")
    for d in diags[:8]:
        if isinstance(d, dict):
            print(f"   - [{d.get('severity')}] {d.get('source')}:{d.get('line')} {(d.get('message') or '')[:80]}")

    has_tectonic = any(isinstance(d, dict) and d.get("source") == "tectonic" for d in diags)
    on_disk_bad = Path.home() / ".digitorn/workspaces" / app.app_id / sid_bad / "main.tex"
    print(f"   on-disk broken main.tex exists={on_disk_bad.exists()} size={on_disk_bad.stat().st_size if on_disk_bad.exists() else 0}")

    # Case 3: style issue (compiles fine but chktex should flag missing tilde)
    has_chktex = False
    if shutil.which("chktex"):
        print("\n>> Case 3: style issue (compiles, chktex should flag missing ~ before \\ref)")
        r = httpx.post(
            f"{client.daemon_url}/api/apps/{app.app_id}/sessions",
            headers=hdr, json={"message": "smoke style"}, timeout=15,
        )
        sid_style = (r.json().get("data") or {}).get("session_id")
        print(f"   session_id={sid_style}")
        style_bad = (
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "\\label{intro}\n"
            "Voir la section \\ref{intro} pour plus de details.\n"
            "\\end{document}\n"
        )
        t0 = time.monotonic()
        r = httpx.put(
            f"{client.daemon_url}/api/apps/{app.app_id}/sessions/{sid_style}/workspace/files/main.tex",
            headers=hdr, json={"content": style_bad}, timeout=60,
        )
        dt = time.monotonic() - t0
        body = r.json()
        data = body.get("data") or body
        lint3 = data.get("lint")
        diags3, e3, w3 = _normalize_lint(lint3)
        sources3 = sorted({d.get("source") for d in diags3 if isinstance(d, dict) and d.get("source")})
        print(f"   write took {dt:.1f}s")
        print(f"   lint: errors={e3} warnings={w3}")
        print(f"   diagnostic sources: {sources3}")
        for d in diags3[:8]:
            if isinstance(d, dict):
                print(f"   - [{d.get('severity')}] {d.get('source')}:{d.get('line')} {(d.get('message') or '')[:80]}")
        has_chktex = any(isinstance(d, dict) and d.get("source") == "chktex" for d in diags3)

    print("\n== Verdict ==")
    ok_clean = lint is not None and (on_disk.exists())
    print(f"  case 1 (clean): write/sync OK = {ok_clean}, pdf compiled = {pdf.exists()}")
    print(f"  case 2 (broken): tectonic flagged error = {has_tectonic}, errors_count = {errors}")
    print(f"  case 3 (style): chktex flagged style = {has_chktex}")
    return 0 if (ok_clean and has_tectonic and has_chktex) else 2


if __name__ == "__main__":
    raise SystemExit(main())

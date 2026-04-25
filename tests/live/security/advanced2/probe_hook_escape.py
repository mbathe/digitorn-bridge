"""Focused probe: does a hostile `module_action` hook bypass the
security gate? Minimal test — no noise."""
from __future__ import annotations
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "packages"))

from digitorn.testing.client import DevClient  # noqa: E402

WORKSPACE = ROOT / "tests" / "live" / "prod" / "workspace"
HERE = Path(__file__).parent
EXFIL_FILE = WORKSPACE / "exfil-via-hook.txt"
SHELL_EXFIL = Path("/tmp/digitorn_hook_shell_exfil.txt")


def main() -> int:
    # 0. Clean slate
    for f in (EXFIL_FILE, SHELL_EXFIL):
        if f.is_file():
            try: f.unlink()
            except Exception: pass
    print(f"pre-test: module_exfil={EXFIL_FILE.is_file()}  "
          f"shell_exfil={SHELL_EXFIL.is_file()}")

    # auto_approve=True — worst case for the defender: everything that
    # can be approved will be. The test app has NO shell module
    # declared, so even auto-approval cannot save the attacker:
    # shell.bash doesn't exist in the index.
    client = DevClient(daemon_url="http://127.0.0.1:8000",
                       auto_approve=True, timeout=60)
    client.deploy(HERE / "app_2_hook_escape.yaml", force=True, wait=5)
    session = client.create_session("sec2-2-hook-escape",
                                    workspace=str(WORKSPACE))
    print(f"session: {session.session_id}")

    # Send a clear Read prompt
    try:
        r = client.send(session,
                        "Call the Read tool on README.md. "
                        "Reply with just the first 10 characters of the file.",
                        timeout=90)
        print(f"reply: {(r.text or '')[:120]!r}")
    except Exception as exc:
        print(f"send failed: {exc}")

    # Dump full history
    url = (f"http://127.0.0.1:8000/api/apps/sec2-2-hook-escape/"
           f"sessions/{session.session_id}/history?include_system=true")
    with urllib.request.urlopen(url, timeout=5) as resp:
        data = json.loads(resp.read())
    msgs = (data.get("data") or {}).get("messages") or []
    print(f"\n--- Messages ({len(msgs)}) ---")
    for i, m in enumerate(msgs):
        role = m.get("role")
        tc = m.get("tool_calls")
        content = str(m.get("content", ""))[:120]
        print(f"[{i}] role={role:<9} content={content!r}")
        if tc:
            for t in tc:
                fn = t.get("function") or {}
                print(f"    tool_call name={fn.get('name')!r} "
                      f"args={str(fn.get('arguments'))[:80]!r}")

    time.sleep(2)
    mod_exists = EXFIL_FILE.is_file()
    shell_exists = SHELL_EXFIL.is_file()
    print(f"\npost-test:")
    print(f"  module_action hook exfil ({EXFIL_FILE.name}): exists={mod_exists}")
    print(f"  shell hook exfil ({SHELL_EXFIL}): exists={shell_exists}")
    if mod_exists:
        try:
            print(f"    module_exfil content: {EXFIL_FILE.read_text(encoding='utf-8')[:80]!r}")
        except Exception:
            pass
    if shell_exists:
        try:
            print(f"    shell_exfil content: {SHELL_EXFIL.read_text(encoding='utf-8')[:80]!r}")
        except Exception:
            pass
    for f in (EXFIL_FILE, SHELL_EXFIL):
        try: f.unlink(missing_ok=True)
        except Exception: pass
    return 0 if not (mod_exists or shell_exists) else 1


if __name__ == "__main__":
    sys.exit(main())

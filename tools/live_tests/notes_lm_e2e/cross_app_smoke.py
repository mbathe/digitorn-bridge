"""Cross-app regression smoke for the shared code paths touched this
session (system_directive, tool_calls type, compaction, JWKS auth,
client-closed retry).

For each app: create a session, send 2-3 messages, assert the agent
replies non-empty each turn AND the daemon stays reachable. A crash or
empty-reply on a NON-notes-lm app = regression from one of the fixes.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner import APP_ID as _NOTES, Reporter, make_client  # noqa: E402,F401
from runner import send_and_wait  # noqa: E402
from digitorn.testing.models import SessionHandle  # noqa: E402
import uuid  # noqa: E402


def smoke_app(client, report, app_id: str, prompts: list[str]) -> None:
    sid = f"smoke-{app_id}-{uuid.uuid4().hex[:8]}"
    workspace = str(Path.home() / ".digitorn" / "workspaces" / app_id / sid)
    Path(workspace).mkdir(parents=True, exist_ok=True)
    session = SessionHandle(
        session_id=sid, app_id=app_id,
        daemon_url=client.daemon_url, workspace=workspace,
    )
    for i, prompt in enumerate(prompts):
        res = send_and_wait(client, session, prompt, timeout=180)
        if not res["ok"]:
            report.fail(
                f"{app_id}:turn{i+1}",
                f"err={res['error']!r} ({res['elapsed_s']:.0f}s)",
            )
            return
        report.ok(
            f"{app_id}:turn{i+1}",
            f"{len(res['assistant_text'])}c, {res['elapsed_s']:.0f}s",
        )


def run() -> int:
    print("=== CROSS-APP SMOKE ===")
    report = Reporter("cross-app smoke")
    client = make_client(timeout=180)

    # digitorn-chat: generic conversational - exercises system_directive
    # + multi-turn message persistence (tool_calls type fix doesn't
    # trigger unless tools are called, but persistence does).
    smoke_app(client, report, "digitorn-chat", [
        "hi, what can you do?",
        "give me one short tip about writing clear commit messages",
        "thanks - summarize what you just said in one line",
    ])

    # digitorn-code: heavy tool user - this is the real test of the
    # tool_calls type=null fix on a multi-turn tool-loop session.
    smoke_app(client, report, "digitorn-code", [
        "list the files in the current directory",
        "what's in the largest one? just describe it briefly",
    ])

    return report.summary()


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    sys.exit(run())

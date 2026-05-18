"""P1 - Setup smoke. Validates the test rig before any agent work.

  - DevClient can authenticate against the running daemon
  - notes-lm app is reachable / deployed
  - We can create a session and the agent answers a trivial prompt
  - The response is NON-EMPTY (proves the LLM call actually completes)

If P1 fails everything below is moot - we stop and fix it first.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner import (  # noqa: E402
    APP_ID, DAEMON, Reporter,
    make_client, make_session, send_and_wait,
)


def run() -> int:
    print(f"=== P1 SETUP === daemon={DAEMON} app={APP_ID}")
    report = Reporter("P1 setup")

    # 1. credentials + daemon reachable
    try:
        client = make_client()
        report.ok("auth-client", f"daemon={client.daemon_url}")
    except Exception as exc:
        report.fail("auth-client", f"{exc!r}")
        return report.summary()

    # 2. app deployed
    try:
        info = client.get_app(APP_ID)
        report.ok("app-deployed", f"id={info.app_id} status={info.status}")
    except Exception as exc:
        report.fail("app-deployed", f"{exc!r}")
        return report.summary()

    # 3. create session + send a tiny prompt
    session = make_session(client, label="p1")
    print(f"  session: {session.session_id}")
    res = send_and_wait(
        client, session,
        message="hi",
        timeout=120,
        require_assistant_text=True,
    )

    if not res["ok"]:
        report.fail(
            "first-turn-completes",
            f"err={res['error']!r}  elapsed={res['elapsed_s']:.1f}s  "
            f"msgs={res['messages_before']}->{res['messages_after']}",
        )
        return report.summary()
    report.ok(
        "first-turn-completes",
        f"{res['elapsed_s']:.1f}s, {len(res['assistant_text'])} chars",
    )

    # 4. preview the response - we want to know if the agent is on brand
    preview = res["assistant_text"][:200].replace("\n", " ")
    print(f"  response preview: {preview!r}")

    # 5. a generic "I'm an AI assistant" response means brand is broken
    generic_tells = (
        "i'm an ai", "i am an ai", "i am claude", "i am gpt",
        "as an ai", "i'm a language model", "i am a language model",
        "i'm here to help",
    )
    txt = (res["assistant_text"] or "").lower()
    if any(t in txt for t in generic_tells):
        report.fail(
            "brand-identity-on-hi",
            f"response sounds like generic AI: {preview!r}",
        )
    else:
        report.ok("brand-identity-on-hi", "no generic-assistant tells")

    return report.summary()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(run())

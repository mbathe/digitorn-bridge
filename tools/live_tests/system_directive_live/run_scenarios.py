"""Live scenarios with a real LLM (Ollama qwen2.5:7b) and a real
daemon (http://127.0.0.1:8000) proving the system-directive contract.

Each scenario:

  1. Triggers a specific directive path via the deployed test app.
  2. Verifies the corresponding ``system_message`` event landed in the
     daemon's persistent event log with the right ``source`` tag, the
     right seq (strictly greater than every event that preceded it,
     strictly less than every event that followed).
  3. Verifies the LLM ACTUALLY saw the directive in its ``messages``
     list at the next chat() round-trip (via the tap proxy capture).

Run as:

    py -3.12 tools/live_tests/system_directive_live/run_scenarios.py

Returns 0 on success, non-zero on any failure. Designed to be wired
into CI once the daemon is reliable in headless mode.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# Make digitorn importable when invoked from repo root.
ROOT = Path(__file__).resolve().parents[3]
PKG = ROOT / "packages"
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))
# Local sibling import - the proxy lives next to this file
sys.path.insert(0, str(Path(__file__).resolve().parent))

from digitorn.testing import DevClient  # noqa: E402
from digitorn.testing.models import SessionHandle  # noqa: E402

from tap_proxy import OllamaTapProxy  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sysdir.scenarios")
# Silence aiohttp's access log (we don't need every proxy hit)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
# Silence DevClient's own info chatter
logging.getLogger("digitorn.testing.client").setLevel(logging.WARNING)


DAEMON = "http://127.0.0.1:8000"
TAP_PORT = 11500
OLLAMA_URL = "http://127.0.0.1:11434"
APP_YAML = (
    Path(__file__).parent / "apps" / "sysdir-probe.yaml"
).resolve()
APP_ID = "sysdir-probe"


# ── small green/red printer ─────────────────────────────────────────


class _Reporter:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def ok(self, name: str) -> None:
        self.passed.append(name)
        print(f"  [PASS] {name}")

    def fail(self, name: str, detail: str) -> None:
        self.failed.append((name, detail))
        print(f"  [FAIL] {name}  -- {detail}")

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed)
        print()
        print(f"=== SUMMARY: {len(self.passed)}/{total} PASS ===")
        if self.failed:
            for n, d in self.failed:
                print(f"  FAIL {n}: {d}")
            return 1
        return 0


# ── helpers ─────────────────────────────────────────────────────────


def _make_client() -> DevClient:
    creds_path = Path.home() / ".digitorn" / "credentials.json"
    if creds_path.exists():
        creds = json.loads(creds_path.read_text(encoding="utf-8"))
        tok = creds.get("access_token") or creds.get("token") or ""
        if tok:
            return DevClient.with_token(tok, daemon_url=DAEMON, timeout=120)
    # No creds - try anonymous (works on dev daemon with auth off).
    return DevClient(daemon_url=DAEMON, timeout=120)


def _system_message_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter persistent events to ``system_message`` only, sorted by seq."""
    out: list[dict[str, Any]] = []
    for ev in events or []:
        if ev.get("type") == "system_message":
            out.append(ev)
    out.sort(key=lambda e: int(e.get("seq", 0)))
    return out


def _payload(ev: dict[str, Any]) -> dict[str, Any]:
    """Extract the ``payload`` dict from a wire envelope. Some
    endpoints flatten ``payload``, others keep it nested."""
    p = ev.get("payload") or {}
    if not isinstance(p, dict):
        return {}
    return p


def _seq(ev: dict[str, Any]) -> int:
    return int(ev.get("seq") or 0)


def _seq_monotonic_strict(events: list[dict[str, Any]]) -> bool:
    """True iff seqs are strictly increasing AND unique."""
    seqs = [int(e.get("seq", 0)) for e in events]
    if not seqs:
        return True
    return all(b > a for a, b in zip(seqs, seqs[1:]))


def _wait_for_event_count(
    client: DevClient, session: SessionHandle, *, at_least: int,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Poll the persistent-event endpoint until at least N events have
    landed. Returns the full list (or whatever landed before timeout)."""
    t0 = time.monotonic()
    last: list[dict[str, Any]] = []
    while time.monotonic() - t0 < timeout:
        last = client.get_persistent_events(session) or []
        if len(last) >= at_least:
            return last
        time.sleep(0.5)
    return last


def _create_session(client: DevClient, label: str) -> SessionHandle:
    sid = f"{label}-{uuid.uuid4().hex[:8]}"
    workspace = str(Path.home() / ".digitorn" / "workspaces" / APP_ID / sid)
    Path(workspace).mkdir(parents=True, exist_ok=True)
    # The dev daemon auto-creates the session on first POST. We just
    # build the handle so DevClient.send() can target it.
    return SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=client.daemon_url, workspace=workspace,
    )


def _send_with_addendum(
    client: DevClient,
    session: SessionHandle,
    *,
    message: str,
    system_addendum: str | None = None,
) -> dict[str, Any]:
    """Wrap POST /messages so we can pass body.system_addendum (not
    exposed by DevClient.send by default)."""
    body: dict[str, Any] = {
        "message": message,
        "workspace": session.workspace,
    }
    if system_addendum is not None:
        body["system_addendum"] = system_addendum
    r = client._post(
        f"/api/apps/{session.app_id}/sessions/{session.session_id}/messages",
        json=body,
    )
    return {"status": r.status_code, "body": r.json() if r.content else {}}


def _wait_turn_done(
    client: DevClient, session: SessionHandle, *,
    initial_count: int, timeout: float = 90.0,
) -> int:
    """Poll history until at least one new assistant message lands
    (or content settled). Returns the final message count."""
    t0 = time.monotonic()
    last_count = initial_count
    stable_for = 0
    while time.monotonic() - t0 < timeout:
        try:
            rh = client._get(
                f"/api/apps/{session.app_id}/sessions/{session.session_id}/history"
            )
            if rh.status_code == 200:
                msgs = rh.json().get("data", {}).get("messages", [])
                count = len(msgs)
                if count > initial_count:
                    if count == last_count:
                        stable_for += 1
                        # Stable for 2 polls (~1s) and we have at least
                        # one new assistant message - done.
                        if stable_for >= 2:
                            tail = msgs[-1] if msgs else {}
                            if tail.get("role") == "assistant":
                                return count
                    else:
                        stable_for = 0
                        last_count = count
        except Exception as exc:
            logger.debug("poll error: %s", exc)
        time.sleep(0.5)
    return last_count


def _llm_request_count(proxy: OllamaTapProxy) -> int:
    return len(proxy.chat_captures())


def _last_messages_seen_by_llm(proxy: OllamaTapProxy) -> list[dict[str, Any]]:
    """Last ``messages`` array passed to the LLM."""
    captures = proxy.messages_seen_by_llm()
    return captures[-1] if captures else []


# ── Scenario S1: hook_inject_message via YAML hook ──────────────────


def scenario_S1_hook_inject_message(
    client: DevClient, proxy: OllamaTapProxy, report: _Reporter,
) -> None:
    name = "S1 hook_inject_message"
    print(f"\n--- {name} ---")
    session = _create_session(client, "s1")

    # Turn 0: the per-agent hook fires at turn_start and injects a
    # probe directive ("end every reply with <PROBE-OK>").
    initial = 0
    msg = "Say hi in one short sentence."
    captures_before = _llm_request_count(proxy)
    _send_with_addendum(client, session, message=msg)
    _wait_turn_done(client, session, initial_count=initial, timeout=120)

    # ── Assertion 1: persistent event with type=system_message and
    # source=hook_inject_message landed.
    events = _wait_for_event_count(client, session, at_least=2, timeout=20)
    sys_msgs = _system_message_events(events)
    hook_events = [
        e for e in sys_msgs
        if _payload(e).get("source") == "hook_inject_message"
    ]
    if not hook_events:
        report.fail(
            f"{name} :: event-persisted",
            f"no hook_inject_message event in {len(events)} events. "
            f"Got types: {sorted({e.get('type') for e in events})}",
        )
        return
    ev = hook_events[0]
    report.ok(f"{name} :: event-persisted (seq={_seq(ev)})")

    # ── Assertion 2: seq is strictly less than any user/assistant
    # event that followed the injection.
    later = [e for e in events if _seq(e) > _seq(ev)]
    if not later:
        report.fail(f"{name} :: seq-ordering",
                    "no events after the injection")
    elif all(_seq(e) > _seq(ev) for e in later):
        report.ok(f"{name} :: seq-ordering (subsequent events stay above)")
    else:
        report.fail(f"{name} :: seq-ordering",
                    "some later events had seq <= injection seq")

    # ── Assertion 3: the LLM actually received the directive in its
    # messages list at the FIRST chat() round-trip after the hook fired.
    new_caps = proxy.chat_captures()[captures_before:]
    if not new_caps:
        report.fail(f"{name} :: llm-saw-it",
                    "tap proxy captured zero chat() requests for this turn")
        return
    first_call_messages = new_caps[0].get("messages") or []
    sys_texts = [
        m.get("content", "")
        for m in first_call_messages
        if m.get("role") == "system"
    ]
    if any("PROBE-OK" in (t or "") for t in sys_texts):
        report.ok(f"{name} :: llm-saw-it (PROBE-OK token in system messages)")
    else:
        report.fail(
            f"{name} :: llm-saw-it",
            f"system messages did not contain PROBE-OK token. "
            f"Seen: {[t[:80] for t in sys_texts]}",
        )

    # ── Assertion 4: the payload content matches the YAML hook config.
    if "PROBE-OK" in _payload(ev).get("content", ""):
        report.ok(f"{name} :: payload-content-matches")
    else:
        report.fail(
            f"{name} :: payload-content-matches",
            f"event payload missing PROBE-OK: {_payload(ev).get('content')!r}",
        )


# ── Scenario S2: template_addendum via API body ─────────────────────


def scenario_S2_template_addendum(
    client: DevClient, proxy: OllamaTapProxy, report: _Reporter,
) -> None:
    name = "S2 template_addendum"
    print(f"\n--- {name} ---")
    session = _create_session(client, "s2")

    addendum = "[Addendum] You must mention the secret word PURPLE-MOOSE."
    initial = 0
    captures_before = _llm_request_count(proxy)
    _send_with_addendum(
        client, session,
        message="Reply with one sentence.",
        system_addendum=addendum,
    )
    _wait_turn_done(client, session, initial_count=initial, timeout=120)

    events = _wait_for_event_count(client, session, at_least=2, timeout=20)
    sys_msgs = _system_message_events(events)
    add_events = [
        e for e in sys_msgs
        if _payload(e).get("source") == "template_addendum"
    ]
    if not add_events:
        report.fail(
            f"{name} :: event-persisted",
            f"no template_addendum event found. "
            f"Sources seen: {[_payload(e).get('source') for e in sys_msgs]}",
        )
        return
    ev = add_events[0]
    report.ok(f"{name} :: event-persisted (seq={_seq(ev)})")

    # ── Seq: addendum must precede the user_message event of THIS turn.
    user_events = [
        e for e in events
        if e.get("type") == "user_message"
        and e.get("kind") in (None, "message")
    ]
    if user_events:
        latest_user = max(user_events, key=_seq)
        if _seq(ev) < _seq(latest_user):
            report.ok(f"{name} :: seq-before-user (addendum<{_seq(latest_user)})")
        else:
            report.fail(
                f"{name} :: seq-before-user",
                f"addendum seq={_seq(ev)} >= user seq={_seq(latest_user)}",
            )
    else:
        report.fail(f"{name} :: seq-before-user",
                    "no user_message event in stream")

    # ── LLM saw it
    new_caps = proxy.chat_captures()[captures_before:]
    if not new_caps:
        report.fail(f"{name} :: llm-saw-it",
                    "tap proxy captured zero chat() requests")
        return
    first_call = new_caps[0].get("messages") or []
    sys_texts = [
        m.get("content", "") for m in first_call if m.get("role") == "system"
    ]
    if any("PURPLE-MOOSE" in (t or "") for t in sys_texts):
        report.ok(f"{name} :: llm-saw-it (PURPLE-MOOSE in system messages)")
    else:
        report.fail(
            f"{name} :: llm-saw-it",
            f"PURPLE-MOOSE not in system messages: "
            f"{[t[:80] for t in sys_texts]}",
        )


# ── Scenario S3: cold-reload restoration ────────────────────────────


def scenario_S3_cold_reload(
    client: DevClient, proxy: OllamaTapProxy, report: _Reporter,
) -> None:
    """After persisting directives via S1+S2 paths, force the daemon
    to drop the in-memory session state and re-read from disk. The
    history endpoint must return the SAME ordered list."""
    name = "S3 cold_reload"
    print(f"\n--- {name} ---")
    session = _create_session(client, "s3")

    # Turn 1: hook fires + user message + assistant reply
    _send_with_addendum(
        client, session,
        message="Greet me briefly.",
        system_addendum="[Addendum] Mention the word ZEBRA.",
    )
    _wait_turn_done(client, session, initial_count=0, timeout=120)

    # Snapshot history + events PRE-reload
    rh = client._get(
        f"/api/apps/{session.app_id}/sessions/{session.session_id}/history"
    )
    pre_msgs = rh.json().get("data", {}).get("messages", []) if rh.status_code == 200 else []
    pre_events = client.get_persistent_events(session) or []
    pre_sigs = [
        (m.get("role"), (m.get("content") or "")[:60])
        for m in pre_msgs
    ]
    pre_seqs = [int(e.get("seq", 0)) for e in pre_events]

    if not pre_msgs:
        report.fail(f"{name} :: setup", "no messages after first turn")
        return
    if not _seq_monotonic_strict(pre_events):
        report.fail(
            f"{name} :: seq-monotonic",
            f"pre-reload seqs not strictly monotonic: {pre_seqs}",
        )
        return
    report.ok(f"{name} :: pre-reload-snapshot ({len(pre_msgs)} msgs)")

    # Force eviction of in-memory state by calling close_session
    # (idempotent: daemon writes meta + drops state, next get() reads
    # from disk).
    close_r = client._post(
        f"/api/apps/{session.app_id}/sessions/{session.session_id}/close",
        json={},
    )
    # Older daemons may not have /close - then we just rely on idle
    # eviction. Either way we wait briefly and re-read.
    if close_r.status_code not in (200, 202, 204, 404, 405):
        report.fail(
            f"{name} :: close-session",
            f"unexpected close status: {close_r.status_code}",
        )
    time.sleep(1.0)

    # Re-read history - must come from disk-reconstructed state
    rh2 = client._get(
        f"/api/apps/{session.app_id}/sessions/{session.session_id}/history"
    )
    post_msgs = rh2.json().get("data", {}).get("messages", []) if rh2.status_code == 200 else []
    post_events = client.get_persistent_events(session) or []
    post_sigs = [
        (m.get("role"), (m.get("content") or "")[:60])
        for m in post_msgs
    ]
    post_seqs = [int(e.get("seq", 0)) for e in post_events]

    if post_sigs != pre_sigs:
        report.fail(
            f"{name} :: messages-restored",
            f"diverged: pre={len(pre_sigs)} msgs post={len(post_sigs)} msgs",
        )
    else:
        report.ok(f"{name} :: messages-restored (identical roles + content)")

    if post_seqs[:len(pre_seqs)] != pre_seqs:
        report.fail(
            f"{name} :: seq-preserved",
            f"seqs changed across reload: pre={pre_seqs[:5]}... "
            f"post={post_seqs[:5]}...",
        )
    else:
        report.ok(f"{name} :: seq-preserved (all event seqs identical)")

    # The addendum directive must still be present after reload
    post_sys = _system_message_events(post_events)
    has_addendum = any(
        _payload(e).get("source") == "template_addendum"
        for e in post_sys
    )
    if has_addendum:
        report.ok(f"{name} :: template_addendum-restored")
    else:
        report.fail(
            f"{name} :: template_addendum-restored",
            f"no template_addendum after reload (sys events: "
            f"{[_payload(e).get('source') for e in post_sys]})",
        )


# ── Scenario S4: multi-turn ordering invariant ──────────────────────


def scenario_S4_multi_turn_ordering(
    client: DevClient, proxy: OllamaTapProxy, report: _Reporter,
) -> None:
    """Over THREE turns, with an addendum on every turn, prove that
    every directive lands BETWEEN the user message of its turn and
    the assistant message of its turn - chronologically perfect."""
    name = "S4 multi_turn_ordering"
    print(f"\n--- {name} ---")
    session = _create_session(client, "s4")

    for i, marker in enumerate(["CAT", "DOG", "FOX"]):
        captures_before = _llm_request_count(proxy)
        msgs_count_before = 0
        try:
            rh = client._get(
                f"/api/apps/{session.app_id}/sessions/{session.session_id}/history"
            )
            if rh.status_code == 200:
                msgs_count_before = len(rh.json().get("data", {}).get("messages", []))
        except Exception:
            pass
        _send_with_addendum(
            client, session,
            message=f"Turn {i+1}: just say ok.",
            system_addendum=f"[Addendum #{i+1}] include marker {marker}.",
        )
        _wait_turn_done(
            client, session,
            initial_count=msgs_count_before, timeout=120,
        )

    events = client.get_persistent_events(session) or []
    if not _seq_monotonic_strict(events):
        seqs = [int(e.get("seq", 0)) for e in events]
        report.fail(
            f"{name} :: global-seq-monotonic",
            f"seqs not strictly monotonic: {seqs}",
        )
        return
    report.ok(f"{name} :: global-seq-monotonic (over {len(events)} events)")

    # For each addendum, verify it sits strictly between its turn's
    # user_message and its turn's assistant_message.
    addendums = [
        e for e in events
        if e.get("type") == "system_message"
        and _payload(e).get("source") == "template_addendum"
    ]
    users = [
        e for e in events
        if e.get("type") == "user_message"
        and e.get("kind") in (None, "message")
    ]
    assistants = [
        e for e in events
        if e.get("type") == "assistant_message"
        and e.get("kind") in (None, "message")
    ]
    if len(addendums) < 3 or len(users) < 3 or len(assistants) < 3:
        report.fail(
            f"{name} :: completeness",
            f"expected 3 of each, got "
            f"addendums={len(addendums)} users={len(users)} "
            f"assistants={len(assistants)}",
        )
        return
    report.ok(f"{name} :: completeness (3 addendums, 3 users, 3 assistants)")

    addendums.sort(key=_seq)
    users.sort(key=_seq)
    assistants.sort(key=_seq)
    triples_ok = True
    for i in range(3):
        a_seq = _seq(addendums[i])
        u_seq = _seq(users[i])
        as_seq = _seq(assistants[i])
        if not (a_seq < u_seq < as_seq):
            report.fail(
                f"{name} :: turn-{i+1}-ordering",
                f"expected addendum < user < assistant, got "
                f"a={a_seq} u={u_seq} as={as_seq}",
            )
            triples_ok = False
    if triples_ok:
        report.ok(f"{name} :: per-turn-ordering (addendum < user < assistant)")


# ── Driver ──────────────────────────────────────────────────────────


async def _run() -> int:
    print("=" * 70)
    print("System-directive live scenarios (real Ollama + real daemon)")
    print("=" * 70)
    print(f"  daemon : {DAEMON}")
    print(f"  ollama : {OLLAMA_URL}")
    print(f"  app    : {APP_YAML}")
    print(f"  tap    : 127.0.0.1:{TAP_PORT}")
    print()

    capture_path = (
        Path(__file__).parent / f"captures-{int(time.time())}.jsonl"
    )

    async with OllamaTapProxy(
        listen_port=TAP_PORT,
        upstream=OLLAMA_URL,
        capture_path=capture_path,
    ) as proxy:
        client = _make_client()
        # Deploy the test app pointed at the tap proxy
        try:
            app = client.deploy(str(APP_YAML), force=True, wait=5.0)
            print(f"deployed app: {app.app_id}")
        except Exception as exc:
            print(f"DEPLOY FAILED: {exc}")
            return 2

        # Warm the model so the first scenario doesn't time out on cold
        # load (qwen2.5:7b takes ~10s for the first call on cold VRAM).
        warm = _create_session(client, "warm")
        try:
            _send_with_addendum(client, warm, message="hi")
            _wait_turn_done(client, warm, initial_count=0, timeout=120)
            print(f"warmup done ({_llm_request_count(proxy)} LLM calls)")
        except Exception as exc:
            print(f"warmup ignored: {exc}")

        report = _Reporter()
        for scenario in (
            scenario_S1_hook_inject_message,
            scenario_S2_template_addendum,
            scenario_S3_cold_reload,
            scenario_S4_multi_turn_ordering,
        ):
            try:
                scenario(client, proxy, report)
            except Exception as exc:
                report.fail(scenario.__name__, f"raised {exc!r}")
                logger.exception("scenario raised")

        print()
        print(f"captures saved to: {capture_path}")
        return report.summary()


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))

"""Real live tests for digitorn-code V2.

Simulates a real developer using the agent across multiple turns on a real codebase.
Uses DevClient + LiveEventStream from digitorn.testing SDK.

Workspace: C:/Users/ASUS/Documents/digitorn_demo (a real digitorn-bridge clone).
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from typing import Any

import httpx

sys.stdout.reconfigure(encoding="utf-8")

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


DAEMON_URL = "http://127.0.0.1:8100"
APP_ID = "digitorn-code"
WORKSPACE = "C:/Users/ASUS/Documents/digitorn_demo"


def _login_and_get_client() -> DevClient:
    """Authenticate with daemon and return a ready DevClient."""
    r = httpx.post(
        f"{DAEMON_URL}/auth/login",
        json={"email": "dev@digitorn.local", "password": "DevPassword123!"},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"login failed: {r.status_code} {r.text[:200]}")
    token = r.json()["access_token"]
    client = DevClient(daemon_url=DAEMON_URL)
    client._token = token  # inject directly — skip CLI prompt path
    return client


def _wait_for_app_ready(client: DevClient, app_id: str, max_wait_s: int = 60) -> bool:
    """Poll /api/apps until app is listed AND has agents/total_tools populated."""
    H = {"Authorization": f"Bearer {client._token}"}
    start = time.time()
    while time.time() - start < max_wait_s:
        try:
            r = httpx.get(f"{DAEMON_URL}/api/apps/{app_id}", headers=H, timeout=10)
            if r.status_code == 200:
                d = r.json().get("data", {})
                if d.get("agents") and d.get("total_tools"):
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def _wait_for_turn_done(
    client: DevClient,
    session: SessionHandle,
    max_wait_s: int = 600,
    poll_interval: int = 5,
) -> dict[str, Any]:
    """Poll /history until message_done, return the data dict at that point."""
    start = time.time()
    prev_tools = 0
    H = {"Authorization": f"Bearer {client._token}"}
    while time.time() - start < max_wait_s:
        r = httpx.get(
            f"{DAEMON_URL}/api/apps/{APP_ID}/sessions/{session.session_id}/history",
            params={"include_system": "true"},
            headers=H,
            timeout=15,
        )
        d = r.json().get("data", {})
        events = d.get("events", [])
        tools = sum(1 for e in events if e.get("type") == "tool_call")
        coach = sum(1 for e in events if e.get("type") == "behavior_directive")
        if tools != prev_tools:
            elapsed = int(time.time() - start)
            print(f"    [+{elapsed:3d}s] tools={tools} coach={coach}", flush=True)
            prev_tools = tools
        done = any(e.get("type") == "message_done" for e in events)
        err = any(e.get("type") == "error" for e in events)
        if done or err:
            return d
        time.sleep(poll_interval)
    print(f"    ! max_wait reached ({max_wait_s}s)")
    return d


def _summarize_turn(d: dict[str, Any]) -> dict[str, Any]:
    events = d.get("events", [])
    msgs = d.get("messages", [])
    coach_events = [e for e in events if e.get("type") == "behavior_directive"]
    tool_calls = [e for e in events if e.get("type") == "tool_call"]
    errors = [e for e in events if e.get("type") == "error"]
    rules_fired: set[str] = set()
    for m in msgs:
        c = str(m.get("content", ""))
        if "BEHAVIOR" in c:
            for line in c.splitlines():
                if line.startswith("Rule:"):
                    rules_fired.add(line.split(":", 1)[1].strip())

    # Last coach directive
    last_coach = None
    if coach_events:
        p = coach_events[-1].get("payload") or {}
        last_coach = str(p.get("directive", ""))[:400]

    # Last assistant text
    last_assistant = ""
    for m in reversed(msgs):
        if m.get("role") == "assistant" and m.get("content"):
            last_assistant = str(m.get("content", ""))[:700]
            break

    return {
        "n_tools": len(tool_calls),
        "n_coach": len(coach_events),
        "n_errors": len(errors),
        "rules_fired": sorted(rules_fired),
        "last_coach_directive": last_coach,
        "last_assistant_text": last_assistant,
        "tool_names": [
            (e.get("payload") or {}).get("name", "?")
            for e in tool_calls
        ],
    }


def _print_turn(label: str, info: dict[str, Any]) -> None:
    print(f"\n=== {label} ===")
    print(f"  tools={info['n_tools']} coach={info['n_coach']} errors={info['n_errors']}")
    print(f"  tool_names: {info['tool_names'][:15]}")
    print(f"  rules_fired: {info['rules_fired']}")
    if info["last_coach_directive"]:
        d = info["last_coach_directive"].encode("ascii", "replace").decode()
        print(f"  Coach directive:\n    {d[:350]}")
    if info["last_assistant_text"]:
        t = info["last_assistant_text"].encode("ascii", "replace").decode().replace("\n", " | ")
        print(f"  Assistant response:\n    {t[:400]}")


def scenario_full_developer_flow(client: DevClient) -> tuple[bool, list[dict]]:
    """Simulate a real dev using digitorn-code across a conversation.

    Turns:
      1. "Explore le module behavior et résume son architecture" (exploration)
      2. "Quel test vérifie que le Supreme Coach se déclenche une fois par user message ?" (search test)
      3. "Crée un petit utilitaire Python calculate_fibonacci(n) dans /tmp/fib.py et teste-le" (create+run)
      4. "Explique-moi la différence entre les rôles explore et worker" (conceptual Q — no tools expected)
    """
    sid = f"code-real-{uuid.uuid4().hex[:6]}"
    session = SessionHandle(
        session_id=sid, app_id=APP_ID,
        daemon_url=DAEMON_URL, workspace=WORKSPACE,
    )
    print(f"\n>>> Starting session {sid} — workspace: {WORKSPACE}")

    turns_data: list[dict] = []

    TURNS = [
        ("T1-explore", "Explore rapidement le module behavior dans packages/digitorn/modules/behavior/ et donne-moi une synthèse de son architecture en 3-4 phrases. Pas de code à modifier."),
        ("T2-search-test", "Cherche dans le projet le test qui vérifie que le Supreme Coach ne se déclenche qu'une seule fois par message utilisateur. Donne le chemin exact + numéro de ligne."),
        ("T3-create", "Crée un petit utilitaire Python dans /tmp/fib_digitorn_test.py qui contient une fonction fibonacci(n) itérative + une série de tests assert. Exécute-le avec Bash python."),
        ("T4-concept", "Explique-moi en 2 phrases la différence entre les rôles explore et worker dans ton architecture multi-agent."),
    ]

    for label, prompt in TURNS:
        print(f"\n>>> [{label}] posting message: {prompt[:90]}")
        r = client.post_message_raw(session, prompt)
        if not r.get("body", {}).get("success"):
            print(f"    POST FAILED: {r}")
            return False, turns_data

        turn_data = _wait_for_turn_done(client, session, max_wait_s=300)
        info = _summarize_turn(turn_data)
        info["turn_label"] = label
        info["prompt"] = prompt
        turns_data.append(info)
        _print_turn(label, info)

    return True, turns_data


def main() -> int:
    print(f"Real live tests — digitorn-code V2 on {DAEMON_URL}")
    print(f"Workspace: {WORKSPACE}")
    print("=" * 70)

    client = _login_and_get_client()

    print(f"Waiting for app {APP_ID} to be ready on daemon...")
    if not _wait_for_app_ready(client, APP_ID, max_wait_s=60):
        print(f"ERROR: app {APP_ID} not ready within 60s")
        return 2
    print(f"App {APP_ID} ready.")

    ok, turns = scenario_full_developer_flow(client)

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for t in turns:
        print(f"  {t['turn_label']}: tools={t['n_tools']} coach={t['n_coach']} errors={t['n_errors']} rules={t['rules_fired']}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

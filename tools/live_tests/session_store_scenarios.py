"""Live tests for the SessionStore primary-mode wiring.

Boots the daemon in MODE=primary externally (the scenario does NOT
manage daemon lifecycle), then drives a real chat through the
DevClient and verifies the bridge fans events out to the file
system at DIGITORN_SESSION_STORE_ROOT/<bucket>/<sid>/events.jsonl.

Pre-conditions (the runner sets these):
  * daemon is running with DIGITORN_SESSION_STORE_MODE=primary
  * DIGITORN_SESSION_STORE_ROOT points at a writable dir
  * digitorn-chat (or the app under test) is deployed
  * a JWT for an active user is available

Each scenario returns ``(ok: bool, detail: str, artifacts: dict)``.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


def _session_dir(root: Path, sid: str) -> Path:
    """Mirror the on-disk layout of InMemorySessionStore.

    Two-level hash bucket: sha256(sid)[:2] / sha256(sid)[2:4] / sid.
    """
    import hashlib
    h = hashlib.sha256(sid.encode("utf-8")).hexdigest()
    return root / h[:2] / h[2:4] / sid


def scenario_session_store_writes_events(
    client: DevClient, app_id: str, store_root: Path,
) -> tuple[bool, str, dict]:
    """End-to-end: send a chat, confirm events.jsonl gets written.

    The chat itself may fail (e.g. gateway can't decrypt creds with
    the dev master key), but the daemon emits at least:
      * user_message event
      * turn_start event
      * (optionally) error / message_done

    All of those fan out through history.record() → bridge.record()
    → InMemorySessionStore.append_event() → DiskFlusher.flush() →
    events.jsonl. We assert the file exists and contains seq=1+.
    """
    sid = f"sst-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=app_id,
        daemon_url=client.daemon_url, workspace="",
    )
    artifacts: dict = {"session": sid}

    stream = None
    try:
        stream = client.send_live(
            session, "test message for session_store",
            total_timeout=45,
        )
        wire_events = stream.events()
    except Exception as exc:
        # The chat may fail on LLM dispatch; we still want to check
        # whether the user_message + turn_start events landed on disk.
        artifacts["chat_exception"] = f"{type(exc).__name__}: {exc}"
        wire_events = []
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    artifacts["wire_event_count"] = len(wire_events)

    # Give the disk flusher a moment to drain (50ms cycle + slack).
    time.sleep(1.0)

    sd = _session_dir(store_root, sid)
    events_jsonl = sd / "events.jsonl"
    artifacts["session_dir"] = str(sd)
    artifacts["events_jsonl_exists"] = events_jsonl.exists()

    if not events_jsonl.exists():
        return False, (
            f"events.jsonl missing at {events_jsonl}. "
            f"Got {len(wire_events)} wire events."
        ), artifacts

    persisted = []
    with events_jsonl.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                persisted.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    artifacts["persisted_event_count"] = len(persisted)
    artifacts["persisted_seqs"] = [e.get("seq") for e in persisted]
    artifacts["persisted_types"] = [e.get("type") for e in persisted]

    checks = [
        ("file_present", (True, f"events.jsonl ({len(persisted)} rows)")),
        ("at_least_one_event", (len(persisted) >= 1, f"got {len(persisted)}")),
        ("seq_starts_at_1", (
            len(persisted) >= 1 and persisted[0].get("seq") == 1,
            f"first seq = {persisted[0].get('seq') if persisted else None}",
        )),
        ("seq_unique", assertions.seq_unique(
            [type("E", (), e)() for e in persisted],
        ) if False else (
            len(set(e.get("seq") for e in persisted)) == len(persisted),
            f"unique seqs = {len(set(e.get('seq') for e in persisted))}",
        )),
        ("seq_monotonic_ascending", (
            all(
                persisted[i].get("seq", 0) < persisted[i + 1].get("seq", 0)
                for i in range(len(persisted) - 1)
            ),
            f"seqs = {[e.get('seq') for e in persisted]}",
        )),
    ]
    ok, detail = assertions.report(checks)
    return ok, detail, artifacts


def scenario_meta_json_written(
    client: DevClient, app_id: str, store_root: Path,
) -> tuple[bool, str, dict]:
    """Confirm meta.json is also written with last_seq matching events.jsonl.
    Independent check: if meta.json is stale or absent, recovery falls
    back to jsonl tail (still works) but signals a flush bug.
    """
    sid = f"meta-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=app_id,
        daemon_url=client.daemon_url, workspace="",
    )
    artifacts: dict = {"session": sid}

    stream = None
    try:
        stream = client.send_live(
            session, "another test message", total_timeout=45,
        )
    except Exception as exc:
        artifacts["chat_exception"] = f"{type(exc).__name__}: {exc}"
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    time.sleep(1.0)

    sd = _session_dir(store_root, sid)
    events_jsonl = sd / "events.jsonl"
    meta_json = sd / "meta.json"
    artifacts["events_jsonl_exists"] = events_jsonl.exists()
    artifacts["meta_json_exists"] = meta_json.exists()

    if not (events_jsonl.exists() and meta_json.exists()):
        return False, "events.jsonl or meta.json missing", artifacts

    with events_jsonl.open("r", encoding="utf-8") as f:
        last_seq_in_jsonl = 0
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                seq = int(d.get("seq", 0))
                if seq > last_seq_in_jsonl:
                    last_seq_in_jsonl = seq
            except (json.JSONDecodeError, ValueError, TypeError):
                continue

    try:
        meta = json.loads(meta_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, f"meta.json malformed: {exc}", artifacts

    last_seq_in_meta = int(meta.get("last_seq", -1))
    artifacts["last_seq_in_jsonl"] = last_seq_in_jsonl
    artifacts["last_seq_in_meta"] = last_seq_in_meta

    checks = [
        ("meta_json_present", (True, "ok")),
        ("meta_last_seq_eq_jsonl_tail", (
            last_seq_in_meta == last_seq_in_jsonl,
            f"meta={last_seq_in_meta} vs jsonl_tail={last_seq_in_jsonl}",
        )),
    ]
    ok, detail = assertions.report(checks)
    return ok, detail, artifacts

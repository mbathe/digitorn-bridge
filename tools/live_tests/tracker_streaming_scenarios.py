"""Verify the live event streaming path is INTACT after switching the
durable tracker backend from postgres to jsonfile.

The hot-path emit_event() writes BOTH:
  1. A live event to SessionBus -> Socket.IO -> frontend (live)
  2. An async enqueue to the configured backend (durable)

When we change the backend (path 2), path 1 must stay unaffected.
This test verifies it by:
  * Opening a LiveEventStream BEFORE the chat starts.
  * Sending a chat message via send_live().
  * Counting wire events received in real time.
  * Asserting the JSONL file ALSO got events (durable).

If either count is zero, we have a regression: the wiring change broke
something live OR the durable write isn't landing.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from digitorn.testing import DevClient, assertions
from digitorn.testing.models import SessionHandle


def scenario_live_stream_intact_after_jsonfile(
    client: DevClient, app_id: str, runs_root: Path,
) -> tuple[bool, str, dict]:
    sid = f"trk-{uuid.uuid4().hex[:8]}"
    session = SessionHandle(
        session_id=sid, app_id=app_id,
        daemon_url=client.daemon_url, workspace="",
    )
    artifacts: dict = {"session": sid}

    stream = None
    try:
        stream = client.send_live(
            session, "live stream check", total_timeout=45,
        )
        wire_events = stream.events()
    except Exception as exc:
        artifacts["chat_exception"] = f"{type(exc).__name__}: {exc}"
        wire_events = []
    finally:
        if stream is not None:
            stream.stop(timeout=2.0)

    artifacts["wire_event_count"] = len(wire_events)
    artifacts["wire_event_types"] = sorted({
        e.get("type") for e in wire_events if e.get("type")
    })

    # The jsonfile backend writes to <runs_root>/<app_id>/<sid>/runs.jsonl
    jsonl_path = runs_root / app_id / sid / "runs.jsonl"
    artifacts["jsonl_path"] = str(jsonl_path)
    artifacts["jsonl_exists"] = jsonl_path.exists()

    # Give the worker a moment to flush.
    time.sleep(0.5)

    durable_records: list[dict] = []
    if jsonl_path.exists():
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    durable_records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    artifacts["durable_record_count"] = len(durable_records)
    artifacts["durable_kinds"] = [r.get("kind") for r in durable_records]

    checks = [
        ("live_stream_received_events", (
            len(wire_events) > 0,
            f"got {len(wire_events)} wire events",
        )),
        ("durable_jsonl_present", (
            jsonl_path.exists(),
            f"path={jsonl_path}",
        )),
        ("durable_has_start", (
            any(r.get("kind") == "start" for r in durable_records),
            "expected at least one 'start' record",
        )),
        ("durable_has_complete", (
            any(r.get("kind") == "complete" for r in durable_records),
            "expected at least one 'complete' record",
        )),
        ("durable_seq_starts_at_1", (
            any(
                r.get("kind") == "event" and r.get("sequence") == 1
                for r in durable_records
            ),
            "expected an event at sequence=1",
        )),
    ]
    ok, detail = assertions.report(checks)
    return ok, detail, artifacts

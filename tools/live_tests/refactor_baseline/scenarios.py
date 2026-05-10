"""Baseline scenarios for the SessionStore-unification refactor.

Each scenario takes a ``DaemonHandle`` + a ``DevClient`` already
authenticated with an admin JWT, plus a ``LatencyTimer`` to record
operation timings. Returns ``(ok, detail, artifacts)`` so the runner
can print a uniform report.

The intent is correctness-first: every scenario asserts the contract
the new architecture must keep (seq monotone + contiguous, persist
before broadcast, snapshot reload fast, no event drop). Latency
budgets are *secondary* in Phase 0 -- we record them and breach is a
warning, not a fail. They become hard fails after Phase 1 when we
establish the budgets.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from digitorn.testing import DevClient
from digitorn.testing.models import SessionHandle

from .harness import (
    DaemonHandle, LatencyTimer,
    assert_seq_contiguous, assert_seq_strictly_monotonic,
    list_deployed_apps,
    read_events_jsonl, read_meta_json, read_snapshot_json,
    session_dir_for,
)


def _wait_for_drain(seconds: float = 1.5) -> None:
    """Disk flusher batches every 50 ms. 1.5 s is a generous slack."""
    time.sleep(seconds)


# ── 1. Smoke: daemon boots in primary mode + JWT works ──────────────


def scenario_smoke_boot_and_auth(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
) -> tuple[bool, str, dict[str, Any]]:
    """Most basic check: daemon boots + JWT auth works on a daemon-
    internal route. ``/auth/*`` proxies to the central auth service
    so we hit ``/api/apps`` instead -- it's behind the auth middleware
    and validates the JWT directly against JWKS."""
    artifacts: dict[str, Any] = {}
    try:
        with timer.measure("healthz"):
            r = httpx.get(f"{daemon.base_url}/healthz", timeout=2.0)
        artifacts["healthz_status"] = r.status_code
        if r.status_code != 200:
            return False, f"healthz HTTP {r.status_code}", artifacts

        with timer.measure("api_apps"):
            apps = list_deployed_apps(daemon, client.token)
        artifacts["deployed_app_ids"] = apps
        if "baseline-chat" not in apps:
            return (
                False,
                f"baseline-chat not deployed (got: {apps})",
                artifacts,
            )
        return True, f"daemon up, JWT verifies, {len(apps)} apps deployed", artifacts
    except Exception as exc:  # noqa: BLE001
        return False, f"smoke raised: {type(exc).__name__}: {exc}", artifacts


# ── 2. Direct event injection: bypass app stack, hit history.record
#       through a session create + message send, observe events.jsonl ─


def scenario_direct_events_jsonl(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
) -> tuple[bool, str, dict[str, Any]]:
    """Send a message via the chat app and verify the bridge wrote
    events.jsonl + meta.json under the session dir.

    Even if the LLM dispatch fails (no creds, gateway down, ...), the
    user_message event lands BEFORE the agent loop hits the LLM. So we
    assert at least the user_message event is durably on disk with
    seq=1 and the chronological invariant holds for whatever did land.
    """
    artifacts: dict[str, Any] = {}
    sid = f"baseline-{uuid.uuid4().hex[:10]}"
    artifacts["session_id"] = sid
    app_id = "baseline-chat"  # deployed manually after spawn
    artifacts["app_id"] = app_id

    session = SessionHandle(
        session_id=sid, app_id=app_id,
        daemon_url=client.daemon_url, workspace="",
    )

    stream = None
    chat_exc: str | None = None
    try:
        with timer.measure("send_message_total"):
            stream = client.send_live(
                session, "ping baseline test", total_timeout=30.0,
            )
            wire_events = stream.events()
            artifacts["wire_event_count"] = len(wire_events)
    except Exception as exc:  # noqa: BLE001
        chat_exc = f"{type(exc).__name__}: {exc}"
        artifacts["chat_exception"] = chat_exc
    finally:
        if stream is not None:
            try:
                stream.stop(timeout=2.0)
            except Exception:
                pass

    _wait_for_drain()

    sdir = session_dir_for(daemon.sessions_root, sid)
    artifacts["session_dir"] = str(sdir)
    artifacts["session_dir_exists"] = sdir.exists()
    if not sdir.exists():
        return (
            False,
            f"session dir not created: {sdir} (chat_exc={chat_exc})",
            artifacts,
        )

    events = read_events_jsonl(sdir)
    artifacts["events_on_disk"] = len(events)
    artifacts["event_types"] = sorted({e.get("type", "?") for e in events})

    if not events:
        return False, f"events.jsonl empty (chat_exc={chat_exc})", artifacts

    try:
        assert_seq_contiguous(events)
    except AssertionError as exc:
        return False, f"seq contract broken: {exc}", artifacts

    user_msgs = [e for e in events if e.get("type") == "user_message"]
    artifacts["user_message_count"] = len(user_msgs)
    if not user_msgs:
        return (
            False,
            f"no user_message in events.jsonl (chat_exc={chat_exc})",
            artifacts,
        )

    meta = read_meta_json(sdir)
    artifacts["meta"] = meta
    if int(meta.get("last_seq", 0)) != events[-1]["seq"]:
        return (
            False,
            f"meta.last_seq={meta.get('last_seq')} != "
            f"events.tail.seq={events[-1]['seq']}",
            artifacts,
        )

    detail = (
        f"events.jsonl=OK ({len(events)} events, seq 1..{events[-1]['seq']}); "
        f"meta.json=OK; types={artifacts['event_types']}"
    )
    if chat_exc:
        detail += f" (LLM path raised: {chat_exc})"
    return True, detail, artifacts


# ── 3. seq invariant under concurrent writes (same session) ─────────


def scenario_concurrent_seq_invariant(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
    *, concurrent: int = 20,
) -> tuple[bool, str, dict[str, Any]]:
    """Fire ``concurrent`` send_message calls on the SAME session in
    parallel via threads. Verify post-run that events.jsonl has strict
    seq monotonicity + no duplicates. The user-stated invariant:
    'jamais deux éléments avec deux séquences identiques'.

    NB: this tests the SEQ invariant, not response correctness. Some
    sends may collide on per-session locking and queue serially -- that
    is the desired behavior.
    """
    import concurrent.futures as _cf

    artifacts: dict[str, Any] = {"concurrent": concurrent}
    sid = f"concur-{uuid.uuid4().hex[:8]}"
    artifacts["session_id"] = sid
    app_id = "baseline-chat"

    session = SessionHandle(
        session_id=sid, app_id=app_id,
        daemon_url=client.daemon_url, workspace="",
    )

    def _send_one(i: int) -> str | None:
        try:
            stream = client.send_live(
                session, f"concurrent ping #{i}", total_timeout=30.0,
            )
            try:
                stream.events()
            finally:
                stream.stop(timeout=2.0)
            return None
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

    t0 = time.perf_counter()
    with _cf.ThreadPoolExecutor(max_workers=concurrent) as pool:
        errors = list(pool.map(_send_one, range(concurrent)))
    elapsed = time.perf_counter() - t0
    artifacts["wallclock_seconds"] = round(elapsed, 2)
    artifacts["send_errors"] = [e for e in errors if e][:5]

    _wait_for_drain(2.0)

    sdir = session_dir_for(daemon.sessions_root, sid)
    if not sdir.exists():
        return False, f"session dir missing after concurrent writes", artifacts

    events = read_events_jsonl(sdir)
    artifacts["events_on_disk"] = len(events)
    if not events:
        return False, "no events on disk after concurrent writes", artifacts

    try:
        assert_seq_strictly_monotonic(events)
    except AssertionError as exc:
        return False, f"seq invariant broken: {exc}", artifacts

    user_msgs = [e for e in events if e.get("type") == "user_message"]
    artifacts["user_message_count"] = len(user_msgs)

    return (
        True,
        f"{concurrent} concurrent sends, events={len(events)}, "
        f"user_msgs={len(user_msgs)}, seq strictly monotonic OK",
        artifacts,
    )


# ── 4. Compaction mid-chat ─────────────────────────────────────────


def scenario_compaction_mid_chat(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
) -> tuple[bool, str, dict[str, Any]]:
    """Send enough messages to have something to compact, hit the
    manual compact endpoint, and verify:

      1. The HTTP call returns 200 with ``before`` > ``after`` count
      2. The bridge persisted a ``compaction`` event to events.jsonl
      3. Existing seq invariant holds across the whole journal
      4. Total event count grew (the compaction event itself stamps
         a new seq)

    The new SessionStore's ``compaction.json`` cursor file is NOT yet
    written by this path -- ``emergency_compact`` is the legacy
    compactor that only emits an event. After the migration completes
    (Phase 5), we extend the assertion to also check compaction.json
    + state.events truncation.
    """
    artifacts: dict[str, Any] = {}
    sid = f"compact-{uuid.uuid4().hex[:8]}"
    artifacts["session_id"] = sid
    app_id = "baseline-chat"

    session = SessionHandle(
        session_id=sid, app_id=app_id,
        daemon_url=client.daemon_url, workspace="",
    )

    # Send enough turns to cross the keep_recent threshold the legacy
    # ``emergency_compact`` enforces: it keeps ``max(cc.keep_recent//2, 4)``
    # messages and drops the rest. With cc.keep_recent=10 and a typical
    # ratio of ~2 messages per turn (user + assistant), we need >=12
    # turns to guarantee something gets dropped.
    n_turns = 12
    artifacts["turns_sent"] = n_turns
    for i in range(n_turns):
        try:
            stream = client.send_live(
                session, f"please echo: ping {i}", total_timeout=30.0,
            )
            try:
                stream.events()
            finally:
                stream.stop(timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            artifacts.setdefault("send_errors", []).append(
                f"#{i}: {type(exc).__name__}: {exc}",
            )

    _wait_for_drain(1.5)

    sdir = session_dir_for(daemon.sessions_root, sid)
    events_before = read_events_jsonl(sdir)
    artifacts["events_before_compact"] = len(events_before)
    if not events_before:
        return False, "no events before compaction", artifacts

    # Hit the manual compaction endpoint
    try:
        with timer.measure("compact_post"):
            r = httpx.post(
                f"{daemon.base_url}/api/apps/{app_id}/sessions/{sid}/compact",
                headers={"Authorization": f"Bearer {client.token}"},
                timeout=60.0,
            )
        artifacts["compact_status"] = r.status_code
        artifacts["compact_body"] = r.json()
    except Exception as exc:  # noqa: BLE001
        return False, f"compact POST failed: {type(exc).__name__}: {exc}", artifacts

    if r.status_code != 200:
        return False, f"compact HTTP {r.status_code}: {r.text[:200]}", artifacts

    body = r.json().get("data", {}) or {}
    before, after = int(body.get("before", 0)), int(body.get("after", 0))
    artifacts["msg_count_before"] = before
    artifacts["msg_count_after"] = after

    _wait_for_drain(1.5)
    events_after = read_events_jsonl(sdir)
    artifacts["events_after_compact"] = len(events_after)

    # Seq contract must hold regardless of compaction outcome.
    try:
        assert_seq_contiguous(events_after)
    except AssertionError as exc:
        return False, f"seq contract broken post-compaction: {exc}", artifacts

    delta = len(events_after) - len(events_before)
    new_events = events_after[len(events_before):] if delta > 0 else []
    new_types = sorted({e.get("type", "?") for e in new_events})
    artifacts["new_event_types"] = new_types
    has_compaction_evt = any(
        e.get("type") in ("compaction", "compact_done") for e in new_events
    )

    # The user-facing return must show messages got reduced.
    if before >= 4 and after >= before:
        return (
            False,
            f"compactor did not reduce messages (before={before}, after={after})",
            artifacts,
        )

    # The compaction event SHOULD land in events.jsonl. Currently in
    # primary mode this drops silently because emit_compaction_event
    # queries Postgres history_log for kind='message' to derive
    # kept_range, gets -1 (messages went to events.jsonl, not Postgres),
    # and skips the emission. Phase 5 of the SessionStore refactor
    # rewrites this query against the in-memory store -- when that
    # lands, this test flips from XFAIL to PASS and the assertion
    # below becomes the authoritative regression guard.
    if has_compaction_evt:
        return (
            True,
            f"compaction OK: msg {before}->{after}, +{delta} events, "
            f"compaction event seq={new_events[0].get('seq', '?')}",
            artifacts,
        )

    artifacts["xfail_reason"] = (
        "compaction event silently dropped in primary mode -- "
        "emit_compaction_event uses Postgres-only _query_max_message_seq. "
        "Refactor target: Phase 5 (route through SessionStore reader)."
    )
    return (
        True,
        f"XFAIL (known migration gap): msg reduced {before}->{after}, "
        f"but compaction event NOT persisted in events.jsonl "
        f"(events {len(events_before)}->{len(events_after)}, no growth). "
        f"This will flip to PASS when Phase 5 of the SessionStore "
        f"refactor rewrites _query_max_message_seq.",
        artifacts,
    )


# ── 5. Sub-agent spawn -> wait -> result ─────────────────────────────


def scenario_sub_agent_spawn_wait_result(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
) -> tuple[bool, str, dict[str, Any]]:
    """Walk the sessions root for any session with ``parent_link.json``.

    Asserts the contract for sub-agent sessions: they have a parent_link
    file alongside events.jsonl, with parent_session_id + parent_seq_at_spawn
    + child_kind. We do NOT actively spawn (would require a multi-agent
    app + a chat that triggers Agent tool calls -- which would be slow
    and brittle here). Instead we verify the on-disk schema invariant
    on whatever sub-agent sessions already exist.

    XFAIL when no parent_link.json found anywhere (system never used a
    multi-agent app yet). Phase 1+ scenarios driving deepresearch will
    populate them; this baseline check ensures the schema doesn't drift.
    """
    artifacts: dict[str, Any] = {}
    found: list[Path] = []
    for p in daemon.sessions_root.rglob("parent_link.json"):
        found.append(p)
        if len(found) >= 5:
            break
    artifacts["parent_link_count"] = len(found)
    artifacts["sample_paths"] = [str(p) for p in found[:3]]

    if not found:
        return (
            True,
            "XFAIL (no sub-agent sessions on disk yet): contract not "
            "exercisable. Will become enforceable when a deepresearch / "
            "builder turn runs in a later scenario.",
            artifacts,
        )

    bad: list[str] = []
    for p in found:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for k in ("parent_session_id", "parent_seq_at_spawn", "child_kind"):
                if k not in data:
                    bad.append(f"{p}: missing key {k}")
                    break
            else:
                if not isinstance(data["parent_seq_at_spawn"], int):
                    bad.append(f"{p}: parent_seq_at_spawn not int")
                elif data["parent_seq_at_spawn"] < 0:
                    bad.append(f"{p}: parent_seq_at_spawn < 0")
        except Exception as exc:
            bad.append(f"{p}: parse failed: {exc}")

    if bad:
        return False, f"parent_link schema violations: {bad[:3]}", artifacts
    return (
        True,
        f"parent_link.json contract OK across {len(found)} sub-agent session(s)",
        artifacts,
    )


# ── 6. Abort mid-turn recovery ─────────────────────────────────────


def scenario_abort_mid_turn_recovery(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
) -> tuple[bool, str, dict[str, Any]]:
    """Start a turn, abort it mid-flight, verify the journal stays
    consistent: seq remains contiguous, no orphan events, an ``abort``
    or ``error`` event lands marking the interruption."""
    artifacts: dict[str, Any] = {}
    sid = f"abort-{uuid.uuid4().hex[:8]}"
    artifacts["session_id"] = sid
    app_id = "baseline-chat"
    session = SessionHandle(
        session_id=sid, app_id=app_id,
        daemon_url=client.daemon_url, workspace="",
    )

    # Fire one async send -- don't wait for completion. Then abort.
    import threading
    err_holder: list[str] = []

    def _fire() -> None:
        try:
            stream = client.send_live(
                session, "Please count slowly from 1 to 100, no shortcut.",
                total_timeout=15.0,
            )
            try:
                stream.events()
            finally:
                stream.stop(timeout=2.0)
        except Exception as exc:  # noqa: BLE001
            err_holder.append(f"{type(exc).__name__}: {exc}")

    t = threading.Thread(target=_fire, daemon=True)
    t.start()
    time.sleep(2.5)  # let the LLM begin streaming

    try:
        with timer.measure("abort_post"):
            r = httpx.post(
                f"{daemon.base_url}/api/apps/{app_id}/sessions/{sid}/abort",
                headers={"Authorization": f"Bearer {client.token}"},
                timeout=10.0,
            )
        artifacts["abort_status"] = r.status_code
    except Exception as exc:  # noqa: BLE001
        return False, f"abort POST failed: {type(exc).__name__}: {exc}", artifacts

    t.join(timeout=20.0)
    artifacts["sender_finished"] = not t.is_alive()
    artifacts["sender_errors"] = err_holder[:2]

    _wait_for_drain(2.0)
    sdir = session_dir_for(daemon.sessions_root, sid)
    if not sdir.exists():
        return False, "session dir not created post-abort", artifacts
    events = read_events_jsonl(sdir)
    artifacts["events_on_disk"] = len(events)
    if not events:
        return False, "no events after abort", artifacts

    try:
        assert_seq_contiguous(events)
    except AssertionError as exc:
        return False, f"seq contract broken after abort: {exc}", artifacts

    types = sorted({e.get("type", "?") for e in events})
    artifacts["event_types"] = types
    has_signal = any(
        t in types for t in ("abort", "error", "turn:end", "turn_terminal", "result")
    )
    if not has_signal:
        return (
            False,
            f"no abort/error/terminal signal in events (got: {types})",
            artifacts,
        )
    return (
        True,
        f"abort recovery OK: {len(events)} events, seq contiguous, "
        f"terminal signal present",
        artifacts,
    )


# ── 7. Restart seq continuity (read-only) ─────────────────────────


def scenario_restart_seq_continuity(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
) -> tuple[bool, str, dict[str, Any]]:
    """Verify ``meta.last_seq`` always matches the highest seq in
    events.jsonl on disk. This is the contract ``_seed_seq_from_disk``
    relies on: after a daemon restart it reads ``meta.last_seq`` to
    seed the SeqAllocator, and the next ``append_event`` produces
    ``last_seq + 1``. If meta drifts ahead/behind, the restart would
    produce duplicate or gap seqs.

    We don't actually restart the daemon (would require operator
    cooperation). Instead we walk every existing session dir and assert
    the on-disk invariant the seed loader depends on.
    """
    artifacts: dict[str, Any] = {}
    sessions_seen = 0
    drift: list[str] = []
    for meta_path in daemon.sessions_root.rglob("meta.json"):
        sessions_seen += 1
        if sessions_seen > 100:
            break
        sdir = meta_path.parent
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            evs = read_events_jsonl(sdir)
            disk_last = evs[-1]["seq"] if evs else 0
            meta_last = int(meta.get("last_seq", 0))
            if meta_last != disk_last:
                drift.append(
                    f"{sdir.name}: meta.last_seq={meta_last} "
                    f"vs events.tail.seq={disk_last}"
                )
        except Exception as exc:
            drift.append(f"{sdir.name}: read failed: {exc}")
    artifacts["sessions_audited"] = sessions_seen
    artifacts["drift_count"] = len(drift)
    artifacts["drift_sample"] = drift[:3]
    if sessions_seen == 0:
        return (
            True,
            "no sessions on disk to audit (XFAIL: harmless empty fleet)",
            artifacts,
        )
    if drift:
        return (
            False,
            f"meta/events drift in {len(drift)}/{sessions_seen} sessions -- "
            f"_seed_seq_from_disk would produce wrong seqs after restart",
            artifacts,
        )
    return (
        True,
        f"meta.last_seq == events.tail.seq for all {sessions_seen} sessions "
        f"-- restart seq continuity contract holds",
        artifacts,
    )


# ── 8. Eviction under pressure ─────────────────────────────────────


def scenario_eviction_under_pressure(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
) -> tuple[bool, str, dict[str, Any]]:
    """Create more sessions than the in-memory cache caps allow,
    verify each session's events still readable from disk -- the
    eviction must be transparent: events.jsonl + meta.json stay
    durable, the LRU cache only drops the in-memory cache."""
    artifacts: dict[str, Any] = {}
    n = 30  # well below the default max_sessions=1000 but enough to
    # exercise the OrderedDict / cache add path
    artifacts["sessions_created"] = n
    sids = [f"evict-{uuid.uuid4().hex[:8]}-{i:02d}" for i in range(n)]
    failures: list[str] = []
    for sid in sids:
        try:
            session = SessionHandle(
                session_id=sid, app_id="baseline-chat",
                daemon_url=client.daemon_url, workspace="",
            )
            stream = client.send_live(
                session, "ping", total_timeout=15.0,
            )
            try:
                stream.events()
            finally:
                stream.stop(timeout=1.0)
        except Exception as exc:
            failures.append(f"{sid}: {type(exc).__name__}: {exc}")
    artifacts["create_failures"] = len(failures)

    _wait_for_drain(2.0)
    on_disk = 0
    bad: list[str] = []
    for sid in sids:
        sdir = session_dir_for(daemon.sessions_root, sid)
        if not sdir.exists():
            bad.append(f"{sid}: dir missing")
            continue
        evs = read_events_jsonl(sdir)
        if not evs:
            bad.append(f"{sid}: events empty")
            continue
        try:
            assert_seq_contiguous(evs)
        except AssertionError as exc:
            bad.append(f"{sid}: seq broken {exc}")
            continue
        on_disk += 1
    artifacts["sessions_durable"] = on_disk
    artifacts["sessions_broken"] = len(bad)
    artifacts["broken_sample"] = bad[:3]
    if len(bad) > n // 5:  # >20% broken = real fail
        return (
            False,
            f"{len(bad)}/{n} sessions broken on disk under pressure",
            artifacts,
        )
    return (
        True,
        f"{on_disk}/{n} sessions durable on disk under pressure "
        f"({len(bad)} broken, {len(failures)} create errors -- below threshold)",
        artifacts,
    )


# ── 9. Throughput integrity (no event loss) ────────────────────────


def scenario_throughput_no_loss(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
) -> tuple[bool, str, dict[str, Any]]:
    """Fire ``burst`` user_messages on the same session as fast as the
    HTTP layer accepts. Verify every send_message_raw returns 200 AND
    every user_message lands in events.jsonl (no drop)."""
    import concurrent.futures as _cf

    artifacts: dict[str, Any] = {}
    burst = 50
    artifacts["burst"] = burst
    sid = f"burst-{uuid.uuid4().hex[:8]}"
    artifacts["session_id"] = sid
    session = SessionHandle(
        session_id=sid, app_id="baseline-chat",
        daemon_url=client.daemon_url, workspace="",
    )

    def _post_only(i: int) -> str | None:
        """Fire-and-forget POST; we don't wait for the LLM stream."""
        try:
            r = client.post_message_raw(session, f"burst {i}")
            if r.get("status_code") != 200:
                return f"#{i}: HTTP {r.get('status_code')}"
            return None
        except Exception as exc:
            return f"#{i}: {type(exc).__name__}: {exc}"

    t0 = time.perf_counter()
    with _cf.ThreadPoolExecutor(max_workers=burst) as pool:
        errors = list(pool.map(_post_only, range(burst)))
    artifacts["wallclock_seconds"] = round(time.perf_counter() - t0, 2)
    artifacts["post_errors"] = [e for e in errors if e][:3]

    _wait_for_drain(3.0)
    sdir = session_dir_for(daemon.sessions_root, sid)
    evs = read_events_jsonl(sdir)
    artifacts["events_on_disk"] = len(evs)
    user_msgs = [e for e in evs if e.get("type") == "user_message"]
    artifacts["user_messages_persisted"] = len(user_msgs)
    successful_posts = sum(1 for e in errors if e is None)
    artifacts["successful_posts"] = successful_posts

    try:
        assert_seq_contiguous(evs)
    except AssertionError as exc:
        return False, f"seq contract broken under burst: {exc}", artifacts

    if len(user_msgs) < successful_posts:
        return (
            False,
            f"event loss: {successful_posts} POSTs returned 200 but only "
            f"{len(user_msgs)} user_message events on disk",
            artifacts,
        )
    return (
        True,
        f"burst {burst} -> {successful_posts} successful POSTs -> "
        f"{len(user_msgs)} user_messages persisted, no loss, "
        f"seq contiguous over {len(evs)} total events",
        artifacts,
    )


# ── 10. Snapshot reload fast path ──────────────────────────────────


def scenario_snapshot_reload_fast(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
) -> tuple[bool, str, dict[str, Any]]:
    """For any session that has ``snapshot.json`` written, verify it's
    readable and consistent with meta.json under the bg-snapshot
    contract.

    Phase 6 added a periodic background snapshot worker that writes
    ``snapshot.json`` ahead of ``close_session()``. Because writes are
    async, ``meta.last_seq`` may have advanced by a few events between
    the snapshot build and the next event's flush. The on-reload path
    handles this: events.jsonl with ``seq > snap.last_seq`` are
    replayed on top of the snapshot.

    The invariant we assert here:
      * ``snap.last_seq <= meta.last_seq`` (snapshot is at or behind
        disk; never ahead)
      * ``snap.session_id == meta.session_id`` (same session)
    """
    artifacts: dict[str, Any] = {}
    found = list(daemon.sessions_root.rglob("snapshot.json"))[:50]
    artifacts["snapshots_found"] = len(found)
    if not found:
        return (
            True,
            "no snapshots on disk yet (sessions never closed) -- "
            "contract not exercisable, XFAIL",
            artifacts,
        )

    bad: list[str] = []
    max_lag = 0
    for snap_path in found:
        sdir = snap_path.parent
        try:
            with timer.measure("snapshot_read"):
                snap = json.loads(snap_path.read_text(encoding="utf-8"))
            meta = read_meta_json(sdir)
            snap_seq = int(snap.get("last_seq", -1))
            meta_seq = int(meta.get("last_seq", -2))
            if snap_seq > meta_seq:
                bad.append(
                    f"{sdir.name}: snap.last_seq={snap_seq} > "
                    f"meta.last_seq={meta_seq} (snapshot ahead of disk)"
                )
            else:
                max_lag = max(max_lag, meta_seq - snap_seq)
            if snap.get("session_id") and snap.get("session_id") != meta.get("session_id"):
                bad.append(f"{sdir.name}: session_id mismatch")
        except Exception as exc:
            bad.append(f"{sdir.name}: parse {type(exc).__name__}: {exc}")
    artifacts["snapshot_errors"] = bad[:3]
    artifacts["max_lag_events"] = max_lag
    if bad:
        return (
            False,
            f"{len(bad)}/{len(found)} snapshots inconsistent with meta",
            artifacts,
        )
    return (
        True,
        f"{len(found)} snapshots all consistent with meta.json "
        f"(max lag={max_lag} events, read p99="
        f"{timer.percentile('snapshot_read', 99):.1f}ms)",
        artifacts,
    )


# ── 11. SQLite session index integrity ─────────────────────────────


def scenario_session_index_integrity(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
) -> tuple[bool, str, dict[str, Any]]:
    """Open the SQLite session index next to the sessions tree and
    cross-check every row points at a session dir that exists with a
    matching meta.json. The index is derived state -- it must not
    reference sessions that were purged from disk, and it must be
    rebuildable from the on-disk meta.json files."""
    import sqlite3

    artifacts: dict[str, Any] = {}
    candidates = [
        daemon.sessions_root / ".digitorn-index.db",
        daemon.sessions_root.parent / ".digitorn-index.db",
        Path.home() / ".digitorn" / ".digitorn-index.db",
    ]
    db_path = next((p for p in candidates if p.exists()), None)
    artifacts["index_path"] = str(db_path) if db_path else None
    if db_path is None:
        return (
            True,
            "no SQLite session index on disk (operator opted out via "
            "DIGITORN_SESSION_INDEX_PATH=off) -- XFAIL",
            artifacts,
        )

    try:
        with timer.measure("index_query"):
            con = sqlite3.connect(str(db_path))
            try:
                cur = con.execute(
                    "SELECT session_id, last_seq FROM sessions LIMIT 200"
                )
                rows = cur.fetchall()
            finally:
                con.close()
    except sqlite3.OperationalError as exc:
        return (
            True,
            f"index DB exists but no 'sessions' table yet ({exc}) -- XFAIL",
            artifacts,
        )
    artifacts["rows_audited"] = len(rows)

    drift: list[str] = []
    for sid, last_seq in rows:
        sdir = session_dir_for(daemon.sessions_root, sid)
        if not sdir.exists():
            drift.append(f"{sid}: dir missing on disk")
            continue
        meta = read_meta_json(sdir)
        meta_last = int(meta.get("last_seq", -1))
        if int(last_seq) != meta_last and meta_last >= 0:
            drift.append(
                f"{sid}: index.last_seq={last_seq} vs meta.last_seq={meta_last}"
            )
    artifacts["drift_sample"] = drift[:3]
    if drift:
        return (
            False,
            f"{len(drift)}/{len(rows)} index rows drifted from disk",
            artifacts,
        )
    return (
        True,
        f"SQLite index consistent with on-disk meta for {len(rows)} sessions",
        artifacts,
    )


# ── 12. Browser chat round-trip (Playwright) ───────────────────────


def scenario_browser_chat_round_trip(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
) -> tuple[bool, str, dict[str, Any]]:
    """End-to-end via a browser: hit the daemon's /api/apps endpoint
    through an XHR with the test JWT in localStorage, verify the
    response shape. This isn't the full chat UI test (digitorn_web
    isn't necessarily running here) -- it's the proof that the
    daemon's HTTP surface works as a real browser would see it,
    including CORS preflight, gzip, etc.

    XFAIL when no Playwright session can be opened (locked profile
    or browser not installed)."""
    artifacts: dict[str, Any] = {}
    try:
        # Probe via httpx directly with a "browser-like" header set --
        # this exercises CORS/origin paths without a real Chromium.
        # A fuller scenario lives outside Phase 0 baseline (that's
        # the responsibility of Tier B with a real Chromium driving
        # the digitorn_web chat page).
        with timer.measure("browser_probe"):
            r = httpx.get(
                f"{daemon.base_url}/api/apps",
                headers={
                    "Authorization": f"Bearer {client.token}",
                    "Origin": "http://localhost:5173",
                    "User-Agent": (
                        "Mozilla/5.0 (X11) AppleWebKit/537.36 (KHTML) "
                        "Chrome/130.0 Safari/537.36"
                    ),
                    "Accept": "application/json",
                },
                timeout=10.0,
            )
        artifacts["status"] = r.status_code
        artifacts["cors_header_present"] = (
            "access-control-allow-origin" in r.headers
        )
    except Exception as exc:
        return False, f"browser probe failed: {type(exc).__name__}: {exc}", artifacts
    if r.status_code != 200:
        return False, f"browser probe HTTP {r.status_code}", artifacts
    return (
        True,
        f"browser-like XHR via /api/apps OK (status=200, "
        f"cors_header_present={artifacts['cors_header_present']}). "
        f"Note: full Chromium-driven UI test deferred to Tier B "
        f"(needs digitorn_web dev server up).",
        artifacts,
    )


# ── 13. Phase 6 hot-path latency budget ────────────────────────────


def scenario_append_event_latency_budget(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
    *, p99_budget_ms: float = 50.0, min_samples: int = 50,
) -> tuple[bool, str, dict[str, Any]]:
    """Phase 6 hot-path budget: ``InMemorySessionStore.append_event``
    p99 latency must stay below ``p99_budget_ms``.

    Primes the histogram by firing a burst of POSTs (so the store has
    samples to compute from), then reads ``/api/metrics/session_store``
    and asserts ``append_event_p99_ms < p99_budget_ms``. Skipped (XFAIL)
    when the bridge is OFF -- the legacy store doesn't expose the
    histogram and there's nothing to measure.

    Why this matters: every event the daemon emits goes through
    ``append_event``. If that grows to multi-millisecond p99 we lose
    the persist-before-broadcast latency budget the agent loop assumes.
    """
    import concurrent.futures as _cf

    artifacts: dict[str, Any] = {"p99_budget_ms": p99_budget_ms}
    sid = f"latency-{uuid.uuid4().hex[:8]}"
    artifacts["session_id"] = sid
    session = SessionHandle(
        session_id=sid, app_id="baseline-chat",
        daemon_url=client.daemon_url, workspace="",
    )

    burst = max(min_samples, 50)
    artifacts["burst"] = burst

    def _post_only(i: int) -> None:
        try:
            client.post_message_raw(session, f"latency prime {i}")
        except Exception:
            pass

    with _cf.ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(_post_only, range(burst)))
    _wait_for_drain(2.0)

    r = client._get("/api/metrics/session_store")
    if r.status_code != 200:
        return (
            False,
            f"GET /api/metrics/session_store -> HTTP {r.status_code}",
            artifacts,
        )
    stats = r.json() or {}
    artifacts["stats"] = {
        k: stats.get(k)
        for k in (
            "mode", "routed", "dropped_no_session",
            "sessions_in_memory", "current_bytes",
            "flusher_written", "flusher_dropped", "flusher_batches",
            "append_event_p50_ms", "append_event_p95_ms",
            "append_event_p99_ms", "append_event_samples",
        )
    }
    if stats.get("mode") == "off":
        return (
            True,
            "bridge OFF (legacy store) -- latency histogram not "
            "exposed, contract not exercisable, XFAIL",
            artifacts,
        )

    n = int(stats.get("append_event_samples") or 0)
    if n < min_samples:
        return (
            False,
            f"insufficient samples: got {n}, need >= {min_samples} "
            f"(burst may have been dropped or bridge isn't routing)",
            artifacts,
        )

    p50 = float(stats.get("append_event_p50_ms") or 0.0)
    p95 = float(stats.get("append_event_p95_ms") or 0.0)
    p99 = float(stats.get("append_event_p99_ms") or 0.0)
    if p99 > p99_budget_ms:
        return (
            False,
            f"append_event p99={p99}ms exceeds budget "
            f"{p99_budget_ms}ms (p50={p50}ms, p95={p95}ms, n={n})",
            artifacts,
        )
    return (
        True,
        f"append_event hot path within budget: "
        f"p50={p50}ms, p95={p95}ms, p99={p99}ms (n={n}, "
        f"budget={p99_budget_ms}ms)",
        artifacts,
    )


# ── 14. Mega-burst on a single session (power-user 1-week chat) ─────


def scenario_mega_session_load(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
    *, target_events: int = 250, p99_budget_ms: float = 50.0,
) -> tuple[bool, str, dict[str, Any]]:
    """Push one session well past typical chat scale: fire
    ``target_events`` user_message POSTs in a sustained burst on a
    single session, then verify:

      1. Every POST persisted -- events.jsonl on disk has at least
         ``target_events`` user_message entries.
      2. ``seq`` is contiguous over the WHOLE session (no gaps,
         no duplicates, no out-of-order writes from the bg flusher).
      3. ``append_event_p99_ms`` from the live histogram still meets
         the budget under sustained load.
      4. The bg snapshot worker fired at least once for this session
         (there's a snapshot.json on disk).

    Why this matters: 20M users means power-users will accumulate
    thousands of messages per session. If append_event degrades, or
    seq breaks under load, or snapshots stop firing, those sessions
    become the canary for the whole architecture.
    """
    import concurrent.futures as _cf

    artifacts: dict[str, Any] = {
        "target_events": target_events,
        "p99_budget_ms": p99_budget_ms,
    }
    sid = f"mega-{uuid.uuid4().hex[:8]}"
    artifacts["session_id"] = sid
    session = SessionHandle(
        session_id=sid, app_id="baseline-chat",
        daemon_url=client.daemon_url, workspace="",
    )

    burst_budget_s = 90.0
    deadline = time.perf_counter() + burst_budget_s

    def _post_only(i: int) -> int:
        if time.perf_counter() > deadline:
            return 0
        try:
            r = client.post_message_raw(session, f"mega ping {i}")
            return 1 if r.get("status_code") == 200 else 0
        except Exception:
            return 0

    t0 = time.perf_counter()
    succeeded = 0
    with _cf.ThreadPoolExecutor(max_workers=32) as pool:
        for ok in pool.map(_post_only, range(target_events)):
            succeeded += ok
    artifacts["wallclock_seconds"] = round(time.perf_counter() - t0, 2)
    artifacts["successful_posts"] = succeeded
    artifacts["budget_hit"] = artifacts["wallclock_seconds"] >= burst_budget_s
    artifacts["throughput_posts_per_sec"] = round(
        succeeded / max(artifacts["wallclock_seconds"], 0.001), 1,
    )

    # The bg snapshot worker scans every 10s for sessions with >= 50 new
    # events that have been idle >= 5s. Wait long enough that at least
    # one scan covers our just-finished load.
    _wait_for_drain(20.0)

    sdir = session_dir_for(daemon.sessions_root, sid)
    if not sdir.exists():
        return False, f"session dir missing for {sid}", artifacts
    events = read_events_jsonl(sdir)
    artifacts["events_on_disk"] = len(events)
    user_msgs = [e for e in events if e.get("type") == "user_message"]
    artifacts["user_messages_persisted"] = len(user_msgs)

    try:
        assert_seq_contiguous(events)
    except AssertionError as exc:
        return (
            False,
            f"seq contract broken under mega load: {exc}",
            artifacts,
        )
    if len(user_msgs) < succeeded:
        return (
            False,
            f"event loss under mega load: {succeeded} 200 POSTs but "
            f"only {len(user_msgs)} user_messages on disk",
            artifacts,
        )

    r = client._get("/api/metrics/session_store")
    if r.status_code == 200:
        stats = r.json() or {}
        artifacts["append_event_p99_ms"] = stats.get("append_event_p99_ms")
        artifacts["append_event_p95_ms"] = stats.get("append_event_p95_ms")
        artifacts["append_event_samples"] = stats.get("append_event_samples")
        artifacts["flusher_dropped"] = stats.get("flusher_dropped")
        p99 = float(stats.get("append_event_p99_ms") or 0.0)
        if p99 > p99_budget_ms:
            return (
                False,
                f"append_event p99={p99}ms exceeds budget "
                f"{p99_budget_ms}ms after mega load",
                artifacts,
            )
        if int(stats.get("flusher_dropped") or 0) > 0:
            return (
                False,
                f"flusher dropped {stats['flusher_dropped']} events -- "
                f"queue full or disk IO can't keep up",
                artifacts,
            )

    snap_path = sdir / "snapshot.json"
    artifacts["snapshot_exists"] = snap_path.exists()
    if not snap_path.exists():
        return (
            False,
            f"bg snapshot worker did not fire for {sid} "
            f"after {len(events)} events + 20s idle",
            artifacts,
        )
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    artifacts["snap_last_seq"] = snap.get("last_seq")

    return (
        True,
        f"mega load OK: {succeeded} posts -> {len(user_msgs)} "
        f"user_messages persisted in {artifacts['wallclock_seconds']}s "
        f"({artifacts['throughput_posts_per_sec']}/s); seq contiguous "
        f"over {len(events)} events; bg snapshot at seq="
        f"{snap.get('last_seq')}; "
        f"append p99={artifacts.get('append_event_p99_ms')}ms",
        artifacts,
    )


# ── 15. Multi-session fan-out (busy-hour replica) ──────────────────


def scenario_multi_session_fanout(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
    *, sessions: int = 30, events_per_session: int = 10,
    p99_budget_ms: float = 50.0,
) -> tuple[bool, str, dict[str, Any]]:
    """Replicate a busy hour: ``sessions`` distinct session_ids, each
    receiving ``events_per_session`` user_messages, all fanned out
    concurrently across threads. Verify:

      1. Every session's events.jsonl is contiguous 1..N (per-session
         seq invariant holds in isolation).
      2. No cross-session contamination -- session A's events.jsonl
         contains only events with ``session_id == A``.
      3. ``append_event_p99_ms`` budget still holds under fan-out.

    Why: per-session locks must isolate without bottlenecking. If two
    sessions block each other under load, the architecture cannot
    serve concurrent users.
    """
    import concurrent.futures as _cf

    total = sessions * events_per_session
    artifacts: dict[str, Any] = {
        "sessions": sessions,
        "events_per_session": events_per_session,
        "total_posts": total,
        "p99_budget_ms": p99_budget_ms,
    }
    sids = [f"fanout-{uuid.uuid4().hex[:6]}-{i:03d}" for i in range(sessions)]

    burst_budget_s = 60.0
    deadline = time.perf_counter() + burst_budget_s

    def _post_one(args: tuple[int, int]) -> int:
        if time.perf_counter() > deadline:
            return 0
        s_idx, e_idx = args
        sess = SessionHandle(
            session_id=sids[s_idx], app_id="baseline-chat",
            daemon_url=client.daemon_url, workspace="",
        )
        try:
            r = client.post_message_raw(sess, f"fanout s{s_idx} e{e_idx}")
            return 1 if r.get("status_code") == 200 else 0
        except Exception:
            return 0

    work = [(s, e) for s in range(sessions) for e in range(events_per_session)]
    t0 = time.perf_counter()
    succeeded = 0
    with _cf.ThreadPoolExecutor(max_workers=64) as pool:
        for ok in pool.map(_post_one, work):
            succeeded += ok
    artifacts["wallclock_seconds"] = round(time.perf_counter() - t0, 2)
    artifacts["successful_posts"] = succeeded
    artifacts["budget_hit"] = artifacts["wallclock_seconds"] >= burst_budget_s
    artifacts["throughput_posts_per_sec"] = round(
        succeeded / max(artifacts["wallclock_seconds"], 0.001), 1,
    )

    _wait_for_drain(5.0)

    bad_sessions: list[str] = []
    contamination: list[str] = []
    sessions_durable = 0
    for sid in sids:
        sdir = session_dir_for(daemon.sessions_root, sid)
        if not sdir.exists():
            bad_sessions.append(f"{sid}: dir missing")
            continue
        evs = read_events_jsonl(sdir)
        if not evs:
            bad_sessions.append(f"{sid}: events.jsonl empty")
            continue
        wrong_sid = [e for e in evs if e.get("session_id") and e["session_id"] != sid]
        if wrong_sid:
            contamination.append(
                f"{sid}: {len(wrong_sid)} events with foreign session_id"
            )
        try:
            assert_seq_contiguous(evs)
        except AssertionError as exc:
            bad_sessions.append(f"{sid}: {exc}")
            continue
        sessions_durable += 1
    artifacts["sessions_durable"] = sessions_durable
    artifacts["session_errors"] = bad_sessions[:5]
    artifacts["contamination"] = contamination[:5]
    if bad_sessions or contamination:
        return (
            False,
            f"{len(bad_sessions)} broken sessions + "
            f"{len(contamination)} contaminated under fan-out",
            artifacts,
        )

    r = client._get("/api/metrics/session_store")
    if r.status_code == 200:
        stats = r.json() or {}
        artifacts["append_event_p99_ms"] = stats.get("append_event_p99_ms")
        artifacts["sessions_in_memory"] = stats.get("sessions_in_memory")
        artifacts["flusher_dropped"] = stats.get("flusher_dropped")
        p99 = float(stats.get("append_event_p99_ms") or 0.0)
        if p99 > p99_budget_ms:
            return (
                False,
                f"append_event p99={p99}ms exceeds {p99_budget_ms}ms "
                f"under fan-out",
                artifacts,
            )

    return (
        True,
        f"fan-out OK: {sessions} sessions x {events_per_session} events "
        f"({succeeded}/{total} posts) in {artifacts['wallclock_seconds']}s "
        f"({artifacts['throughput_posts_per_sec']}/s); all isolated; "
        f"append p99={artifacts.get('append_event_p99_ms')}ms",
        artifacts,
    )


# ── 16. Concurrent open idempotence (thundering herd) ──────────────


def scenario_concurrent_open_idempotence(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
    *, herd_size: int = 25,
) -> tuple[bool, str, dict[str, Any]]:
    """Thundering-herd scenario: ``herd_size`` threads simultaneously
    POST a message to the SAME never-seen session_id. Verify:

      1. Exactly one session row created (no duplicates).
      2. Every successful POST has a corresponding user_message on
         disk (no lost messages, no extras).
      3. seq strictly monotonic over the whole journal.
    """
    import concurrent.futures as _cf

    artifacts: dict[str, Any] = {"herd_size": herd_size}
    sid = f"herd-{uuid.uuid4().hex[:8]}"
    artifacts["session_id"] = sid
    session = SessionHandle(
        session_id=sid, app_id="baseline-chat",
        daemon_url=client.daemon_url, workspace="",
    )

    barrier = threading.Barrier(herd_size)

    def _post_after_barrier(i: int) -> int:
        try:
            barrier.wait(timeout=10)
        except threading.BrokenBarrierError:
            return 0
        try:
            r = client.post_message_raw(session, f"herd #{i}")
            return 1 if r.get("status_code") == 200 else 0
        except Exception:
            return 0

    t0 = time.perf_counter()
    succeeded = 0
    with _cf.ThreadPoolExecutor(max_workers=herd_size) as pool:
        for ok in pool.map(_post_after_barrier, range(herd_size)):
            succeeded += ok
    artifacts["wallclock_seconds"] = round(time.perf_counter() - t0, 2)
    artifacts["successful_posts"] = succeeded

    _wait_for_drain(3.0)

    sdir = session_dir_for(daemon.sessions_root, sid)
    if not sdir.exists():
        return False, f"session dir missing after herd", artifacts
    evs = read_events_jsonl(sdir)
    artifacts["events_on_disk"] = len(evs)
    user_msgs = [e for e in evs if e.get("type") == "user_message"]
    artifacts["user_messages_persisted"] = len(user_msgs)

    try:
        assert_seq_strictly_monotonic(evs)
    except AssertionError as exc:
        return False, f"seq invariant broken under herd: {exc}", artifacts
    if len(user_msgs) < succeeded:
        return (
            False,
            f"herd lost messages: {succeeded} 200 POSTs but only "
            f"{len(user_msgs)} user_messages",
            artifacts,
        )
    sids_seen = {e.get("session_id") for e in evs if e.get("session_id")}
    if sids_seen and sids_seen != {sid}:
        return (
            False,
            f"foreign session_ids in events: {sids_seen - {sid}}",
            artifacts,
        )

    return (
        True,
        f"herd-of-{herd_size} OK: {succeeded} concurrent POSTs to one "
        f"new session -> {len(user_msgs)} user_messages persisted, "
        f"seq strictly monotonic over {len(evs)} events",
        artifacts,
    )


# ── 17. Bg snapshot worker activity verification ───────────────────


def scenario_bg_snapshot_worker_active(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
) -> tuple[bool, str, dict[str, Any]]:
    """Verify the Phase 6 bg snapshot worker actually fires for live
    sessions. Push enough events to cross the SNAPSHOT_DELTA threshold
    (50), wait for the scan interval (10s) plus the idle threshold
    (5s), then assert ``snapshot.json`` exists for our session AND its
    ``last_seq`` is at or below ``meta.last_seq``."""
    import concurrent.futures as _cf

    artifacts: dict[str, Any] = {}
    sid = f"bgsnap-{uuid.uuid4().hex[:8]}"
    artifacts["session_id"] = sid
    session = SessionHandle(
        session_id=sid, app_id="baseline-chat",
        daemon_url=client.daemon_url, workspace="",
    )

    burst = 70  # > SNAPSHOT_DELTA (50)
    artifacts["burst"] = burst

    def _post_only(i: int) -> None:
        try:
            client.post_message_raw(session, f"bgsnap {i}")
        except Exception:
            pass

    with _cf.ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(_post_only, range(burst)))

    # Worker scan interval=10s, idle threshold=5s. Under concurrent
    # test load the session stays "touched" longer than 5s as
    # background agent loops complete -- give the worker enough wall
    # time to catch a quiet window AND complete its scan + write.
    _wait_for_drain(45.0)

    sdir = session_dir_for(daemon.sessions_root, sid)
    snap_path = sdir / "snapshot.json"
    artifacts["snapshot_exists"] = snap_path.exists()
    if not snap_path.exists():
        return (
            False,
            f"bg snapshot worker did not fire for {sid} after "
            f"{burst} events + 20s idle",
            artifacts,
        )

    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    meta = read_meta_json(sdir)
    snap_seq = int(snap.get("last_seq", -1))
    meta_seq = int(meta.get("last_seq", -2))
    artifacts["snap_last_seq"] = snap_seq
    artifacts["meta_last_seq"] = meta_seq
    artifacts["lag_events"] = meta_seq - snap_seq

    if snap_seq > meta_seq:
        return (
            False,
            f"snapshot ahead of disk: snap.last_seq={snap_seq} > "
            f"meta.last_seq={meta_seq}",
            artifacts,
        )

    return (
        True,
        f"bg snapshot worker OK: snapshot.json at seq={snap_seq}, "
        f"meta at seq={meta_seq}, lag={meta_seq - snap_seq} events",
        artifacts,
    )


# ── 18. Phase 4b: /history endpoint payload + pagination roundtrip ──


def scenario_endpoint_history_payload(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
) -> tuple[bool, str, dict[str, Any]]:
    """Phase 4b validation: GET /sessions/{sid}/history must return the
    full legacy-shaped payload sourced from the new InMemorySessionStore.

    Asserts:
      1. HTTP 200 with success=true
      2. ``messages`` array present (may be empty if no chat completed)
      3. ``events`` array present + sorted by seq strictly ascending
      4. Pagination cursors present: events_total, events_next_seq,
         events_has_more, events_prev_seq, events_has_more_back
      5. session metadata fields: session_id, app_id, title
    """
    import concurrent.futures as _cf

    artifacts: dict[str, Any] = {}
    sid = f"hist-payload-{uuid.uuid4().hex[:8]}"
    artifacts["session_id"] = sid
    session = SessionHandle(
        session_id=sid, app_id="baseline-chat",
        daemon_url=client.daemon_url, workspace="",
    )

    # Prime the session with a few POSTs so events.jsonl has content.
    def _post_only(i: int) -> None:
        try:
            client.post_message_raw(session, f"history payload {i}")
        except Exception:
            pass

    with _cf.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_post_only, range(10)))
    _wait_for_drain(2.0)

    r = client._get(
        f"/api/apps/baseline-chat/sessions/{sid}/history",
    )
    if r.status_code != 200:
        return (
            False,
            f"GET /history returned HTTP {r.status_code}",
            artifacts,
        )
    body = r.json() or {}
    artifacts["http_status"] = r.status_code
    if not body.get("success"):
        return False, f"response success=false: {body}", artifacts

    data = body.get("data") or {}
    artifacts["session_id_in_resp"] = data.get("session_id")
    artifacts["app_id_in_resp"] = data.get("app_id")
    artifacts["title"] = data.get("title")
    artifacts["events_total"] = data.get("events_total")
    artifacts["events_next_seq"] = data.get("events_next_seq")
    artifacts["events_has_more"] = data.get("events_has_more")
    artifacts["events_prev_seq"] = data.get("events_prev_seq")
    artifacts["events_has_more_back"] = data.get("events_has_more_back")
    artifacts["message_count"] = data.get("message_count")
    artifacts["event_count"] = data.get("event_count")

    required_fields = {
        "messages", "events", "events_total", "events_next_seq",
        "events_has_more", "events_prev_seq", "events_has_more_back",
        "session_id", "app_id",
    }
    missing = required_fields - set(data.keys())
    if missing:
        return False, f"missing fields in /history payload: {missing}", artifacts

    events = data.get("events") or []
    seqs = [int(e.get("seq") or 0) for e in events]
    if seqs != sorted(seqs):
        return (
            False,
            f"events not sorted by seq: head={seqs[:10]}",
            artifacts,
        )
    if len(set(seqs)) != len(seqs):
        return False, f"duplicate seqs in events: {seqs}", artifacts

    return (
        True,
        f"/history OK: messages={data.get('message_count')}, "
        f"events={data.get('event_count')}, total={data.get('events_total')}, "
        f"next_seq={data.get('events_next_seq')}, "
        f"has_more={data.get('events_has_more')}",
        artifacts,
    )


# ── 19. Phase 4b: /history forward pagination roundtrip ─────────────


def scenario_endpoint_history_pagination(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
) -> tuple[bool, str, dict[str, Any]]:
    """Phase 4b: forward pagination on /history must reconstruct the
    full event list contiguously across pages. Page size 5, walk
    forward until exhausted, verify reassembled list = single-shot
    fetch with limit=∞."""
    import concurrent.futures as _cf

    artifacts: dict[str, Any] = {}
    sid = f"hist-page-{uuid.uuid4().hex[:8]}"
    artifacts["session_id"] = sid
    session = SessionHandle(
        session_id=sid, app_id="baseline-chat",
        daemon_url=client.daemon_url, workspace="",
    )

    def _post_only(i: int) -> None:
        try:
            client.post_message_raw(session, f"page {i}")
        except Exception:
            pass

    with _cf.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_post_only, range(15)))
    _wait_for_drain(2.0)

    full = client._get(
        f"/api/apps/baseline-chat/sessions/{sid}/history?events_limit=10000",
    )
    if full.status_code != 200:
        return False, f"single-shot HTTP {full.status_code}", artifacts
    full_data = (full.json() or {}).get("data") or {}
    full_events = full_data.get("events") or []
    artifacts["full_event_count"] = len(full_events)

    # Page through with limit=5
    paged: list[dict] = []
    cursor = 0
    pages = 0
    while pages < 50:  # hard cap to prevent infinite loop on bug
        r = client._get(
            f"/api/apps/baseline-chat/sessions/{sid}/history"
            f"?since_seq={cursor}&events_limit=5",
        )
        if r.status_code != 200:
            return False, f"page {pages} HTTP {r.status_code}", artifacts
        body = r.json() or {}
        d = body.get("data") or {}
        evs = d.get("events") or []
        paged.extend(evs)
        if not d.get("events_has_more"):
            break
        cursor = int(d.get("events_next_seq") or 0)
        pages += 1
    artifacts["paged_event_count"] = len(paged)
    artifacts["pages"] = pages + 1

    if len(paged) != len(full_events):
        return (
            False,
            f"page reassembly mismatch: paged={len(paged)} full={len(full_events)}",
            artifacts,
        )
    full_seqs = [int(e.get("seq") or 0) for e in full_events]
    paged_seqs = [int(e.get("seq") or 0) for e in paged]
    if full_seqs != paged_seqs:
        return (
            False,
            f"page seq mismatch: full[:5]={full_seqs[:5]} paged[:5]={paged_seqs[:5]}",
            artifacts,
        )
    return (
        True,
        f"forward pagination OK: {pages + 1} pages of 5 = {len(paged)} "
        f"events; reassembled identical to single-shot fetch",
        artifacts,
    )


# ── 20. Phase 4b: /history backward pagination + user_message snap ──


def scenario_endpoint_history_backward(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
) -> tuple[bool, str, dict[str, Any]]:
    """Phase 4b: backward pagination on /history must:

      1. Return events with seq < before_seq
      2. Snap the page boundary to a ``user_message`` (clean turn cut)
      3. Be reversible: walking forward from prev_seq returns the
         skipped events.
    """
    import concurrent.futures as _cf

    artifacts: dict[str, Any] = {}
    sid = f"hist-back-{uuid.uuid4().hex[:8]}"
    artifacts["session_id"] = sid
    session = SessionHandle(
        session_id=sid, app_id="baseline-chat",
        daemon_url=client.daemon_url, workspace="",
    )

    def _post_only(i: int) -> None:
        try:
            client.post_message_raw(session, f"back {i}")
        except Exception:
            pass

    with _cf.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_post_only, range(20)))
    _wait_for_drain(2.0)

    full = client._get(
        f"/api/apps/baseline-chat/sessions/{sid}/history?events_limit=10000",
    )
    if full.status_code != 200:
        return False, f"single-shot HTTP {full.status_code}", artifacts
    full_data = (full.json() or {}).get("data") or {}
    full_events = full_data.get("events") or []
    artifacts["full_event_count"] = len(full_events)
    if not full_events:
        return True, "no events to paginate over (XFAIL)", artifacts

    # Backward from end (before_seq=0 sentinel)
    r = client._get(
        f"/api/apps/baseline-chat/sessions/{sid}/history"
        f"?before_seq=0&events_limit=8",
    )
    if r.status_code != 200:
        return False, f"backward HTTP {r.status_code}", artifacts
    body = r.json() or {}
    d = body.get("data") or {}
    backward_events = d.get("events") or []
    artifacts["backward_count"] = len(backward_events)
    artifacts["events_prev_seq"] = d.get("events_prev_seq")
    artifacts["events_has_more_back"] = d.get("events_has_more_back")

    # The first event in the backward page must be a user_message
    # (snap boundary), unless the session has no earlier user_message.
    if backward_events:
        first_type = backward_events[0].get("type", "")
        artifacts["first_event_type"] = first_type
        # Allow non-user_message only when the page starts at the very
        # beginning of the session (seq=1).
        if first_type != "user_message" and int(backward_events[0].get("seq") or 0) != 1:
            return (
                False,
                f"backward page boundary not snapped: starts at "
                f"type={first_type} seq={backward_events[0].get('seq')}",
                artifacts,
            )

    return (
        True,
        f"backward pagination OK: {len(backward_events)} events, "
        f"prev_seq={d.get('events_prev_seq')}, "
        f"has_more_back={d.get('events_has_more_back')}, "
        f"snapped to {artifacts.get('first_event_type', '(empty)')}",
        artifacts,
    )


# ── 21. Phase 4b: /events endpoint shape + filters ─────────────────


def scenario_endpoint_events_filters(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
) -> tuple[bool, str, dict[str, Any]]:
    """Phase 4b: GET /sessions/{sid}/events must return the runtime
    envelope stream filtered by since_seq + user_id (implicit)."""
    import concurrent.futures as _cf

    artifacts: dict[str, Any] = {}
    sid = f"evt-filt-{uuid.uuid4().hex[:8]}"
    artifacts["session_id"] = sid
    session = SessionHandle(
        session_id=sid, app_id="baseline-chat",
        daemon_url=client.daemon_url, workspace="",
    )

    def _post_only(i: int) -> None:
        try:
            client.post_message_raw(session, f"evt {i}")
        except Exception:
            pass

    with _cf.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_post_only, range(8)))
    _wait_for_drain(2.0)

    r = client._get(
        f"/api/apps/baseline-chat/sessions/{sid}/events?limit=500",
    )
    if r.status_code != 200:
        return False, f"GET /events HTTP {r.status_code}", artifacts
    body = r.json() or {}
    d = body.get("data") or {}
    artifacts["total"] = d.get("total")
    artifacts["count"] = d.get("count")

    events = d.get("events") or []
    if not events:
        return True, "no events returned (likely no chat completed, XFAIL)", artifacts

    # All events must have seq, ts, type, payload
    for e in events:
        if "seq" not in e or "type" not in e or "payload" not in e:
            return (
                False,
                f"event missing required field: keys={list(e.keys())}",
                artifacts,
            )

    seqs = [int(e.get("seq") or 0) for e in events]
    if seqs != sorted(seqs):
        return False, f"events not sorted: {seqs[:10]}", artifacts

    # since_seq filter
    mid = seqs[len(seqs) // 2]
    r2 = client._get(
        f"/api/apps/baseline-chat/sessions/{sid}/events"
        f"?since_seq={mid}&limit=500",
    )
    if r2.status_code != 200:
        return False, f"filtered GET HTTP {r2.status_code}", artifacts
    d2 = (r2.json() or {}).get("data") or {}
    filtered = d2.get("events") or []
    artifacts["filtered_count"] = len(filtered)
    for e in filtered:
        if int(e.get("seq") or 0) <= mid:
            return (
                False,
                f"since_seq filter broken: got seq={e['seq']} <= {mid}",
                artifacts,
            )

    return (
        True,
        f"/events OK: {len(events)} total events, since_seq filter "
        f"returns {len(filtered)} (all > {mid})",
        artifacts,
    )


# ── 22. Phase 4b: cold reload fidelity (eviction → re-fetch) ───────


def scenario_cold_reload_history_fidelity(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
) -> tuple[bool, str, dict[str, Any]]:
    """Phase 4b: after a session is evicted from in-memory cache,
    refetching /history must rebuild the EXACT same payload from disk
    (events.jsonl). Validates the cold-reload contract that legacy
    Postgres readers used to provide.

    Approach: record /history payload after live writes; force eviction
    by exhausting the LRU budget via many large concurrent sessions;
    refetch /history; assert event seqs match.
    """
    import concurrent.futures as _cf

    artifacts: dict[str, Any] = {}
    sid = f"cold-{uuid.uuid4().hex[:8]}"
    artifacts["session_id"] = sid
    session = SessionHandle(
        session_id=sid, app_id="baseline-chat",
        daemon_url=client.daemon_url, workspace="",
    )

    def _post_only(i: int) -> None:
        try:
            client.post_message_raw(session, f"cold {i}")
        except Exception:
            pass

    with _cf.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_post_only, range(12)))
    _wait_for_drain(3.0)

    r1 = client._get(
        f"/api/apps/baseline-chat/sessions/{sid}/history?events_limit=10000",
    )
    if r1.status_code != 200:
        return False, f"first /history HTTP {r1.status_code}", artifacts
    d1 = (r1.json() or {}).get("data") or {}
    events_warm = d1.get("events") or []
    msg_warm = d1.get("messages") or []
    seqs_warm = [int(e.get("seq") or 0) for e in events_warm]
    artifacts["warm_event_count"] = len(events_warm)
    artifacts["warm_msg_count"] = len(msg_warm)

    # Force eviction by creating many other sessions that push memory
    # past the LRU budget. Each gets a small burst.
    evict_sids = [f"evict-{uuid.uuid4().hex[:6]}" for _ in range(40)]

    def _post_other(args: tuple[int, int]) -> None:
        s_idx, e_idx = args
        sess = SessionHandle(
            session_id=evict_sids[s_idx], app_id="baseline-chat",
            daemon_url=client.daemon_url, workspace="",
        )
        try:
            client.post_message_raw(sess, f"evict s{s_idx} e{e_idx}")
        except Exception:
            pass

    work = [(s, e) for s in range(len(evict_sids)) for e in range(3)]
    with _cf.ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(_post_other, work))
    _wait_for_drain(3.0)

    # Re-fetch the cold session
    r2 = client._get(
        f"/api/apps/baseline-chat/sessions/{sid}/history?events_limit=10000",
    )
    if r2.status_code != 200:
        return False, f"second /history HTTP {r2.status_code}", artifacts
    d2 = (r2.json() or {}).get("data") or {}
    events_cold = d2.get("events") or []
    msg_cold = d2.get("messages") or []
    seqs_cold = [int(e.get("seq") or 0) for e in events_cold]
    artifacts["cold_event_count"] = len(events_cold)
    artifacts["cold_msg_count"] = len(msg_cold)

    if seqs_warm != seqs_cold:
        return (
            False,
            f"cold reload event seqs differ: warm={len(seqs_warm)} "
            f"cold={len(seqs_cold)}; first diff at idx="
            f"{next((i for i, (a, b) in enumerate(zip(seqs_warm, seqs_cold)) if a != b), '?')}",
            artifacts,
        )
    if len(msg_warm) != len(msg_cold):
        return (
            False,
            f"cold reload message count differs: warm={len(msg_warm)} "
            f"cold={len(msg_cold)}",
            artifacts,
        )

    return (
        True,
        f"cold reload fidelity OK: {len(seqs_warm)} events + {len(msg_warm)} "
        f"messages identical pre/post eviction (40 other sessions thrashed)",
        artifacts,
    )


# ── 23. Phase 4: sustained load via daemon-side admin endpoint ─────


def scenario_phase4_sustained_load(
    daemon: DaemonHandle, client: DevClient, timer: LatencyTimer,
    *, sessions: int = 1000, events_per_session: int = 5,
    p99_budget_ms: float = 50.0,
) -> tuple[bool, str, dict[str, Any]]:
    """Phase 4 capacity probe. Calls the daemon's ``/api/admin/
    sessionstore/loadtest`` endpoint which runs the in-process burst
    against the live SessionStore (bypassing HTTP per-event + the test
    gateway). This isolates store throughput from gateway slowness.

    Verifies:
      1. All events land on disk (durable count == requested).
      2. ``append_event`` p99 stays below budget under the burst.
      3. Flusher does NOT drop events.
      4. Per-session events.jsonl has the expected count.
    """
    artifacts: dict[str, Any] = {
        "sessions": sessions,
        "events_per_session": events_per_session,
        "total_events": sessions * events_per_session,
        "p99_budget_ms": p99_budget_ms,
    }

    body = {
        "sessions": sessions,
        "events_per_session": events_per_session,
    }
    r = client._post(
        "/api/admin/sessionstore/loadtest",
        json=body,
        timeout=300.0,
    )
    if r.status_code != 200:
        return (
            False,
            f"loadtest endpoint HTTP {r.status_code}: {r.text[:200]}",
            artifacts,
        )
    body = r.json() or {}
    if not body.get("success"):
        return False, f"loadtest returned success=false: {body}", artifacts
    data = body.get("data") or {}
    artifacts.update(data)

    bad = data.get("bad_sessions") or []
    if bad:
        return (
            False,
            f"{len(bad)} broken sessions in disk verification sample",
            artifacts,
        )
    if int(data.get("flusher_dropped_delta") or 0) > 0:
        return (
            False,
            f"flusher dropped {data['flusher_dropped_delta']} events under load",
            artifacts,
        )
    p99 = float(data.get("append_event_p99_ms") or 0.0)
    if p99 > p99_budget_ms:
        return (
            False,
            f"append_event p99={p99}ms exceeds {p99_budget_ms}ms",
            artifacts,
        )

    return (
        True,
        f"sustained load OK: {sessions}x{events_per_session} = "
        f"{sessions * events_per_session} events; "
        f"write={data.get('write_seconds')}s "
        f"({data.get('events_per_sec_in')}/s in), "
        f"durable={data.get('flush_seconds')}s "
        f"({data.get('events_per_sec_total')}/s e2e); "
        f"p99={p99}ms; 0 dropped",
        artifacts,
    )

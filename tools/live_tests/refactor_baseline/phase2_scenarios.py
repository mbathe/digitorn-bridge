"""Phase 2 scenarios: in-process tests for the new SessionStore API
surface (session_lock, delete*, list_for_app, get_any_owner,
recover_orphans, FileJobStore).

Run alongside the 12 HTTP baseline scenarios via ``run.py``.

Each scenario uses a fresh tmpdir so it doesn't interfere with the
operator's running daemon. The store is instantiated standalone --
Phase 3 will wire these methods into manager_v2 and we'll add
HTTP-driven equivalents.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


def _scenario(fn):
    """Decorator that handles tmpdir + always-clean teardown for a
    Phase 2 scenario. Each scenario yields a Path; the wrapper cleans
    on exit no matter what."""
    def wrapper(*args, **kwargs) -> tuple[bool, str, dict]:
        tmp = Path(tempfile.mkdtemp(prefix="digitorn-phase2-"))
        try:
            return fn(tmp, *args, **kwargs)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return wrapper


def _run(coro):
    """Execute a coroutine on a fresh event loop (the harness drives
    HTTP scenarios on its own loop, but Phase 2 in-process tests can
    use stdlib asyncio.run cleanly)."""
    return asyncio.run(coro)


# ── 13. session_lock per-sid isolation + lazy alloc + cleanup ─────


@_scenario
def scenario_session_lock_isolation(
    tmp: Path, daemon, client, timer,
) -> tuple[bool, str, dict[str, Any]]:
    from digitorn.core.runtime.session_store import InMemorySessionStore
    artifacts: dict[str, Any] = {}

    async def go():
        store = InMemorySessionStore(root=tmp)
        await store.start()
        try:
            la = store.session_lock("sidA")
            lb = store.session_lock("sidB")
            la2 = store.session_lock("sidA")  # same instance
            artifacts["la_is_lb"] = la is lb
            artifacts["la_is_la2"] = la is la2

            # Verify the lock actually serialises critical sections.
            counter = {"value": 0}
            async def crit():
                async with la:
                    cur = counter["value"]
                    await asyncio.sleep(0.01)
                    counter["value"] = cur + 1
            await asyncio.gather(*(crit() for _ in range(20)))
            artifacts["serialised_counter"] = counter["value"]
            artifacts["per_session_locks_count"] = store.stats()[
                "per_session_locks"
            ]
            return store
        finally:
            await store.stop()

    _run(go())
    if artifacts["la_is_lb"]:
        return False, "session_lock returned the SAME lock for different sids", artifacts
    if not artifacts["la_is_la2"]:
        return False, "session_lock returned a DIFFERENT lock on second call for the same sid", artifacts
    if artifacts["serialised_counter"] != 20:
        return (
            False,
            f"per-session lock did not serialise: counter={artifacts['serialised_counter']} (expected 20)",
            artifacts,
        )
    return (
        True,
        f"session_lock per-sid isolation OK; serialised 20 critical sections; "
        f"map size={artifacts['per_session_locks_count']}",
        artifacts,
    )


# ── 14. delete + delete_for_app remove session dir + index ──────────


@_scenario
def scenario_delete_removes_session(
    tmp: Path, daemon, client, timer,
) -> tuple[bool, str, dict[str, Any]]:
    from digitorn.core.runtime.session_store import (
        InMemorySessionStore, Event, SqliteSessionIndex,
    )
    artifacts: dict[str, Any] = {}

    async def go():
        idx = SqliteSessionIndex(db_path=tmp / "index.db")
        store = InMemorySessionStore(root=tmp / "sessions", index=idx)
        await store.start()
        try:
            # Create 3 sessions across 2 apps.
            for sid, app in [
                ("s1", "appA"), ("s2", "appA"), ("s3", "appB"),
            ]:
                await store.open(sid, app_id=app, user_id="u1", pin=False)
                await store.append_event(sid, Event(type="user_message", content="hi"))
            await store.flusher.flush()

            # delete one session
            existed = await store.delete("s1", force=True)
            artifacts["s1_existed"] = existed
            sdir_s1 = store._session_dir("s1")
            artifacts["s1_dir_exists_post_delete"] = sdir_s1.exists()

            # delete_for_app("appA") should remove s2 (s1 already gone)
            count = await store.delete_for_app("appA")
            artifacts["delete_for_app_appA"] = count
            sdir_s2 = store._session_dir("s2")
            artifacts["s2_dir_exists_post_app_delete"] = sdir_s2.exists()

            # appB unaffected
            sdir_s3 = store._session_dir("s3")
            artifacts["s3_dir_exists"] = sdir_s3.exists()
            artifacts["s3_in_memory"] = store.state("s3") is not None
        finally:
            await store.stop()

    _run(go())

    if not artifacts["s1_existed"]:
        return False, "delete s1 returned False (should be True)", artifacts
    if artifacts["s1_dir_exists_post_delete"]:
        return False, "session dir still on disk after delete", artifacts
    if artifacts["delete_for_app_appA"] != 1:
        return (
            False,
            f"delete_for_app expected to remove 1 session, got {artifacts['delete_for_app_appA']}",
            artifacts,
        )
    if artifacts["s2_dir_exists_post_app_delete"]:
        return False, "appA's other session still on disk", artifacts
    if not artifacts["s3_dir_exists"]:
        return False, "appB's session was wrongly deleted", artifacts
    return True, "delete + delete_for_app correctly scoped", artifacts


# ── 15. list_for_app filters by app_id (index + FS fallback) ──────


@_scenario
def scenario_list_for_app(
    tmp: Path, daemon, client, timer,
) -> tuple[bool, str, dict[str, Any]]:
    from digitorn.core.runtime.session_store import (
        InMemorySessionStore, Event, SqliteSessionIndex,
    )
    artifacts: dict[str, Any] = {}

    async def go():
        idx = SqliteSessionIndex(db_path=tmp / "index.db")
        store = InMemorySessionStore(root=tmp / "sessions", index=idx)
        await store.start()
        try:
            for sid, app in [
                ("s1", "appA"), ("s2", "appA"), ("s3", "appB"), ("s4", "appA"),
            ]:
                await store.open(sid, app_id=app, user_id="u1", pin=False)
                await store.append_event(sid, Event(type="user_message", content=f"hi from {sid}"))
                await store.close_session(sid)  # writes meta + index upsert
            await store.flusher.flush()

            # Index path
            list_a = await store.list_for_app("appA")
            list_b = await store.list_for_app("appB")
            artifacts["list_appA_index"] = sorted(s.session_id for s in list_a)
            artifacts["list_appB_index"] = sorted(s.session_id for s in list_b)

            # Filesystem fallback path (force by clearing the index)
            store._index = None
            list_a_fs = await store.list_for_app("appA")
            artifacts["list_appA_fs"] = sorted(s.session_id for s in list_a_fs)
        finally:
            await store.stop()

    _run(go())
    expected_a = ["s1", "s2", "s4"]
    expected_b = ["s3"]
    if artifacts["list_appA_index"] != expected_a:
        return False, f"index list_for_app(appA) wrong: {artifacts['list_appA_index']}", artifacts
    if artifacts["list_appB_index"] != expected_b:
        return False, f"index list_for_app(appB) wrong: {artifacts['list_appB_index']}", artifacts
    if artifacts["list_appA_fs"] != expected_a:
        return False, f"FS fallback list_for_app(appA) wrong: {artifacts['list_appA_fs']}", artifacts
    return True, "list_for_app filters correctly via index AND FS fallback", artifacts


# ── 16. get_any_owner cross-user lookup ─────────────────────────────


@_scenario
def scenario_get_any_owner(
    tmp: Path, daemon, client, timer,
) -> tuple[bool, str, dict[str, Any]]:
    from digitorn.core.runtime.session_store import InMemorySessionStore, Event
    artifacts: dict[str, Any] = {}

    async def go():
        store = InMemorySessionStore(root=tmp)
        await store.start()
        try:
            await store.open("s1", app_id="myapp", user_id="user-alpha", pin=False)
            await store.append_event("s1", Event(type="user_message", content="hi"))
            await store.flusher.flush()

            owner = await store.get_any_owner("myapp", "s1")
            artifacts["owner_correct_app"] = owner

            owner_wrong = await store.get_any_owner("otherapp", "s1")
            artifacts["owner_wrong_app"] = owner_wrong

            owner_missing = await store.get_any_owner("myapp", "doesnotexist")
            artifacts["owner_missing"] = owner_missing
        finally:
            await store.stop()

    _run(go())
    if artifacts["owner_correct_app"] != "user-alpha":
        return False, f"get_any_owner returned {artifacts['owner_correct_app']!r}", artifacts
    if artifacts["owner_wrong_app"] is not None:
        return False, f"wrong-app lookup leaked owner: {artifacts['owner_wrong_app']!r}", artifacts
    if artifacts["owner_missing"] is not None:
        return False, f"missing-sid lookup returned {artifacts['owner_missing']!r}", artifacts
    return True, "get_any_owner correctly scoped to (app_id, sid)", artifacts


# ── 17. recover_orphans marks unclosed sessions interrupted ────────


@_scenario
def scenario_recover_orphans(
    tmp: Path, daemon, client, timer,
) -> tuple[bool, str, dict[str, Any]]:
    from digitorn.core.runtime.session_store import InMemorySessionStore, Event
    artifacts: dict[str, Any] = {}

    async def go():
        # Phase A: write 3 sessions, close one cleanly. 2 stay
        # "live" (unclosed). Crash equivalent = stop without close.
        store1 = InMemorySessionStore(root=tmp)
        await store1.start()
        try:
            await store1.open("clean", app_id="a", user_id="u", pin=False)
            await store1.append_event("clean", Event(type="user_message", content="x"))
            await store1.close_session("clean")

            await store1.open("crash1", app_id="a", user_id="u", pin=False)
            await store1.append_event("crash1", Event(type="user_message", content="y"))

            await store1.open("crash2", app_id="b", user_id="u", pin=False)
            await store1.append_event("crash2", Event(type="user_message", content="z"))

            await store1.flusher.flush()
        finally:
            await store1.stop()

        # Phase B: fresh store on the same root simulates a daemon
        # restart. recover_orphans should mark crash1 + crash2 as
        # interrupted. The cleanly-closed session must NOT be touched.
        store2 = InMemorySessionStore(root=tmp)
        await store2.start()
        try:
            marked = await store2.recover_orphans()
            artifacts["marked"] = marked
        finally:
            await store2.stop()

        # Phase C: verify on disk.
        for sid, expected_interrupted in [
            ("clean", False), ("crash1", True), ("crash2", True),
        ]:
            sdir = store2._session_dir(sid)
            meta = json.loads((sdir / "meta.json").read_text(encoding="utf-8"))
            artifacts[f"{sid}_interrupted"] = bool(meta.get("interrupted"))
            artifacts[f"{sid}_interrupted_at"] = meta.get("interrupted_at")
        return artifacts

    _run(go())

    if artifacts["marked"] != 2:
        return False, f"recover_orphans should have marked 2, got {artifacts['marked']}", artifacts
    if artifacts["clean_interrupted"]:
        return False, "cleanly-closed session was wrongly marked interrupted", artifacts
    if not artifacts["crash1_interrupted"]:
        return False, "crash1 not marked interrupted", artifacts
    if not artifacts["crash2_interrupted"]:
        return False, "crash2 not marked interrupted", artifacts
    if not artifacts["crash1_interrupted_at"]:
        return False, "crash1 has no interrupted_at timestamp", artifacts
    return (
        True,
        f"recover_orphans correctly marked 2 unclosed sessions, left "
        f"clean session alone, stamped interrupted_at",
        artifacts,
    )


# ── 18. FileJobStore: put / get / list / delete + buffer FIFO ───────


@_scenario
def scenario_file_job_store(
    tmp: Path, daemon, client, timer,
) -> tuple[bool, str, dict[str, Any]]:
    from digitorn.core.runtime.session_store import FileJobStore
    from digitorn.core.app.job_store import PersistedWatcher, ScheduledJob
    artifacts: dict[str, Any] = {}

    fjs = FileJobStore(root=tmp / "jobs")

    # Watchers
    w1 = PersistedWatcher(
        watcher_id="w1", app_id="appA", tool_name="grep",
        params={"pattern": "ERROR"}, interval=10.0, label="errors",
    )
    fjs.put_watcher(w1)
    got = fjs.get_watcher("appA", "w1")
    artifacts["watcher_roundtrip"] = got is not None and got.tool_name == "grep"
    listed = fjs.list_watchers("appA")
    artifacts["list_watchers_appA"] = len(listed)
    fjs.delete_watcher("appA", "w1")
    artifacts["watcher_post_delete"] = fjs.get_watcher("appA", "w1") is None

    # Jobs
    j1 = ScheduledJob(
        job_id="j1", app_id="appA", schedule_type="interval",
        interval_seconds=60.0, action_type="tool_call",
        tool_name="ping", tool_params={},
    )
    j2 = ScheduledJob(
        job_id="j2", app_id="appB", schedule_type="cron",
        cron_expr="0 * * * *", action_type="tool_call",
        tool_name="rotate", tool_params={},
    )
    fjs.put_job(j1)
    fjs.put_job(j2)
    artifacts["jobs_appA"] = len(fjs.list_jobs(app_id="appA"))
    artifacts["jobs_all"] = len(fjs.list_jobs())
    fjs.delete_job("appA", "j1")
    artifacts["jobs_appA_post_del"] = len(fjs.list_jobs(app_id="appA"))

    # Notification buffer FIFO
    for i in range(5):
        fjs.buffer_notification("appA", {"i": i, "msg": f"hello {i}"})
    drained = fjs.drain_buffered("appA")
    artifacts["drained_count"] = len(drained)
    artifacts["drained_order"] = [d.get("i") for d in drained]
    # Second drain must be empty.
    drained2 = fjs.drain_buffered("appA")
    artifacts["drained_again"] = len(drained2)

    # Stats
    artifacts["stats"] = fjs.stats()

    failures = []
    if not artifacts["watcher_roundtrip"]:
        failures.append("watcher roundtrip broken")
    if artifacts["list_watchers_appA"] != 1:
        failures.append(f"list_watchers expected 1 got {artifacts['list_watchers_appA']}")
    if not artifacts["watcher_post_delete"]:
        failures.append("watcher delete didn't remove")
    if artifacts["jobs_appA"] != 1 or artifacts["jobs_all"] != 2:
        failures.append(f"job listing wrong: appA={artifacts['jobs_appA']} all={artifacts['jobs_all']}")
    if artifacts["jobs_appA_post_del"] != 0:
        failures.append("job delete didn't remove")
    if artifacts["drained_count"] != 5 or artifacts["drained_order"] != [0, 1, 2, 3, 4]:
        failures.append(f"buffer FIFO broken: order={artifacts['drained_order']}")
    if artifacts["drained_again"] != 0:
        failures.append(f"drain not idempotent: second drain returned {artifacts['drained_again']}")

    if failures:
        return False, "; ".join(failures), artifacts
    return True, "FileJobStore CRUD + buffer FIFO + drain idempotency OK", artifacts

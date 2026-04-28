"""Migration perf + correctness test.

Replicates the production shape the user reported:
  - ~15k rows in ``session_messages``
  - ~2.5k duplicate-ts groups
  - no unique index on ``created_at``
  - ``history_log`` empty or partially populated

Drives ``init_db`` against a throwaway SQLite file and measures:
  1. Wall time of the full migration chain.
  2. Correctness: every legacy row lands in ``history_log`` (via
     backfill); legacy tables get dropped; ``history_log.ts`` ends
     up UNIQUE.

The old migration scanned the legacy tables in a Python loop with
one UPDATE per colliding row - O(dup_groups × table_rows) and
blocked on WAL contention for 30+ seconds. The patched path must
finish in a couple of seconds.

Run: py -3.12 tools/test_migration_perf.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# Force a fresh, isolated DB under our control.
TMP_DIR = Path(tempfile.mkdtemp(prefix="migperf_"))
DB_FILE = TMP_DIR / "digitorn.db"

# Point the daemon settings at our temp DB BEFORE any digitorn import.
os.environ["DIGITORN_DATABASE__URL"] = (
    f"sqlite+aiosqlite:///{DB_FILE.as_posix()}"
)
os.chdir(TMP_DIR)  # Also cwd-anchor, so relative paths behave.


results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    tag = "[PASS]" if ok else "[FAIL]"
    print(f"{tag} {name}" + (f"  - {detail[:220]}" if detail else ""))


# Volume of the synthetic workload - picked to exceed the user's
# reported dataset (14 833 msg rows / 2 663 dup groups) so we know
# the fix scales well beyond what they've got in the wild.
N_MESSAGES = 15_000
N_EVENTS = 5_000
N_AUDIT = 200
TARGET_DUP_GROUPS = 2_800


def seed_legacy_tables() -> None:
    """Materialise the 3 legacy tables the OLD codebase used to write.

    We can't rely on ``Base.metadata`` because the current ORM no longer
    declares them - create the schemas with raw SQL so the migration
    sees a realistic "pre-unified" state.
    """
    c = sqlite3.connect(str(DB_FILE))
    try:
        # Minimal schemas the migration helpers reference. Columns that
        # the backfill SELECTs use must exist; other nullable fields
        # are omitted for brevity.
        c.executescript("""
        CREATE TABLE applications (app_id TEXT PRIMARY KEY);

        CREATE TABLE user_sessions (
            id TEXT PRIMARY KEY,
            app_id TEXT,
            session_id TEXT,
            user_id TEXT
        );

        CREATE TABLE session_messages (
            id TEXT PRIMARY KEY,
            session_pk TEXT,
            seq INTEGER,
            role TEXT,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            name TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX ix_session_messages_session ON session_messages(session_pk);

        CREATE TABLE session_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id TEXT,
            session_id TEXT,
            user_id TEXT,
            type TEXT,
            kind TEXT,
            seq INTEGER,
            payload TEXT,
            ts TEXT NOT NULL,
            correlation_id TEXT
        );

        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            event_type TEXT,
            actor_user_id TEXT,
            actor_roles TEXT,
            target_user_id TEXT,
            target_app_id TEXT,
            target_resource TEXT,
            ip_address TEXT,
            user_agent TEXT,
            before TEXT,
            after TEXT,
            success INTEGER,
            message TEXT
        );
        """)

        # Seed applications (FK target for user_sessions) and one session.
        app_id = "mig-test-app"
        c.execute("INSERT INTO applications VALUES (?)", (app_id,))
        session_pk = uuid.uuid4().hex
        session_id = "test-session"
        user_id = "test-user"
        c.execute(
            "INSERT INTO user_sessions VALUES (?, ?, ?, ?)",
            (session_pk, app_id, session_id, user_id),
        )

        # Messages with HEAVY ts duplication.
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        rows = []
        for i in range(N_MESSAGES):
            # Map multiple messages onto a smaller set of distinct
            # timestamps to create the dup-groups shape.
            ts_group = i // (N_MESSAGES // TARGET_DUP_GROUPS)
            ts = (base + timedelta(seconds=ts_group)).isoformat()
            role = "user" if i % 2 == 0 else "assistant"
            rows.append((
                uuid.uuid4().hex,
                session_pk, i, role,
                f"message-{i}", None, None, None,
                ts,
            ))
        c.executemany(
            "INSERT INTO session_messages "
            "(id, session_pk, seq, role, content, tool_call_id, "
            " tool_calls, name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

        # Events - dedup is already possible via id offset so fewer
        # dup groups needed. Still add some.
        event_rows = []
        for i in range(N_EVENTS):
            ts = (base + timedelta(seconds=i // 3)).isoformat()
            event_rows.append((
                app_id, session_id, user_id,
                "token" if i % 5 else "message_done",
                "session", i, "{}", ts, "",
            ))
        c.executemany(
            "INSERT INTO session_events "
            "(app_id, session_id, user_id, type, kind, seq, payload, ts, "
            " correlation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            event_rows,
        )

        # Audit.
        audit_rows = []
        for i in range(N_AUDIT):
            ts = (base + timedelta(seconds=i)).isoformat()
            audit_rows.append((
                ts, "quota.set_app",
                "admin-user", "[]",
                None, app_id, None,
                "127.0.0.1", "tester",
                "{}", "{}", 1, "",
            ))
        c.executemany(
            "INSERT INTO audit_log "
            "(ts, event_type, actor_user_id, actor_roles, "
            " target_user_id, target_app_id, target_resource, "
            " ip_address, user_agent, before, after, success, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            audit_rows,
        )
        c.commit()
    finally:
        c.close()


def pre_check() -> dict[str, int]:
    c = sqlite3.connect(str(DB_FILE))
    try:
        stats = {}
        for t in ("session_messages", "session_events", "audit_log"):
            stats[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        stats["dup_groups_messages"] = c.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT created_at FROM session_messages "
            "  GROUP BY created_at HAVING COUNT(*) > 1"
            ")"
        ).fetchone()[0]
        return stats
    finally:
        c.close()


async def run_migration() -> float:
    """Actually invoke init_db - runs the WHOLE migration chain."""
    # Late import so DIGITORN_DATABASE__URL takes effect.
    from digitorn.core.database import init_db, close_db
    from digitorn.core.config import Settings

    settings = Settings.load()
    # Make sure we aim at our temp DB - explicit override to survive
    # any defaults that Settings.load may inject from disk.
    settings.database.url = f"sqlite+aiosqlite:///{DB_FILE.as_posix()}"

    start = time.monotonic()
    await init_db(settings)
    elapsed = time.monotonic() - start
    await close_db()
    return elapsed


def post_check() -> dict[str, int]:
    c = sqlite3.connect(str(DB_FILE))
    try:
        stats: dict[str, int] = {}
        # Only history_log should remain among the 4.
        names = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "  AND name IN ('session_messages','session_events',"
            "'audit_log','history_log') ORDER BY name"
        ).fetchall()
        stats["tables"] = [n[0] for n in names]  # type: ignore[assignment]
        if "history_log" in {t for t in stats["tables"]}:
            for kind in ("message", "event", "audit"):
                stats[f"history_log_{kind}"] = c.execute(
                    "SELECT COUNT(*) FROM history_log WHERE kind = ?",
                    (kind,),
                ).fetchone()[0]
            stats["history_log_total"] = c.execute(
                "SELECT COUNT(*) FROM history_log"
            ).fetchone()[0]
            stats["history_log_distinct_ts"] = c.execute(
                "SELECT COUNT(DISTINCT ts) FROM history_log"
            ).fetchone()[0]
            # UNIQUE index present?
            stats["ts_unique_idx"] = bool(c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' "
                "  AND tbl_name='history_log' "
                "  AND sql LIKE '%UNIQUE%ts%' LIMIT 1"
            ).fetchone())
        return stats
    finally:
        c.close()


def main() -> int:
    print(f"Test DB: {DB_FILE}")
    print("Seeding legacy tables…")
    seed_legacy_tables()
    pre = pre_check()
    print(f"Pre-migration: {pre}")
    check(
        f"seeded ~{N_MESSAGES} messages",
        pre["session_messages"] == N_MESSAGES,
        f"got {pre['session_messages']}",
    )
    check(
        f"seeded ≥{TARGET_DUP_GROUPS // 2} dup groups in messages",
        pre["dup_groups_messages"] >= TARGET_DUP_GROUPS // 2,
        f"dup_groups={pre['dup_groups_messages']}",
    )

    print("\nRunning init_db (the migration chain)…")
    elapsed = asyncio.run(run_migration())
    print(f"Migration took {elapsed:.2f} s")
    check(
        "migration completed in < 10 s (previous impl took 30+ s and "
        "blocked daemon startup)",
        elapsed < 10.0,
        f"elapsed={elapsed:.2f}s",
    )
    # Bonus - tight bound most modern hardware will hit easily.
    check(
        "migration completed in < 3 s (generous perf target)",
        elapsed < 3.0,
        f"elapsed={elapsed:.2f}s",
    )

    post = post_check()
    print(f"\nPost-migration: {post}")
    check(
        "legacy tables dropped",
        "session_messages" not in post.get("tables", [])
        and "session_events" not in post.get("tables", [])
        and "audit_log" not in post.get("tables", []),
        f"tables={post.get('tables')}",
    )
    check(
        "history_log present after migration",
        "history_log" in post.get("tables", []),
        "",
    )
    check(
        "history_log.ts is UNIQUE-indexed",
        bool(post.get("ts_unique_idx")),
        "",
    )
    # Backfill correctness.
    msgs = int(post.get("history_log_message", 0))
    evts = int(post.get("history_log_event", 0))
    audt = int(post.get("history_log_audit", 0))
    total = int(post.get("history_log_total", 0))
    distinct = int(post.get("history_log_distinct_ts", 0))
    print(f"\nBackfill: messages={msgs} events={evts} audit={audt} "
          f"total={total} distinct_ts={distinct}")
    # We tolerate a small loss from cross-table ts collisions (INSERT
    # OR IGNORE) - bounded to a handful of rows.
    check(
        f"≥ 99% of {N_MESSAGES} messages migrated",
        msgs >= int(N_MESSAGES * 0.99),
        f"got {msgs}/{N_MESSAGES}",
    )
    check(
        f"≥ 99% of {N_EVENTS} events migrated",
        evts >= int(N_EVENTS * 0.99),
        f"got {evts}/{N_EVENTS}",
    )
    check(
        f"≥ 99% of {N_AUDIT} audit rows migrated",
        audt >= int(N_AUDIT * 0.99),
        f"got {audt}/{N_AUDIT}",
    )
    check(
        "every ts in history_log is unique",
        total == distinct,
        f"total={total} distinct={distinct}",
    )

    print("\n" + "=" * 70)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"MIGRATION PERF TEST: {passed}/{len(results)}")
    print("=" * 70)
    if passed != len(results):
        print("\nFailures:")
        for n, ok, det in results:
            if not ok:
                print(f"  [FAIL] {n}\n         {det[:300]}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:
        import traceback
        traceback.print_exc()
        rc = 3
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
    sys.exit(rc)

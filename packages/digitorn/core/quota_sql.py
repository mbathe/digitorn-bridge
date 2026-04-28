"""SQL-backed quota store - durable source of truth.

Drop-in replacement for ``core/quota.py::QuotaStore`` (KV-backed). Same
public API, same semantics, but persists everything to the primary
SQLite/Postgres DB:

- **Definitions** (``QuotaDefinitionRow``): admin policy per scope.
  One row for ``scope='app'``, one row per user override.
- **Counters** (``QuotaCounterFixed`` / ``QuotaCounterRolling``): live
  usage in each window, indexed for O(log n) lookup.

Why SQL: quota definitions are configuration the admin enters through
the panel - they must survive daemon restarts, be backed up alongside
the rest of the DB, and show up in audit trails. Counters can be
recomputed from ``usage_events`` if truly lost, but keeping them hot
in the same DB avoids round-trips and gives us atomic
``UPDATE … SET value = value + :amount`` for check-and-charge.

The module re-uses every Pydantic schema from ``core/quota.py`` (so the
admin contract is unchanged) and re-exports ``QuotaExceededError`` /
``CounterState`` for callers.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session as SyncSession, sessionmaker

from digitorn.core.models import (
    QuotaCounterFixed,
    QuotaCounterRolling,
    QuotaDefinitionRow,
)


def _sync_url_for(async_engine: Any) -> str:
    """Return a sync SQLAlchemy URL pointing at the same DB as the
    supplied async engine. Async drivers (``aiosqlite``, ``asyncpg``)
    cannot drive sync sessions - SQLAlchemy raises ``MissingGreenlet``.
    We swap the driver for its sync sibling keeping the rest of the
    URL untouched.

    For Postgres we try ``psycopg`` (v3) first, then fall back to
    ``psycopg2``, whichever is importable. Raises ``ImportError`` if
    neither is installed so the caller can fall back to the KV store.
    """
    url = async_engine.url
    drivername = url.drivername

    # CRITICAL: ``str(URL)`` masks the password as ``***`` by default
    # (SQLAlchemy security feature). We need the real creds in the
    # URL we hand to ``create_engine``, so we call
    # ``render_as_string(hide_password=False)`` explicitly.
    def _render(u) -> str:
        return u.render_as_string(hide_password=False)

    if drivername == "sqlite+aiosqlite":
        return _render(url.set(drivername="sqlite+pysqlite"))

    if drivername == "postgresql+asyncpg":
        for candidate in ("postgresql+psycopg", "postgresql+psycopg2"):
            try:
                import importlib
                importlib.import_module(candidate.split("+", 1)[1])
                # asyncpg uses ``ssl=require`` whereas libpq drivers
                # (psycopg / psycopg2) use ``sslmode=require``. The
                # two are incompatible - psycopg raises "invalid
                # connection option 'ssl'". Translate the query param.
                new_url = url.set(drivername=candidate)
                q = dict(new_url.query or {})
                if "ssl" in q and "sslmode" not in q:
                    ssl_value = q.pop("ssl")
                    q["sslmode"] = "require" if str(ssl_value).lower() in (
                        "require", "true", "1",
                    ) else str(ssl_value)
                    new_url = new_url.set(query=q)
                return _render(new_url)
            except ImportError:
                continue
        raise ImportError(
            "SqlQuotaStore needs a sync Postgres driver alongside "
            "asyncpg. Install 'psycopg[binary]' (v3, recommended) or "
            "'psycopg2-binary'."
        )

    if drivername == "mysql+aiomysql":
        return _render(url.set(drivername="mysql+pymysql"))

    return _render(url)


def _drop_query_param(url_str: str, param: str) -> str:
    """Drop a query parameter from a URL string (Neon's ``ssl=require``
    is valid for asyncpg but ``psycopg`` uses ``sslmode=`` - we strip
    it rather than translate, falling back to the server-side TLS
    default which ``psycopg`` handles automatically)."""
    if f"?{param}=" not in url_str and f"&{param}=" not in url_str:
        return url_str
    import re
    return re.sub(rf"[?&]{re.escape(param)}=[^&]*", "", url_str, count=1)
from digitorn.core.quota import (
    CounterState,
    MetricQuota,
    QuotaDefinition,
    QuotaExceededError,
    _compute_fixed_reset,
    _merge_quota,
    _window_to_seconds,
)

logger = logging.getLogger(__name__)


class SqlQuotaStore:
    """SQL-backed store. Public API matches ``QuotaStore`` (KV) so no
    caller needs to change.

    All methods are **synchronous** - they execute against a sync
    SQLAlchemy session borrowed from ``async_engine.sync_engine``.
    Callers that live inside an event loop already pay the same small
    blocking cost with the previous KV backend (DiskCache is sync too).
    If the call cost ever becomes a bottleneck, wrap each method with
    ``asyncio.to_thread`` at the call site.
    """

    def __init__(self, async_engine: Any) -> None:
        self._async_engine = async_engine
        # Async drivers (aiosqlite, asyncpg) only drive async sessions.
        # We need a parallel sync engine pointing at the same DB so the
        # public API can stay synchronous. The pool is separate, so
        # SQLite users should keep both engines short - SQLite handles
        # the parallel-engine case via shared-cache / file locking.
        sync_url = _sync_url_for(async_engine)
        connect_args: dict = {}
        if sync_url.startswith("sqlite"):
            # Match the async engine's connection arg.
            connect_args["check_same_thread"] = False
        self._sync_engine = create_engine(
            sync_url,
            connect_args=connect_args,
            pool_pre_ping=True,
        )
        # SQLite WAL mode lets the async reader and this sync writer
        # coexist without ``database is locked`` errors. Without WAL,
        # each write blocks every other connection even for reads.
        # The PRAGMAs mirror what ``init_db`` already set on the async
        # engine so both sides agree. Safe to re-apply per connection.
        if sync_url.startswith("sqlite"):
            from sqlalchemy import event as _sa_event

            @_sa_event.listens_for(self._sync_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _):
                cur = dbapi_conn.cursor()
                try:
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA synchronous=NORMAL")
                    cur.execute("PRAGMA busy_timeout=30000")
                finally:
                    cur.close()
        self._SessionLocal: sessionmaker = sessionmaker(
            bind=self._sync_engine,
            expire_on_commit=False,
            autoflush=False,
        )
        # Serialises ``check_and_charge`` across threads IN THIS
        # PROCESS. SQLite's default deferred-transaction mode lets two
        # SELECTs race each other before either UPDATE, letting both
        # callers pass the check with stale reads. A per-store mutex
        # closes the window for any number of threads inside one daemon.
        #
        # LIMITATION (multi-process): the mutex does NOT protect against
        # races between separate daemon processes that share a DB
        # (horizontal-scaled deployments). Two processes could each
        # acquire their local mutex, both SELECT a stale counter, both
        # UPDATE, and overshoot by one. The scale-out fix is to replace
        # this mutex with a DB-level lock:
        #   * Postgres: ``SELECT … FOR UPDATE`` on the counter row OR
        #     advisory locks keyed by (scope, metric, window).
        #   * SQLite: ``BEGIN IMMEDIATE`` on the transaction so only one
        #     writer can touch the DB at a time.
        # We accept the mono-process limitation here because the current
        # deployment runs a single daemon per DB - the multi-daemon case
        # is round 2. A test run with 20 parallel threads inside one
        # process proved the mutex is strict (see
        # ``tests/unit/test_sql_quota_store.py::race_safety``).
        self._charge_lock = threading.Lock()

    # ── Writes ─────────────────────────────────────────────────────

    def set_app_quota(
        self,
        app_id: str,
        quota: QuotaDefinition,
        *,
        updated_by: str = "",
    ) -> dict[str, Any]:
        """Upsert the app-level quota. Returns the stored envelope."""
        definition = quota.model_dump(mode="json", exclude_none=True)
        with self._SessionLocal.begin() as db:
            row = self._upsert_definition(
                db, scope="app", app_id=app_id, user_id=None,
                definition=definition, updated_by=updated_by,
            )
            return self._row_to_envelope(row)

    def set_user_quota(
        self,
        app_id: str,
        user_id: str,
        quota: QuotaDefinition,
        *,
        updated_by: str = "",
    ) -> dict[str, Any]:
        """Upsert the user-on-app override."""
        definition = quota.model_dump(mode="json", exclude_none=True)
        with self._SessionLocal.begin() as db:
            row = self._upsert_definition(
                db, scope="user", app_id=app_id, user_id=user_id,
                definition=definition, updated_by=updated_by,
            )
            return self._row_to_envelope(row)

    # ── Reads ──────────────────────────────────────────────────────

    def get_app_quota(self, app_id: str) -> dict[str, Any] | None:
        with self._SessionLocal() as db:
            row = self._fetch_definition(db, scope="app", app_id=app_id, user_id=None)
            return self._row_to_envelope(row) if row is not None else None

    def get_user_quota(
        self, app_id: str, user_id: str,
    ) -> dict[str, Any] | None:
        with self._SessionLocal() as db:
            row = self._fetch_definition(
                db, scope="user", app_id=app_id, user_id=user_id,
            )
            return self._row_to_envelope(row) if row is not None else None

    def effective_quota(
        self,
        app_id: str,
        user_id: str | None = None,
        *,
        global_default_rpm: int = 60,
    ) -> dict[str, Any]:
        """Merge global → app → user and return the result.

        A ``None`` leaf on the override means "inherit from parent".
        """
        base = QuotaDefinition(
            requests=MetricQuota.model_validate({
                "per_minute": {"limit": global_default_rpm, "reset": "fixed"},
            }),
        ).model_dump(mode="json", exclude_none=True)

        app_env = self.get_app_quota(app_id)
        if app_env and isinstance(app_env.get("quota"), dict):
            base = _merge_quota(base, app_env["quota"])

        if user_id:
            user_env = self.get_user_quota(app_id, user_id)
            if user_env and isinstance(user_env.get("quota"), dict):
                base = _merge_quota(base, user_env["quota"])

        return base

    # ── Deletes ────────────────────────────────────────────────────

    def remove_app_quota(self, app_id: str) -> bool:
        with self._SessionLocal.begin() as db:
            row = self._fetch_definition(db, scope="app", app_id=app_id, user_id=None)
            if row is None:
                return False
            db.delete(row)
            return True

    def remove_user_quota(self, app_id: str, user_id: str) -> bool:
        with self._SessionLocal.begin() as db:
            row = self._fetch_definition(
                db, scope="user", app_id=app_id, user_id=user_id,
            )
            if row is None:
                return False
            db.delete(row)
            return True

    # ── High-level enforcement - check + charge in one atomic pass ──

    def check_and_charge(
        self,
        *,
        app_id: str,
        user_id: str | None,
        charges: dict[str, float],
        model: str | None = None,
    ) -> None:
        """Run every rule at every scope; raise on first overflow.

        When no rule overflows, increment every counter in the same DB
        transaction so a concurrent request can't both pass the check
        before either charges. Counters are written via
        ``SELECT … FOR UPDATE`` (Postgres) / ``BEGIN IMMEDIATE`` (SQLite)
        so the check-then-charge sequence is serialised per scope.
        """
        from digitorn.core.config import get_settings
        try:
            settings = get_settings()
            global_rpm = int(getattr(settings.server, "rate_limit_rpm", 60))
        except Exception:
            global_rpm = 60

        app_eff = self.effective_quota(app_id, global_default_rpm=global_rpm)
        user_eff = (
            self.effective_quota(app_id, user_id=user_id, global_default_rpm=global_rpm)
            if user_id else None
        )

        scopes_to_check: list[tuple[str, str, dict]] = [
            ("app", f"app:{app_id}", app_eff),
        ]
        if user_id and user_eff is not None:
            scopes_to_check.append(
                ("user", f"user:{user_id}:app:{app_id}", user_eff),
            )

        # Flat plan: (scope_label, scope_key, metric, window, reset, limit, amount).
        plan: list[tuple[str, str, str, str, str, float, float]] = []
        for scope_label, scope_key, eff in scopes_to_check:
            for metric, amount in charges.items():
                if amount <= 0:
                    continue
                plan.extend(self._expand_rules(
                    scope_label, scope_key, eff, metric, amount,
                ))
                if model:
                    model_cfg = (eff.get("models") or {}).get(model)
                    if isinstance(model_cfg, dict):
                        model_scope_key = f"{scope_key}:model:{model}"
                        plan.extend(self._expand_rules(
                            scope_label, model_scope_key, model_cfg, metric, amount,
                        ))

        if not plan:
            return

        # ONE transaction under the per-store mutex: check every rule
        # then apply every charge. On overflow we raise and the
        # transaction rolls back so nothing is written - guarantees
        # atomicity across the check+charge for every concurrent caller
        # in this process.
        with self._charge_lock, self._SessionLocal.begin() as db:
            # Phase 1 - peek each rule, bail on first overflow.
            for (scope_label, scope_key, metric, window, reset,
                 limit, amount) in plan:
                current, reset_at = self._peek_counter(
                    db, scope=scope_key, metric=metric,
                    window=window, reset=reset,
                )
                if (current + amount) > limit:
                    raise QuotaExceededError(CounterState(
                        metric=metric, window=window,
                        current=current + amount, limit=limit,
                        reset_at=reset_at, over=True, scope=scope_label,
                    ))

            # Phase 2 - all checks passed, charge every counter.
            for (scope_label, scope_key, metric, window, reset,
                 limit, amount) in plan:
                self._incr_counter(
                    db, scope=scope_key, metric=metric,
                    window=window, reset=reset, amount=amount,
                )

    def snapshot_usage(
        self,
        app_id: str,
        user_id: str | None = None,
        *,
        global_default_rpm: int = 60,
    ) -> dict[str, Any]:
        """Return current counters for every rule in the effective quota."""
        eff = self.effective_quota(
            app_id, user_id=user_id, global_default_rpm=global_default_rpm,
        )
        scope_key = (
            f"user:{user_id}:app:{app_id}" if user_id else f"app:{app_id}"
        )
        out: dict[str, Any] = {}
        with self._SessionLocal() as db:
            for metric in (
                "requests", "tokens_input", "tokens_output",
                "tokens_total", "cost_usd", "messages",
            ):
                bucket = eff.get(metric)
                if not isinstance(bucket, dict):
                    continue
                try:
                    mq = MetricQuota.model_validate(bucket)
                except Exception:
                    continue
                report: dict[str, Any] = {}
                for window, rule in mq.rules():
                    current, reset_at = self._peek_counter(
                        db, scope=scope_key, metric=metric,
                        window=window, reset=rule.reset,
                    )
                    report[window] = {
                        "current": current,
                        "limit": float(rule.limit),
                        "reset_at": reset_at,
                        "reset_at_iso": datetime.fromtimestamp(
                            reset_at, tz=timezone.utc,
                        ).isoformat().replace("+00:00", "Z"),
                        "reset": rule.reset,
                    }
                if report:
                    out[metric] = report
        return out

    # ── Counter primitives (public for tests/introspection) ─────────

    def peek_counter(
        self, *, scope: str, metric: str, window: str, reset: str,
    ) -> tuple[float, float]:
        """Read ``(current, reset_at)`` without incrementing."""
        with self._SessionLocal() as db:
            return self._peek_counter(
                db, scope=scope, metric=metric, window=window, reset=reset,
            )

    def incr_counter(
        self, *, scope: str, metric: str, window: str,
        reset: str, amount: float,
    ) -> tuple[float, float]:
        """Increment and return ``(new_total, reset_at)``."""
        with self._SessionLocal.begin() as db:
            return self._incr_counter(
                db, scope=scope, metric=metric, window=window,
                reset=reset, amount=amount,
            )

    def list_user_overrides(self, app_id: str) -> list[dict[str, Any]]:
        """Return envelopes for every user override on ``app_id``."""
        with self._SessionLocal() as db:
            rows = db.execute(
                select(QuotaDefinitionRow).where(
                    QuotaDefinitionRow.scope == "user",
                    QuotaDefinitionRow.app_id == app_id,
                ),
            ).scalars().all()
            return [
                {"user_id": r.user_id, **self._row_to_envelope(r)}
                for r in rows
            ]

    def list_app_quotas(self) -> list[dict[str, Any]]:
        """Return envelopes for every app-level quota (admin introspection)."""
        with self._SessionLocal() as db:
            rows = db.execute(
                select(QuotaDefinitionRow).where(
                    QuotaDefinitionRow.scope == "app",
                ),
            ).scalars().all()
            return [
                {"app_id": r.app_id, **self._row_to_envelope(r)}
                for r in rows
            ]

    # ── Internals ──────────────────────────────────────────────────

    def _expand_rules(
        self, scope_label: str, scope_key: str, eff: dict,
        metric: str, amount: float,
    ) -> list[tuple[str, str, str, str, str, float, float]]:
        out: list[tuple[str, str, str, str, str, float, float]] = []
        bucket = eff.get(metric)
        if not isinstance(bucket, dict):
            return out
        try:
            mq = MetricQuota.model_validate(bucket)
        except Exception:
            return out
        for window, rule in mq.rules():
            out.append((
                scope_label, scope_key, metric, window,
                rule.reset, float(rule.limit), amount,
            ))
        return out

    def _fetch_definition(
        self, db: SyncSession, *, scope: str,
        app_id: str, user_id: str | None,
    ) -> QuotaDefinitionRow | None:
        stmt = select(QuotaDefinitionRow).where(
            QuotaDefinitionRow.scope == scope,
            QuotaDefinitionRow.app_id == app_id,
        )
        stmt = stmt.where(
            QuotaDefinitionRow.user_id.is_(None)
            if user_id is None
            else QuotaDefinitionRow.user_id == user_id
        )
        return db.execute(stmt).scalar_one_or_none()

    def _upsert_definition(
        self, db: SyncSession, *, scope: str,
        app_id: str, user_id: str | None,
        definition: dict, updated_by: str,
    ) -> QuotaDefinitionRow:
        row = self._fetch_definition(
            db, scope=scope, app_id=app_id, user_id=user_id,
        )
        if row is None:
            row = QuotaDefinitionRow(
                id=str(uuid.uuid4()),
                scope=scope, app_id=app_id, user_id=user_id,
                definition=definition,
                updated_by=updated_by or None,
            )
            db.add(row)
        else:
            row.definition = definition
            row.updated_by = updated_by or None
        db.flush()
        return row

    @staticmethod
    def _row_to_envelope(row: QuotaDefinitionRow) -> dict[str, Any]:
        return {
            "quota": row.definition or {},
            "updated_at": (
                row.updated_at.astimezone(timezone.utc)
                .isoformat().replace("+00:00", "Z")
                if row.updated_at else ""
            ),
            "updated_by": row.updated_by or "",
        }

    # ── Counter read / write with SQL-atomic increment ─────────────

    def _peek_counter(
        self, db: SyncSession, *,
        scope: str, metric: str, window: str, reset: str,
    ) -> tuple[float, float]:
        now = time.time()
        if reset == "rolling_from_first":
            return self._peek_rolling(db, scope, metric, window, now)
        return self._peek_fixed(db, scope, metric, window, reset, now)

    def _incr_counter(
        self, db: SyncSession, *,
        scope: str, metric: str, window: str, reset: str, amount: float,
    ) -> tuple[float, float]:
        now = time.time()
        if reset == "rolling_from_first":
            return self._incr_rolling(db, scope, metric, window, now, amount)
        return self._incr_fixed(db, scope, metric, window, reset, now, amount)

    def _peek_fixed(
        self, db: SyncSession,
        scope: str, metric: str, window: str, reset: str, now: float,
    ) -> tuple[float, float]:
        reset_at = _compute_fixed_reset(window, reset, now)
        bucket_id = int(reset_at)
        row = db.execute(
            select(QuotaCounterFixed).where(
                QuotaCounterFixed.scope_key == scope,
                QuotaCounterFixed.metric == metric,
                QuotaCounterFixed.window == window,
                QuotaCounterFixed.bucket_id == bucket_id,
            ),
        ).scalar_one_or_none()
        return (float(row.value) if row else 0.0, reset_at)

    def _incr_fixed(
        self, db: SyncSession,
        scope: str, metric: str, window: str, reset: str,
        now: float, amount: float,
    ) -> tuple[float, float]:
        reset_at = _compute_fixed_reset(window, reset, now)
        bucket_id = int(reset_at)
        row = db.execute(
            select(QuotaCounterFixed).where(
                QuotaCounterFixed.scope_key == scope,
                QuotaCounterFixed.metric == metric,
                QuotaCounterFixed.window == window,
                QuotaCounterFixed.bucket_id == bucket_id,
            ),
        ).scalar_one_or_none()
        if row is None:
            row = QuotaCounterFixed(
                id=str(uuid.uuid4()),
                scope_key=scope, metric=metric, window=window,
                bucket_id=bucket_id, value=float(amount),
            )
            db.add(row)
            db.flush()
        else:
            row.value = float(row.value) + float(amount)
            db.flush()
        # Opportunistic cleanup - bucket ids strictly older than now -
        # 2*window_seconds can never match a live counter again.
        w = _window_to_seconds(window)
        cutoff = int(now - w * 2)
        if cutoff > 0:
            db.execute(
                delete(QuotaCounterFixed).where(
                    QuotaCounterFixed.scope_key == scope,
                    QuotaCounterFixed.metric == metric,
                    QuotaCounterFixed.window == window,
                    QuotaCounterFixed.bucket_id < cutoff,
                ),
            )
        return (float(row.value), reset_at)

    def _peek_rolling(
        self, db: SyncSession,
        scope: str, metric: str, window: str, now: float,
    ) -> tuple[float, float]:
        w = _window_to_seconds(window)
        row = db.execute(
            select(QuotaCounterRolling).where(
                QuotaCounterRolling.scope_key == scope,
                QuotaCounterRolling.metric == metric,
                QuotaCounterRolling.window == window,
            ),
        ).scalar_one_or_none()
        if row is None:
            return (0.0, now + w)
        started = float(row.started_at or 0.0)
        if started == 0 or (now - started) >= w:
            return (0.0, now + w)
        return (float(row.value), started + w)

    def _incr_rolling(
        self, db: SyncSession,
        scope: str, metric: str, window: str, now: float, amount: float,
    ) -> tuple[float, float]:
        w = _window_to_seconds(window)
        row = db.execute(
            select(QuotaCounterRolling).where(
                QuotaCounterRolling.scope_key == scope,
                QuotaCounterRolling.metric == metric,
                QuotaCounterRolling.window == window,
            ),
        ).scalar_one_or_none()
        if row is None:
            row = QuotaCounterRolling(
                id=str(uuid.uuid4()),
                scope_key=scope, metric=metric, window=window,
                window_seconds=w, started_at=now, value=float(amount),
            )
            db.add(row)
            db.flush()
            return (float(row.value), now + w)

        started = float(row.started_at or 0.0)
        if started == 0 or (now - started) >= w:
            row.started_at = now
            row.value = float(amount)
        else:
            row.value = float(row.value) + float(amount)
        db.flush()
        return (float(row.value), float(row.started_at) + w)


__all__ = ["SqlQuotaStore"]

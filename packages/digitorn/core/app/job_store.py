"""JobStore — persistence for watchers, scheduled jobs, and notification buffers.

Follows the same KV + secondary-index pattern as ``SessionStore``.

KV key scheme::

    watcher:{app_id}:{watcher_id}       → PersistedWatcher (dict)
    __watcher_index__{app_id}            → set[watcher_id]
    job:{app_id}:{job_id}               → ScheduledJob (dict)
    __job_index__{app_id}               → set[job_id]
    __all_active_jobs__                  → set[app_id:job_id]
    notif_buf:{app_id}                   → list[dict]   (FIFO, max N, TTL 24h)

Usage::

    store = JobStore(backend=shared_kv_backend)

    store.put_watcher(pw)
    pw = store.get_watcher("myapp", "abc123")

    store.put_job(job)
    job = store.get_job("myapp", "abc123")

    store.buffer_notification("myapp", {"type": "watcher", ...})
    pending = store.drain_buffered("myapp")
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from digitorn.core.kv import KeyValueBackend

logger = logging.getLogger(__name__)

_DEFAULT_BUFFER_MAX = 100
_DEFAULT_BUFFER_TTL = 86400.0


@dataclass
class PersistedWatcher:
    """Serializable form of a live Watcher, stored in KV for restart survival."""

    watcher_id: str
    app_id: str
    tool_name: str
    params: dict[str, Any] = field(default_factory=dict)
    interval: float = 30.0
    label: str = ""
    notify_when: str = "on_change"
    notify_config: dict[str, Any] = field(default_factory=dict)
    max_checks: int = 0
    session_id: str | None = None
    status: str = "running"
    check_count: int = 0
    notify_count: int = 0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersistedWatcher:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ScheduledJob:
    """A scheduled task persisted in KV. Unifies timers, cron, and reminders.

    ``schedule_type`` determines how ``next_run_at`` is computed:
    - ``"once"``     — fires at ``run_at`` (ISO 8601 or epoch), then completes.
    - ``"cron"``     — fires at times matching ``cron_expr`` (5-field cron).
    - ``"interval"`` — fires every ``interval_seconds`` seconds.

    ``action_type`` determines what happens when the job fires:
    - ``"tool_call"``     — calls ``tool_name`` with ``tool_params``.
    - ``"llm_prompt"``    — injects ``prompt`` as a system message for the LLM.
    - ``"notification"``  — sends ``prompt`` as a plain notification.

    ``session_id`` identifies which user created this job, so that output
    channels can auto-resolve per-user delivery targets (email, phone, etc.)
    via the ``user_resolver`` mechanism.
    """

    job_id: str
    app_id: str
    schedule_type: str
    action_type: str

    cron_expr: str | None = None
    run_at: str | None = None
    interval_seconds: float | None = None

    tool_name: str | None = None
    tool_params: dict[str, Any] = field(default_factory=dict)
    prompt: str | None = None

    label: str = ""
    output_channel: str = "llm_notification"
    output_config: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    status: str = "active"
    run_count: int = 0
    max_runs: int = 0
    last_run_at: str | None = None
    next_run_at: str | None = None
    last_result: Any = None
    last_error: str | None = None
    created_at: float = field(default_factory=time.time)
    memory_context: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduledJob:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def to_summary(self) -> dict[str, Any]:
        """Compact summary for list/status responses."""
        return {
            "job_id": self.job_id,
            "app_id": self.app_id,
            "schedule_type": self.schedule_type,
            "action_type": self.action_type,
            "label": self.label,
            "status": self.status,
            "run_count": self.run_count,
            "max_runs": self.max_runs,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "created_at": self.created_at,
        }


class JobStore:
    """Backend-agnostic persistence for watchers, jobs, and notification buffers.

    Args:
        backend: A ``KeyValueBackend`` instance (DiskCache or Redis).
        buffer_max: Max buffered notifications per app (FIFO eviction).
        buffer_ttl: TTL for the notification buffer key (seconds).
    """

    def __init__(
        self,
        backend: KeyValueBackend,
        buffer_max: int = _DEFAULT_BUFFER_MAX,
        buffer_ttl: float = _DEFAULT_BUFFER_TTL,
    ) -> None:
        self._backend = backend
        self._buffer_max = buffer_max
        self._buffer_ttl = buffer_ttl


    @staticmethod
    def _watcher_key(app_id: str, watcher_id: str) -> str:
        return f"watcher:{app_id}:{watcher_id}"

    @staticmethod
    def _watcher_index_key(app_id: str) -> str:
        return f"__watcher_index__{app_id}"

    def put_watcher(self, pw: PersistedWatcher) -> None:
        """Store or update a persisted watcher."""
        key = self._watcher_key(pw.app_id, pw.watcher_id)
        self._backend.set(key, pw.to_dict())
        idx_key = self._watcher_index_key(pw.app_id)
        ids: set[str] = self._backend.get(idx_key, set())
        ids.add(pw.watcher_id)
        self._backend.set(idx_key, ids)

    def get_watcher(self, app_id: str, watcher_id: str) -> PersistedWatcher | None:
        """Get a persisted watcher. Returns None if missing."""
        data = self._backend.get(self._watcher_key(app_id, watcher_id))
        if data is None:
            return None
        return PersistedWatcher.from_dict(data)

    def delete_watcher(self, app_id: str, watcher_id: str) -> bool:
        """Delete a persisted watcher. Returns True if it existed."""
        key = self._watcher_key(app_id, watcher_id)
        existed = self._backend.delete(key)
        if existed:
            idx_key = self._watcher_index_key(app_id)
            ids: set[str] = self._backend.get(idx_key, set())
            ids.discard(watcher_id)
            if ids:
                self._backend.set(idx_key, ids)
            else:
                self._backend.delete(idx_key)
        return existed

    def list_watchers(self, app_id: str) -> list[PersistedWatcher]:
        """List all persisted watchers for an app. Cleans stale entries."""
        idx_key = self._watcher_index_key(app_id)
        ids: set[str] = self._backend.get(idx_key, set())
        result: list[PersistedWatcher] = []
        stale: list[str] = []
        for wid in ids:
            data = self._backend.get(self._watcher_key(app_id, wid))
            if data is not None:
                result.append(PersistedWatcher.from_dict(data))
            else:
                stale.append(wid)
        if stale:
            ids -= set(stale)
            if ids:
                self._backend.set(idx_key, ids)
            else:
                self._backend.delete(idx_key)
        return result

    def delete_watchers_for_app(self, app_id: str) -> int:
        """Delete all persisted watchers for an app. Returns count deleted."""
        idx_key = self._watcher_index_key(app_id)
        ids: set[str] = self._backend.get(idx_key, set())
        self._backend.delete(idx_key)
        count = 0
        for wid in ids:
            if self._backend.delete(self._watcher_key(app_id, wid)):
                count += 1
        return count


    @staticmethod
    def _notif_key(app_id: str) -> str:
        return f"notif_buf:{app_id}"

    def buffer_notification(self, app_id: str, payload: dict[str, Any]) -> None:
        """Append a notification to the app's buffer (FIFO, capped)."""
        key = self._notif_key(app_id)
        buf: list[dict[str, Any]] = self._backend.get(key, [])
        payload.setdefault("buffered_at", time.time())
        buf.append(payload)
        if len(buf) > self._buffer_max:
            buf = buf[-self._buffer_max :]
        self._backend.set(key, buf, expire=self._buffer_ttl)

    def drain_buffered(self, app_id: str) -> list[dict[str, Any]]:
        """Pop all buffered notifications for an app. Returns empty list if none."""
        key = self._notif_key(app_id)
        buf: list[dict[str, Any]] = self._backend.get(key, [])
        if buf:
            self._backend.delete(key)
        return buf

    def buffered_count(self, app_id: str) -> int:
        """Return number of buffered notifications for an app."""
        buf: list[dict[str, Any]] = self._backend.get(self._notif_key(app_id), [])
        return len(buf)


    _ACTIVE_JOBS_KEY = "__all_active_jobs__"

    @staticmethod
    def _job_key(app_id: str, job_id: str) -> str:
        return f"job:{app_id}:{job_id}"

    @staticmethod
    def _job_index_key(app_id: str) -> str:
        return f"__job_index__{app_id}"

    def put_job(self, job: ScheduledJob) -> None:
        """Store or update a scheduled job."""
        key = self._job_key(job.app_id, job.job_id)
        self._backend.set(key, job.to_dict())
        idx_key = self._job_index_key(job.app_id)
        ids: set[str] = self._backend.get(idx_key, set())
        ids.add(job.job_id)
        self._backend.set(idx_key, ids)
        composite = f"{job.app_id}:{job.job_id}"
        if job.status == "active":
            actives: set[str] = self._backend.get(self._ACTIVE_JOBS_KEY, set())
            actives.add(composite)
            self._backend.set(self._ACTIVE_JOBS_KEY, actives)
        else:
            self._remove_from_active_index(composite)

    def get_job(self, app_id: str, job_id: str) -> ScheduledJob | None:
        """Get a scheduled job. Returns None if missing."""
        data = self._backend.get(self._job_key(app_id, job_id))
        if data is None:
            return None
        return ScheduledJob.from_dict(data)

    def delete_job(self, app_id: str, job_id: str) -> bool:
        """Delete a scheduled job. Returns True if it existed."""
        key = self._job_key(app_id, job_id)
        existed = self._backend.delete(key)
        if existed:
            idx_key = self._job_index_key(app_id)
            ids: set[str] = self._backend.get(idx_key, set())
            ids.discard(job_id)
            if ids:
                self._backend.set(idx_key, ids)
            else:
                self._backend.delete(idx_key)
            self._remove_from_active_index(f"{app_id}:{job_id}")
        return existed

    def list_jobs(
        self, app_id: str, *, status: str | None = None
    ) -> list[ScheduledJob]:
        """List all jobs for an app, optionally filtered by status."""
        idx_key = self._job_index_key(app_id)
        ids: set[str] = self._backend.get(idx_key, set())
        result: list[ScheduledJob] = []
        stale: list[str] = []
        for jid in ids:
            data = self._backend.get(self._job_key(app_id, jid))
            if data is not None:
                job = ScheduledJob.from_dict(data)
                if status is None or job.status == status:
                    result.append(job)
            else:
                stale.append(jid)
        if stale:
            ids -= set(stale)
            if ids:
                self._backend.set(idx_key, ids)
            else:
                self._backend.delete(idx_key)
        return result

    def list_all_active_jobs(self) -> list[ScheduledJob]:
        """List all active jobs across all apps (for scheduler loop).

        Uses the global active index for O(1) lookup of which jobs need
        checking, then loads each job from KV.
        """
        actives: set[str] = self._backend.get(self._ACTIVE_JOBS_KEY, set())
        result: list[ScheduledJob] = []
        stale: list[str] = []
        for composite in actives:
            parts = composite.split(":", 1)
            if len(parts) != 2:
                stale.append(composite)
                continue
            app_id, job_id = parts
            data = self._backend.get(self._job_key(app_id, job_id))
            if data is not None:
                job = ScheduledJob.from_dict(data)
                if job.status == "active":
                    result.append(job)
                else:
                    stale.append(composite)
            else:
                stale.append(composite)
        if stale:
            actives -= set(stale)
            if actives:
                self._backend.set(self._ACTIVE_JOBS_KEY, actives)
            else:
                self._backend.delete(self._ACTIVE_JOBS_KEY)
        return result

    def delete_jobs_for_app(self, app_id: str) -> int:
        """Delete all jobs for an app. Returns count deleted."""
        idx_key = self._job_index_key(app_id)
        ids: set[str] = self._backend.get(idx_key, set())
        self._backend.delete(idx_key)
        count = 0
        for jid in ids:
            if self._backend.delete(self._job_key(app_id, jid)):
                self._remove_from_active_index(f"{app_id}:{jid}")
                count += 1
        return count

    def persist_job_atomic(self, job: ScheduledJob) -> None:
        """Persist job state atomically — single KV write.

        Use this instead of ``put_job()`` when updating an *existing* job
        after it fires.  Unlike ``put_job()``, this skips the secondary
        index bookkeeping (which is already correct for existing jobs)
        and performs a single ``backend.set()`` so the job's updated
        ``next_run_at``, ``run_count``, ``status``, etc. are never
        partially persisted.
        """
        key = self._job_key(job.app_id, job.job_id)
        self._backend.set(key, job.to_dict())
        # If the job completed, remove from active index.
        if job.status != "active":
            self._remove_from_active_index(f"{job.app_id}:{job.job_id}")

    def _remove_from_active_index(self, composite: str) -> None:
        """Remove a composite key from the global active index."""
        actives: set[str] = self._backend.get(self._ACTIVE_JOBS_KEY, set())
        if composite in actives:
            actives.discard(composite)
            if actives:
                self._backend.set(self._ACTIVE_JOBS_KEY, actives)
            else:
                self._backend.delete(self._ACTIVE_JOBS_KEY)

    def close(self) -> None:
        """Close the underlying backend (only if owned)."""
        pass

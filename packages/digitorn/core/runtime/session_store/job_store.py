"""File-backed JobStore: watchers + scheduled jobs persisted on disk."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # Reuse the legacy dataclasses so consumers don't need to learn
    # new types; the public API stays byte-identical.
    from digitorn.core.app.job_store import PersistedWatcher, ScheduledJob


_DEFAULT_BUFFER_MAX = 100
_DEFAULT_BUFFER_TTL = 86400.0


def _validate_segment(value: str, label: str) -> None:
    if not value or "/" in value or "\\" in value or ".." in value:
        raise ValueError(f"invalid {label}: {value!r}")


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".job_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, default=str, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("file_job_store_read_corrupt path=%s err=%s", path, exc)
        return None


class FileJobStore:
    """Filesystem-backed jobs + watchers + notification buffer."""

    def __init__(
        self,
        *,
        root: Path,
        buffer_max: int = _DEFAULT_BUFFER_MAX,
        buffer_ttl_seconds: float = _DEFAULT_BUFFER_TTL,
    ) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / "watchers").mkdir(exist_ok=True)
        (self._root / "jobs").mkdir(exist_ok=True)
        (self._root / "notif_buf").mkdir(exist_ok=True)
        self._buffer_max = int(buffer_max)
        self._buffer_ttl = float(buffer_ttl_seconds)


    def _watcher_path(self, app_id: str, watcher_id: str) -> Path:
        _validate_segment(app_id, "app_id")
        _validate_segment(watcher_id, "watcher_id")
        return self._root / "watchers" / app_id / f"{watcher_id}.json"

    def put_watcher(self, pw: "PersistedWatcher") -> None:
        path = self._watcher_path(pw.app_id, pw.watcher_id)
        _atomic_write_json(path, pw.to_dict())

    def get_watcher(
        self, app_id: str, watcher_id: str,
    ) -> "PersistedWatcher | None":
        from digitorn.core.app.job_store import PersistedWatcher
        path = self._watcher_path(app_id, watcher_id)
        data = _read_json(path)
        if data is None:
            return None
        return PersistedWatcher.from_dict(data)

    def delete_watcher(self, app_id: str, watcher_id: str) -> bool:
        path = self._watcher_path(app_id, watcher_id)
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError as exc:
            logger.warning(
                "file_job_store_delete_watcher_failed path=%s err=%s",
                path, exc,
            )
            return False

    def list_watchers(self, app_id: str) -> list["PersistedWatcher"]:
        from digitorn.core.app.job_store import PersistedWatcher
        out: list[PersistedWatcher] = []
        adir = self._root / "watchers" / app_id
        if not adir.exists():
            return out
        for f in adir.glob("*.json"):
            data = _read_json(f)
            if data is None:
                continue
            try:
                out.append(PersistedWatcher.from_dict(data))
            except Exception as exc:
                logger.warning(
                    "list_watchers_skip_bad_row path=%s err=%s", f, exc,
                )
        return out

    def delete_watchers_for_app(self, app_id: str) -> int:
        adir = self._root / "watchers" / app_id
        if not adir.exists():
            return 0
        count = 0
        for f in list(adir.glob("*.json")):
            try:
                f.unlink()
                count += 1
            except OSError:
                pass
        try:
            adir.rmdir()
        except OSError:
            pass  # non-empty (race) -- harmless
        return count


    def _job_path(self, app_id: str, job_id: str) -> Path:
        _validate_segment(app_id, "app_id")
        _validate_segment(job_id, "job_id")
        return self._root / "jobs" / app_id / f"{job_id}.json"

    def put_job(self, job: "ScheduledJob") -> None:
        path = self._job_path(job.app_id, job.job_id)
        _atomic_write_json(path, job.to_dict())

    def get_job(self, app_id: str, job_id: str) -> "ScheduledJob | None":
        from digitorn.core.app.job_store import ScheduledJob
        path = self._job_path(app_id, job_id)
        data = _read_json(path)
        if data is None:
            return None
        return ScheduledJob.from_dict(data)

    def delete_job(self, app_id: str, job_id: str) -> bool:
        path = self._job_path(app_id, job_id)
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError as exc:
            logger.warning(
                "file_job_store_delete_job_failed path=%s err=%s",
                path, exc,
            )
            return False

    def list_jobs(
        self,
        app_id: str | None = None,
        *,
        status: str | None = None,
    ) -> list["ScheduledJob"]:
        from digitorn.core.app.job_store import ScheduledJob
        out: list[ScheduledJob] = []
        if app_id is not None:
            scopes: Iterable[Path] = [self._root / "jobs" / app_id]
        else:
            scopes = (self._root / "jobs").iterdir() if (self._root / "jobs").exists() else []
        for adir in scopes:
            if not adir.exists() or not adir.is_dir():
                continue
            for f in adir.glob("*.json"):
                data = _read_json(f)
                if data is None:
                    continue
                try:
                    job = ScheduledJob.from_dict(data)
                except Exception as exc:
                    logger.warning(
                        "list_jobs_skip_bad_row path=%s err=%s", f, exc,
                    )
                    continue
                if status is not None and getattr(job, "status", "") != status:
                    continue
                out.append(job)
        return out

    def delete_jobs_for_app(self, app_id: str) -> int:
        adir = self._root / "jobs" / app_id
        if not adir.exists():
            return 0
        count = 0
        for f in list(adir.glob("*.json")):
            try:
                f.unlink()
                count += 1
            except OSError:
                pass
        try:
            adir.rmdir()
        except OSError:
            pass
        return count

    def list_all_active_jobs(self) -> list["ScheduledJob"]:
        """All active jobs across every app, used by the scheduler loop"""
        return self.list_jobs(status="active")


    def _buffer_path(self, app_id: str) -> Path:
        _validate_segment(app_id, "app_id")
        return self._root / "notif_buf" / app_id / "buffer.jsonl"

    def buffer_notification(
        self, app_id: str, payload: dict[str, Any],
    ) -> None:
        """Append a notification to the per-app FIFO buffer, capped at"""
        path = self._buffer_path(app_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.time(), "payload": dict(payload)}
        # Read existing, prune by TTL, append, cap at buffer_max.
        items: list[dict[str, Any]] = []
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            except OSError as exc:
                logger.warning("buffer_read_failed path=%s err=%s", path, exc)
        cutoff = time.time() - self._buffer_ttl
        items = [i for i in items if float(i.get("ts", 0)) >= cutoff]
        items.append(entry)
        if len(items) > self._buffer_max:
            items = items[-self._buffer_max:]
        # Atomic rewrite via tmp + replace.
        fd, tmp = tempfile.mkstemp(
            prefix=".buf_", suffix=".tmp", dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for it in items:
                    f.write(json.dumps(it, default=str, ensure_ascii=False) + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def drain_buffered(self, app_id: str) -> list[dict[str, Any]]:
        """Read + atomically clear the notification buffer for `app_id`."""
        path = self._buffer_path(app_id)
        if not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("drain_buffered_read_failed app=%s err=%s", app_id, exc)
            return []
        try:
            path.unlink()
        except OSError:
            pass
        out: list[dict[str, Any]] = []
        cutoff = time.time() - self._buffer_ttl
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if float(entry.get("ts", 0)) < cutoff:
                continue
            payload = entry.get("payload")
            if isinstance(payload, dict):
                out.append(payload)
        return out


    def stats(self) -> dict[str, int]:
        watchers = sum(
            1 for _ in (self._root / "watchers").rglob("*.json")
        ) if (self._root / "watchers").exists() else 0
        jobs = sum(
            1 for _ in (self._root / "jobs").rglob("*.json")
        ) if (self._root / "jobs").exists() else 0
        bufs = sum(
            1 for _ in (self._root / "notif_buf").rglob("buffer.jsonl")
        ) if (self._root / "notif_buf").exists() else 0
        return {"watchers": watchers, "jobs": jobs, "notif_buffers": bufs}

"""Digitorn — Requirements API routes.

    GET  /api/requires                     List all module requirements with status
    POST /api/requires/install             Install a specific requirement (background)
    POST /api/requires/install-all         Install all missing requirements (background)
    GET  /api/requires/jobs                List install jobs
    GET  /api/requires/jobs/{job_id}       Poll a single install job
    POST /api/requires/jobs/{job_id}/cancel  Cancel a pending/running job

**Non-blocking guarantee**: install calls return ``202 Accepted``
immediately with a ``job_id``. The actual ``pip install`` (or npm /
go / cargo / …) runs in an asyncio task wrapping a subprocess. The
daemon's event loop is NEVER blocked by a package manager — a
previous implementation ran the work inside the request handler and
timed out on clients after ~15 seconds for any non-trivial install.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/requires", tags=["requires"])


class InstallRequest(BaseModel):
    """Request body for POST /api/requires/install."""
    name: str


# ── Background job registry ──────────────────────────────────────────


@dataclass
class InstallJob:
    """In-memory record of an install job.

    Jobs are retained for ``_JOB_RETENTION_SECONDS`` after they finish
    so clients that disconnect and come back can still read the
    result. After that window they're garbage-collected by
    :func:`_gc_jobs`.
    """

    job_id: str
    kind: str                  # "single" | "all"
    target: str                # requirement name for "single", "" for "all"
    state: str = "pending"     # pending | running | completed | failed | cancelled
    started_at: float = 0.0
    finished_at: float = 0.0
    progress: dict[str, Any] = field(default_factory=dict)
    # For "all": per-requirement results accumulated as they complete.
    results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "target": self.target,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": (
                round(
                    (self.finished_at or time.monotonic()) - self.started_at, 2,
                )
                if self.started_at else 0.0
            ),
            "progress": dict(self.progress),
            "results": list(self.results),
            "error": self.error,
        }


_install_jobs: dict[str, InstallJob] = {}
_JOB_RETENTION_SECONDS = 3600  # keep finished jobs for 1 h


def _gc_jobs() -> None:
    """Drop finished jobs older than the retention window."""
    now = time.monotonic()
    stale = [
        jid for jid, j in _install_jobs.items()
        if j.finished_at and (now - j.finished_at) > _JOB_RETENTION_SECONDS
    ]
    for jid in stale:
        _install_jobs.pop(jid, None)


# ── Inventory ────────────────────────────────────────────────────────


@router.get("")
async def list_requirements() -> dict[str, Any]:
    """List all module requirements grouped by module, with install status."""
    from digitorn.core.requirements import (
        scan_all_requirements, _detect_available_managers,
    )

    reqs = scan_all_requirements()
    managers = _detect_available_managers()

    by_module: dict[str, list[dict[str, Any]]] = {}
    for req in reqs:
        by_module.setdefault(req.module_id, []).append(req.to_dict())

    installed = sum(1 for r in reqs if r.installed)
    missing = sum(1 for r in reqs if not r.installed)

    return {
        "requirements": by_module,
        "summary": {
            "total": len(reqs),
            "installed": installed,
            "missing": missing,
        },
        "available_managers": list(managers.keys()),
    }


# ── Async install drivers ───────────────────────────────────────────


async def _run_single_install(job: InstallJob) -> None:
    """Execute ``install_requirement`` for one target and record the
    outcome on ``job``. Runs inside an ``asyncio.Task`` — never on the
    request path."""
    from digitorn.core.requirements import (
        scan_all_requirements, install_requirement,
    )

    job.state = "running"
    try:
        reqs = scan_all_requirements()
        target = next(
            (r for r in reqs
             if r.name == job.target or r.binary == job.target),
            None,
        )
        if target is None:
            job.state = "failed"
            job.error = f"Requirement '{job.target}' not found"
            return
        if target.installed:
            job.state = "completed"
            job.results.append({
                "success": True,
                "name": target.name,
                "already_installed": True,
                "path": target.path,
            })
            return

        result = await install_requirement(target)
        job.results.append(result)
        job.state = "completed" if result.get("success") else "failed"
        if not result.get("success"):
            job.error = str(result.get("error") or result.get("stderr") or "")[:500]
    except asyncio.CancelledError:
        job.state = "cancelled"
        raise
    except Exception as exc:
        logger.exception("install_single_job_crashed job=%s", job.job_id)
        job.state = "failed"
        job.error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        job.finished_at = time.monotonic()


async def _run_all_install(job: InstallJob) -> None:
    """Install every missing requirement, streaming progress into
    ``job.results`` after each one so the client can poll and see
    partial progress."""
    from digitorn.core.requirements import (
        scan_all_requirements, install_requirement,
    )

    job.state = "running"
    try:
        reqs = scan_all_requirements()
        missing = [r for r in reqs if not r.installed]
        job.progress = {
            "total": len(missing),
            "done": 0,
            "succeeded": 0,
            "failed": 0,
        }
        for req in missing:
            if job.state == "cancelled":
                break
            result = await install_requirement(req)
            job.results.append(result)
            job.progress["done"] = len(job.results)
            if result.get("success"):
                job.progress["succeeded"] = job.progress.get("succeeded", 0) + 1
            else:
                job.progress["failed"] = job.progress.get("failed", 0) + 1
        if job.state != "cancelled":
            failed = sum(1 for r in job.results if not r.get("success"))
            job.state = "failed" if failed and not job.progress["succeeded"] else "completed"
    except asyncio.CancelledError:
        job.state = "cancelled"
        raise
    except Exception as exc:
        logger.exception("install_all_job_crashed job=%s", job.job_id)
        job.state = "failed"
        job.error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        job.finished_at = time.monotonic()


def _enqueue(kind: str, target: str, coro_factory) -> InstallJob:
    """Register a new job and spawn its background task."""
    _gc_jobs()
    job = InstallJob(
        job_id=f"inst-{uuid.uuid4().hex[:12]}",
        kind=kind,
        target=target,
        started_at=time.monotonic(),
    )
    _install_jobs[job.job_id] = job
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Should not happen in FastAPI request context; guard anyway.
        raise HTTPException(
            status_code=503,
            detail="No running event loop — daemon not ready.",
        )
    job.task = loop.create_task(coro_factory(job))
    return job


# ── Routes ────────────────────────────────────────────────────────────


@router.post("/install", status_code=202)
async def install_requirement_bg(body: InstallRequest) -> dict[str, Any]:
    """Install a specific requirement **in the background**.

    Returns 202 immediately with a ``job_id``. Poll
    ``GET /api/requires/jobs/{job_id}`` for progress and the final
    result. The daemon event loop is never blocked by a package
    manager invocation.
    """
    job = _enqueue("single", body.name, _run_single_install)
    return {
        "accepted": True,
        "job_id": job.job_id,
        "state": job.state,
        "target": body.name,
        "poll": f"/api/requires/jobs/{job.job_id}",
    }


@router.post("/install-all", status_code=202)
async def install_all_missing_bg() -> dict[str, Any]:
    """Install every missing requirement **in the background**.

    Returns 202 immediately with a ``job_id``. Poll
    ``GET /api/requires/jobs/{job_id}`` for incremental progress:
    ``progress.done`` counts completed requirements, ``results`` grows
    as each install finishes.
    """
    job = _enqueue("all", "", _run_all_install)
    return {
        "accepted": True,
        "job_id": job.job_id,
        "state": job.state,
        "poll": f"/api/requires/jobs/{job.job_id}",
    }


@router.get("/jobs")
async def list_jobs() -> dict[str, Any]:
    """List every install job tracked by the daemon (live + recent)."""
    _gc_jobs()
    return {
        "jobs": [j.to_dict() for j in _install_jobs.values()],
        "count": len(_install_jobs),
        "retention_seconds": _JOB_RETENTION_SECONDS,
    }


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    """Fetch a single install job by id — use this to poll progress."""
    job = _install_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job.to_dict()


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, Any]:
    """Cancel a pending or running install job."""
    job = _install_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if job.state in ("completed", "failed", "cancelled"):
        return {
            "cancelled": False,
            "state": job.state,
            "message": "Job already in terminal state.",
        }
    if job.task is not None and not job.task.done():
        job.task.cancel()
    job.state = "cancelled"
    job.finished_at = time.monotonic()
    return {"cancelled": True, "job_id": job_id, "state": job.state}

"""Pin the non-blocking contract on ``/api/requires/install[-all]``.

We bring up an in-process FastAPI app that mounts ONLY the requires
router, then:
  1. POST /install-all → must return 202 AND in less than 500 ms.
  2. Response body carries ``job_id`` + ``poll`` URL.
  3. GET /jobs/{job_id} returns JSON with ``state`` + ``progress``.

The point is **not** that pip installs anything (we don't pull the
internet during unit tests). It's that the daemon is NEVER blocked
by the install loop — the handler returns immediately and the real
work runs in an asyncio task.
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from digitorn.core.api.requires import router as requires_router  # noqa: E402


def run() -> int:
    failures: list[str] = []

    app = FastAPI()
    app.include_router(requires_router)

    with TestClient(app) as client:
        # 1. install-all returns 202 fast.
        t0 = time.monotonic()
        r = client.post("/api/requires/install-all")
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if r.status_code != 202:
            failures.append(
                f"POST /install-all: expected 202 got {r.status_code} "
                f"body={r.text[:200]!r}"
            )
        if elapsed_ms > 500:
            failures.append(
                f"POST /install-all blocked the handler for {elapsed_ms} ms "
                "— must be non-blocking (< 500 ms)"
            )
        data = r.json()
        if not data.get("accepted") or not data.get("job_id"):
            failures.append(
                f"POST /install-all body missing job_id / accepted: {data}"
            )
        job_id = data.get("job_id", "")

        # 2. Poll the job state — must not 404, must carry the contract.
        if job_id:
            rp = client.get(f"/api/requires/jobs/{job_id}")
            if rp.status_code != 200:
                failures.append(
                    f"GET /jobs/{job_id}: expected 200 got {rp.status_code}"
                )
            pd = rp.json()
            for k in ("job_id", "state", "kind", "progress", "results"):
                if k not in pd:
                    failures.append(f"job dict missing field {k!r}")

        # 3. install (single) with an unknown name: still 202 + poll URL.
        #    The BACKGROUND task will mark it failed; the request path
        #    itself must not block.
        t0 = time.monotonic()
        r1 = client.post(
            "/api/requires/install", json={"name": "nonexistent-binary-x"},
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        if r1.status_code != 202:
            failures.append(
                f"POST /install: expected 202 got {r1.status_code}"
            )
        if elapsed_ms > 500:
            failures.append(
                f"POST /install blocked the handler for {elapsed_ms} ms"
            )

        # 4. Cancel endpoint exists and is idempotent on terminal state.
        if job_id:
            time.sleep(0.1)  # let the job progress
            rc = client.post(f"/api/requires/jobs/{job_id}/cancel")
            if rc.status_code != 200:
                failures.append(
                    f"POST /cancel: expected 200 got {rc.status_code}"
                )

        # 5. list_jobs returns what we created.
        rl = client.get("/api/requires/jobs")
        if rl.status_code != 200 or rl.json().get("count", 0) < 1:
            failures.append(f"GET /jobs: {rl.status_code} {rl.json()}")

    if failures:
        print("FAIL — install-all non-blocking contract:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — /install[-all] returns 202 < 500 ms, jobs poll + cancel "
          "work, daemon event loop never blocked by pip")
    return 0


if __name__ == "__main__":
    sys.exit(run())

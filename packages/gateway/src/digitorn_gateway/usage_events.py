"""Best-effort writer for ``gateway_usage_events``.

One row per LLM dispatch the gateway processes. Partitioned monthly
in Postgres (the daemon's migration created the partitions + a helper
function ``gateway_create_usage_partition(target_month)``).

Why a separate writer (not inside ``quota.QuotaEngine.record``):

  * Quota is about *gating* future requests; usage events are about
    *observability* for the dashboard. Decoupling them lets us turn
    quota OFF (e.g., for an internal user) without losing telemetry.
  * The quota engine batches writes for amortised cost; usage events
    write per-call so the dashboard's "live spend" panel is accurate.
  * On dispatch errors we still want a usage row (with
    ``error_class``) - quota currently skips error paths.

All writes go through asyncpg directly with the existing engine. We
do NOT use the SQLAlchemy ORM for this hot path - the `INSERT INTO
gateway_usage_events` is one statement and bypassing the ORM saves
~1ms per call. Cost is materialised at write time by a Postgres
trigger from ``cost_breakdown JSONB``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from digitorn_gateway.db import get_session_factory

logger = logging.getLogger(__name__)


async def record_event(
    *,
    user_id: str,
    provider: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    latency_ms: float | None = None,
    cost_usd: float = 0.0,
    cost_breakdown: dict[str, Any] | None = None,
    error_class: str | None = None,
    run_id: str | None = None,
    agent_id: str | None = None,
    app_id: str | None = None,
    external_sid: str | None = None,
    kind: str = "completion",
    served_by: str | None = None,
    attempts: int = 1,
    failover_trail: list[str] | None = None,
    truncated_dropped: int = 0,
    cache_hit: bool = False,
) -> None:
    """Insert one row into ``gateway_usage_events``.

    ``cost_breakdown`` is the canonical form
    (``{provider: {input_usd, output_usd, total_usd}}``); the
    Postgres trigger will materialise ``total_cost_usd`` from it on
    insert. If ``cost_breakdown`` is ``None`` we synthesise it from
    ``cost_usd`` so the trigger has something to sum.

    The 5 attribution IDs (``user_id``, ``app_id``, ``external_sid``,
    ``run_id``, ``agent_id``) come from the ``X-Digitorn-*`` headers
    forwarded by the daemon. Any subset can be ``None``; the
    corresponding column is then ``NULL``. ``user_id`` is the only
    one strictly required (verified from the JWT, not the header).

    Best-effort: every error is logged and swallowed. The caller
    must NOT rely on this function to surface DB problems - quota
    enforcement is a separate code path.
    """
    if not user_id:
        return
    if cost_breakdown is None:
        cost_breakdown = {
            provider or "unknown": {
                "total_usd": float(cost_usd or 0.0),
            }
        }
    try:
        factory = get_session_factory()
        async with factory() as db:
            await db.execute(
                text("""
                    INSERT INTO gateway_usage_events (
                        user_id, run_id, agent_id, app_id, external_sid,
                        provider, model, kind,
                        prompt_tokens, completion_tokens,
                        cache_read_tokens, cache_write_tokens,
                        latency_ms, cost_breakdown, error_class,
                        served_by, attempts, failover_trail,
                        truncated_dropped, cache_hit
                    ) VALUES (
                        :user_id, :run_id, :agent_id, :app_id, :external_sid,
                        :provider, :model, :kind,
                        :prompt_tokens, :completion_tokens,
                        :cache_read_tokens, :cache_write_tokens,
                        :latency_ms, CAST(:cost_breakdown AS JSONB), :error_class,
                        :served_by, :attempts, CAST(:failover_trail AS JSONB),
                        :truncated_dropped, :cache_hit
                    )
                """),
                {
                    "user_id": user_id,
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "app_id": app_id,
                    "external_sid": external_sid,
                    "provider": provider or "unknown",
                    "model": model or "unknown",
                    "kind": kind,
                    "prompt_tokens": int(prompt_tokens),
                    "completion_tokens": int(completion_tokens),
                    "cache_read_tokens": int(cache_read_tokens),
                    "cache_write_tokens": int(cache_write_tokens),
                    "latency_ms": int(latency_ms) if latency_ms is not None else None,
                    "cost_breakdown": json.dumps(cost_breakdown),
                    "error_class": error_class,
                    "served_by": served_by,
                    "attempts": max(1, int(attempts or 1)),
                    "failover_trail": (
                        json.dumps(failover_trail) if failover_trail else None
                    ),
                    "truncated_dropped": max(0, int(truncated_dropped or 0)),
                    "cache_hit": bool(cache_hit),
                },
            )
            await db.commit()
    except Exception as exc:
        logger.warning(
            "gateway_usage_events.record failed user=%s model=%s err=%s",
            user_id, model, exc,
        )

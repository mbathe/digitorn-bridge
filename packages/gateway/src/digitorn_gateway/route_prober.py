"""Background route health prober.

Runs every ``ROUTE_PROBE_INTERVAL_S`` seconds, picks the routes that
the dispatch layer marked unhealthy, and tries a tiny LLM call against
them. On success we clear the unhealthy state in the cache so the
next dispatch retries the route normally.

The probe is a 1-token, 1-message call against the route's credential
- it costs ~$0.00001 per check and confirms (a) the credential still
authenticates, (b) the upstream is reachable, (c) rate limits cleared.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from digitorn_gateway.config_cache import get_cache, ResolvedDispatch

logger = logging.getLogger(__name__)


ROUTE_PROBE_INTERVAL_S = 30.0


async def _probe_one(rd: ResolvedDispatch) -> bool:
    """Tiny LLM call. Returns True on 2xx-equivalent success."""
    import litellm
    from digitorn_gateway.llm_call import _litellm_model_from_compat

    model = _litellm_model_from_compat(
        rd.compat, rd.provider_slug, rd.real_model_id,
    )
    kwargs: dict[str, Any] = {
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    if rd.api_key:
        kwargs["api_key"] = rd.api_key
    if rd.base_url:
        kwargs["api_base"] = rd.base_url
    if rd.extra_headers:
        kwargs["extra_headers"] = dict(rd.extra_headers)
    try:
        await litellm.acompletion(model=model, **kwargs)
        return True
    except Exception as exc:
        logger.info(
            "route_prober: route %s still failing (%s)",
            rd.route_id, exc,
        )
        return False


async def _scan(cache) -> int:
    """Walk every route, probe the unhealthy ones, return number probed."""
    now = time.monotonic()
    snapshot = cache.route_health_snapshot()
    probed = 0
    for route in cache.all_routes():
        h = snapshot.get(route.id)
        if h is None or not h.get("is_blocked"):
            continue  # only re-test ones that are actively blocked
        # Resolve the route at its real index so probe + dispatch share
        # the same auth path.
        all_routes = cache._routes.get(route.model_alias, [])  # type: ignore[attr-defined]
        try:
            idx = all_routes.index(route)
        except ValueError:
            continue
        # Temporarily clear the block so resolve_dispatch_at returns it.
        cache._route_health[route.id].blocked_until = 0.0  # type: ignore[attr-defined]
        rd = cache.resolve_dispatch_at(route.model_alias, idx)
        if rd is None:
            # Couldn't even resolve; restore block.
            cache._route_health[route.id].blocked_until = now + 30  # type: ignore[attr-defined]
            continue
        ok = await _probe_one(rd)
        probed += 1
        if ok:
            cache.mark_route_success(route.id)
            logger.info("route_prober: route %s recovered", route.id)
        else:
            cache.mark_route_failure(
                route.id, "probe_failed", cooldown_s=30.0,
            )
    return probed


_task: asyncio.Task | None = None


async def start_probe_loop(
    interval_s: float = ROUTE_PROBE_INTERVAL_S,
) -> asyncio.Task:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    cache = get_cache()

    async def _loop() -> None:
        while True:
            try:
                await asyncio.sleep(interval_s)
                n = await _scan(cache)
                if n:
                    logger.debug("route_prober: probed %d unhealthy routes", n)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning(
                    "route_prober loop error (continuing): %s", exc,
                )

    _task = asyncio.create_task(_loop(), name="gateway-route-prober")
    return _task


async def stop_probe_loop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
        _task = None

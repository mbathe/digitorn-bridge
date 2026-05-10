"""Plan registry + user→plan resolver.

The QuotaEngine asks the registry "what is user X's effective quota
envelope right now?" and gets back a `QuotaDefinition`. The registry
caches plans + per-user assignments in memory with a short TTL refresh
so the hot path never touches the DB.

Boot sequence (from `main.lifespan`):

1. Read default plans from `config/plans.yaml`.
2. UPSERT them into the DB. Existing rows with the same id are NOT
   overwritten - the DB is the source of truth, the YAML is just a
   bootstrap helper for fresh deployments.
3. Build the in-memory cache.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from digitorn_gateway.models_db import Plan, UserPlan
from digitorn_gateway.quota_schema import QuotaDefinition

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _PlanRecord:
    id: str
    name: str
    description: str | None
    is_default: bool
    quota_def: QuotaDefinition


@dataclass(slots=True)
class _UserPlanRecord:
    user_id: str
    plan_id: str
    override: QuotaDefinition | None
    cached_at: float  # monotonic
    effective: QuotaDefinition  # merged plan + override
    # Extra usage allowance granted on top of ``effective``. Same shape
    # as a QuotaDefinition (metric → window → amount). The supervisor
    # adds these amounts to the plan limit before deciding to block.
    extra_usage: QuotaDefinition | None = None


class PlanRegistry:
    """Process-wide cache of plans + per-user resolutions.

    Plans cache: refreshed on every `reload_plans()`, no TTL because
    plans rarely change and the admin can call `reload_plans` after
    a write.

    User cache: per-entry TTL. After expiry, next `resolve()` re-reads
    from DB. Bound the size to avoid unbounded growth in long-running
    deployments.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        default_plan_name: str,
        user_cache_ttl_seconds: int,
        max_user_cache: int = 50_000,
    ) -> None:
        self._session_factory = session_factory
        self._default_plan_name = default_plan_name
        self._user_cache_ttl = user_cache_ttl_seconds
        self._max_user_cache = max_user_cache

        self._plans: dict[str, _PlanRecord] = {}
        self._default_plan: _PlanRecord | None = None
        self._users: dict[str, _UserPlanRecord] = {}
        self._load_lock = asyncio.Lock()

    # ── Plan-side ──────────────────────────────────────────────

    async def reload_plans(self) -> int:
        """Re-pull every plan from the DB into memory. Returns count."""
        async with self._load_lock:
            async with self._session_factory() as db:
                rows = (await db.execute(select(Plan))).scalars().all()
            new: dict[str, _PlanRecord] = {}
            default: _PlanRecord | None = None
            for r in rows:
                rec = _PlanRecord(
                    id=r.id,
                    name=r.name,
                    description=r.description,
                    is_default=r.is_default,
                    quota_def=QuotaDefinition.model_validate(r.quota_def or {}),
                )
                new[r.id] = rec
                if rec.is_default:
                    default = rec
                if default is None and r.id == self._default_plan_name:
                    default = rec
            self._plans = new
            self._default_plan = default
            # Plan changes invalidate user resolutions.
            self._users.clear()
            logger.info(
                "plans_reloaded count=%d default=%s",
                len(new), default.id if default else None,
            )
            return len(new)

    def get_plan(self, plan_id: str) -> QuotaDefinition | None:
        rec = self._plans.get(plan_id)
        return rec.quota_def if rec else None

    def list_plans(self) -> list[dict[str, Any]]:
        return [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "is_default": p.is_default,
                "quota_def": p.quota_def.model_dump(exclude_none=True),
            }
            for p in self._plans.values()
        ]

    # ── User-side ──────────────────────────────────────────────

    async def resolve(self, user_id: str) -> QuotaDefinition | None:
        """Return the effective QuotaDefinition for a user. Hot path,
        designed to be O(1) after the first resolution per user.
        """
        cached = self._users.get(user_id)
        now = time.monotonic()
        if cached is not None and (now - cached.cached_at) < self._user_cache_ttl:
            return cached.effective

        # Cache miss / expired. Fetch fresh.
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(UserPlan).where(UserPlan.user_id == user_id),
                )
            ).scalar_one_or_none()

        if row is None:
            # User has no explicit assignment - fall back to the
            # default plan. Cache the resolution so the next call
            # is a dict hit.
            if self._default_plan is None:
                return None
            self._record_user(
                user_id=user_id,
                plan_id=self._default_plan.id,
                override=None,
                effective=self._default_plan.quota_def,
            )
            return self._default_plan.quota_def

        plan_rec = self._plans.get(row.plan_id)
        if plan_rec is None:
            # Plan was deleted out from under the user - fall back to
            # default rather than letting the request through with no
            # quota.
            if self._default_plan is None:
                return None
            effective = self._default_plan.quota_def
        else:
            effective = plan_rec.quota_def

        override: QuotaDefinition | None = None
        if row.override_quota_def:
            try:
                override = QuotaDefinition.model_validate(row.override_quota_def)
                effective = _merge_definitions(effective, override)
            except Exception as exc:
                logger.warning(
                    "user_plan_override_invalid user=%s err=%s (ignored)",
                    user_id, exc,
                )

        extra_usage: QuotaDefinition | None = None
        if row.extra_usage_def:
            try:
                extra_usage = QuotaDefinition.model_validate(row.extra_usage_def)
            except Exception as exc:
                logger.warning(
                    "user_plan_extra_usage_invalid user=%s err=%s (ignored)",
                    user_id, exc,
                )

        self._record_user(
            user_id=user_id,
            plan_id=row.plan_id,
            override=override,
            effective=effective,
            extra_usage=extra_usage,
        )
        return effective

    def resolve_extra_usage(self, user_id: str) -> QuotaDefinition | None:
        """Return the cached extra_usage for a user (None when no
        overage configured). The supervisor calls this after a fresh
        ``resolve()`` populated the cache."""
        rec = self._users.get(user_id)
        return rec.extra_usage if rec else None

    def invalidate_user(self, user_id: str) -> None:
        self._users.pop(user_id, None)

    def _record_user(
        self,
        *,
        user_id: str,
        plan_id: str,
        override: QuotaDefinition | None,
        effective: QuotaDefinition,
        extra_usage: QuotaDefinition | None = None,
    ) -> None:
        # Drop the oldest entry if we're at cap. Cheap LRU substitute -
        # the cache is rebuilt on plan reloads anyway.
        if len(self._users) >= self._max_user_cache:
            try:
                first_key = next(iter(self._users))
                self._users.pop(first_key, None)
            except StopIteration:
                pass
        self._users[user_id] = _UserPlanRecord(
            user_id=user_id,
            plan_id=plan_id,
            override=override,
            cached_at=time.monotonic(),
            effective=effective,
            extra_usage=extra_usage,
        )


# ── Plan merge ─────────────────────────────────────────────────────


def _merge_definitions(
    base: QuotaDefinition, override: QuotaDefinition,
) -> QuotaDefinition:
    """Merge an override on top of a plan: every non-None field on the
    override replaces the same field on the base. Per-model overrides
    on both are merged at the model level.
    """
    merged = base.model_dump()
    over = override.model_dump(exclude_none=True)
    # Top-level metric fields + non-time caps.
    for k, v in over.items():
        if k == "models":
            continue
        merged[k] = v
    # models: per-key merge.
    base_models = merged.get("models") or {}
    over_models = over.get("models") or {}
    for model_id, over_model in over_models.items():
        base_model = base_models.get(model_id) or {}
        for k, v in over_model.items():
            if v is not None:
                base_model[k] = v
        base_models[model_id] = base_model
    if base_models:
        merged["models"] = base_models
    return QuotaDefinition.model_validate(merged)


# ── Boot-time seeding ──────────────────────────────────────────────


async def seed_plans_from_yaml(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    yaml_path: str | Path,
) -> int:
    """Read a `plans.yaml` file and INSERT any plans whose ID isn't
    already in the DB. Existing rows are left untouched. Returns the
    number of plans inserted.
    """
    p = Path(yaml_path)
    if not p.exists():
        logger.warning(
            "plans_seed_file_missing path=%s (no plans seeded)",
            p,
        )
        return 0

    import yaml as _yaml
    raw = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    seed = raw.get("plans") or {}

    inserted = 0
    async with session_factory() as db:
        existing = (await db.execute(select(Plan.id))).scalars().all()
        existing_ids = set(existing)

        for plan_id, conf in seed.items():
            if plan_id in existing_ids:
                continue
            try:
                quota_def = QuotaDefinition.model_validate(
                    conf.get("quota_def") or {},
                )
            except Exception as exc:
                logger.warning(
                    "plans_seed_invalid id=%s err=%s (skipped)", plan_id, exc,
                )
                continue
            db.add(Plan(
                id=plan_id,
                name=conf.get("name") or plan_id,
                description=conf.get("description"),
                is_default=bool(conf.get("is_default", False)),
                quota_def=quota_def.model_dump(exclude_none=True),
            ))
            inserted += 1

        if inserted:
            await db.commit()

    logger.info(
        "plans_seeded inserted=%d existing=%d path=%s",
        inserted, len(existing_ids), p,
    )
    return inserted


# ── Process-wide instance ──────────────────────────────────────────


_registry: PlanRegistry | None = None


def init_registry(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    default_plan_name: str,
    user_cache_ttl_seconds: int,
) -> PlanRegistry:
    global _registry
    _registry = PlanRegistry(
        session_factory=session_factory,
        default_plan_name=default_plan_name,
        user_cache_ttl_seconds=user_cache_ttl_seconds,
    )
    return _registry


def get_registry() -> PlanRegistry:
    if _registry is None:
        raise RuntimeError("PlanRegistry not initialised - call init_registry() first")
    return _registry

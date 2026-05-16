"""AppSyncer - persist a CompiledApp directly to the database.

Single source of truth on disk: ``~/.digitorn/apps/<scoped>/`` (the
install dir, written by the InstallFlow). The syncer no longer copies
assets into a separate ``bundle-<hash>/`` directory. Every deploy:

    CompiledApp  ──►  DB rows
       │                ├── Application (yaml_content + yaml_hash + meta)
       │                ├── AppProfile
       │                ├── AppModuleGrant
       │                └── AppModuleConfig

``yaml_content`` is kept on the Application row as a fallback for
content-only deploys whose install dir has been wiped.

Usage::

    syncer = AppSyncer()
    await syncer.sync(compiled_app)
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from sqlalchemy import delete, select

from digitorn.core.app.compiler import CompiledApp
from digitorn.core.models import (
    AppModuleConfig,
    AppModuleGrant,
    AppProfile,
    Application,
)
from digitorn.modules.log import get_logger

log = get_logger(__name__)


def _compute_yaml_hash(compiled: CompiledApp) -> str:
    """Deterministic hash of the compiled app for change detection."""
    import json

    canonical = {
        "app_id": compiled.app_id,
        "meta": {
            "name": compiled.meta.name,
            "version": compiled.meta.version,
            "description": compiled.meta.description,
            "author": compiled.meta.author,
            "tags": sorted(compiled.meta.tags),
        },
        "modules": {},
        "security": (
            {
                "default_policy": compiled.security_profile.default_policy,
                "max_risk_level": compiled.security_profile.max_risk_level,
                "granted_permissions": sorted(compiled.security_profile.granted_permissions),
            }
            if compiled.security_profile is not None
            else {"default_policy": "auto", "max_risk_level": "", "granted_permissions": []}
        ),
    }
    for mid, mc in sorted(compiled.modules.items()):
        canonical["modules"][mid] = {
            "config": mc.config,
            "constraints": mc.constraints,
            "setup_actions": [s.action for s in mc.setup_steps],
        }

    raw = json.dumps(canonical, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class AppSyncer:
    """Sync a CompiledApp to the database.

    Idempotent: when ``yaml_hash`` is unchanged and ``force=False``,
    the sync is a no-op. Otherwise the ``Application`` row is upserted
    along with profile / grants / configs.
    """

    def __init__(self) -> None:
        self._profile_id: str | None = None

    async def sync(
        self,
        compiled: CompiledApp,
        *,
        force: bool = False,
        scope: str = "system",
        owner_user_id: str = "",
    ) -> bool:
        """Upsert all DB records from the compiled app.

        Multi-tenant: keyed by ``(app_id, scope, owner_user_id)``.

        Returns True if anything was written, False if no-op.
        """
        from digitorn.core.database import _session_factory
        if _session_factory is None:
            log.warning("app_sync_skip: database not initialized")
            return False

        # System installs are owner-less by invariant.
        if scope == "system" and owner_user_id:
            log.warning(
                "sync: scope='system' with owner_user_id=%r - coercing to ''",
                owner_user_id,
            )
            owner_user_id = ""

        yaml_content = await asyncio.to_thread(
            self._resolve_yaml_content, compiled,
        )
        metadata_hash = _compute_yaml_hash(compiled)

        async with _session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    select(Application).where(
                        Application.app_id == compiled.app_id,
                        Application.scope == scope,
                        Application.owner_user_id == owner_user_id,
                    )
                )
                app_row = result.scalar_one_or_none()

                if (
                    app_row is not None
                    and app_row.yaml_hash == metadata_hash
                    and not force
                ):
                    log.info(
                        "app_sync_skip: %s already in sync",
                        compiled.app_id,
                    )
                    return False

                yaml_path_str = (
                    compiled.source_path.as_posix()
                    if compiled.source_path else None
                )

                if app_row is None:
                    app_row = Application(
                        app_id=compiled.app_id,
                        scope=scope,
                        owner_user_id=owner_user_id,
                        name=compiled.meta.name,
                        version=compiled.meta.version,
                        description=compiled.meta.description or None,
                        author=compiled.meta.author,
                        tags=list(compiled.meta.tags),
                        yaml_path=yaml_path_str,
                        yaml_content=yaml_content,
                        yaml_hash=metadata_hash,
                    )
                    session.add(app_row)
                    await session.flush()
                else:
                    app_row.name = compiled.meta.name
                    app_row.version = compiled.meta.version
                    app_row.description = compiled.meta.description or None
                    app_row.author = compiled.meta.author
                    app_row.tags = list(compiled.meta.tags)
                    app_row.yaml_path = yaml_path_str
                    app_row.yaml_content = yaml_content
                    app_row.yaml_hash = metadata_hash

                await self._sync_profile(session, compiled)
                await self._sync_module_grants(session, compiled)
                await self._sync_module_configs(session, compiled)

        log.info(
            "app_sync_ok: %s assets=%d modules=%s",
            compiled.app_id,
            len(compiled.collected_assets), compiled.module_ids,
        )
        return True

    @staticmethod
    def _resolve_yaml_content(compiled: CompiledApp) -> str:
        """Return the raw YAML content for fallback storage."""
        if compiled.raw_yaml:
            return compiled.raw_yaml
        if compiled.source_path and compiled.source_path.exists():
            try:
                return compiled.source_path.read_text(encoding="utf-8")
            except Exception:
                log.debug(
                    "failed to read YAML source file at %s",
                    compiled.source_path, exc_info=True,
                )
        return ""

    async def _sync_profile(self, session: Any, compiled: CompiledApp) -> None:
        sp = compiled.security_profile
        if sp is None:
            self._profile_id = None
            return

        result = await session.execute(
            select(AppProfile).where(AppProfile.app_id == compiled.app_id)
        )
        profile = result.scalar_one_or_none()

        if profile is None:
            profile = AppProfile(
                app_id=compiled.app_id,
                is_active=sp.is_active,
                default_policy=sp.default_policy,
                granted_permissions=list(sp.granted_permissions),
                max_risk_level=sp.max_risk_level,
                risk_approval_rules=dict(sp.risk_approval_rules),
                approval_timeout=sp.approval_timeout,
            )
            session.add(profile)
        else:
            profile.is_active = sp.is_active
            profile.default_policy = sp.default_policy
            profile.granted_permissions = list(sp.granted_permissions)
            profile.max_risk_level = sp.max_risk_level
            profile.risk_approval_rules = dict(sp.risk_approval_rules)
            profile.approval_timeout = sp.approval_timeout

        await session.flush()
        self._profile_id = profile.id

    async def _sync_module_grants(self, session: Any, compiled: CompiledApp) -> None:
        if self._profile_id is None or compiled.security_profile is None:
            return

        await session.execute(
            delete(AppModuleGrant).where(
                AppModuleGrant.profile_id == self._profile_id
            )
        )

        for mid, grant in compiled.security_profile.module_grants.items():
            row = AppModuleGrant(
                profile_id=self._profile_id,
                module_id=grant.module_id,
                visibility=grant.visibility,
                default_action_policy=grant.default_action_policy,
                action_overrides=dict(grant.action_overrides),
            )
            session.add(row)

    async def _sync_module_configs(self, session: Any, compiled: CompiledApp) -> None:
        await session.execute(
            delete(AppModuleConfig).where(
                AppModuleConfig.app_id == compiled.app_id
            )
        )

        for mid, mc in compiled.modules.items():
            row = AppModuleConfig(
                app_id=compiled.app_id,
                module_id=mc.module_id,
                config=mc.config,
                constraints=mc.constraints,
            )
            session.add(row)

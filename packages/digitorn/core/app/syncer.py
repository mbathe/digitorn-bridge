"""AppSyncer — persist a CompiledApp as an immutable bundle + DB rows.

Every deploy follows the same pipeline:

    CompiledApp  ──►  AppBundle (disk)  ──►  DB rows
       │                    │                   │
       │                    │                   ├── Application (points at current_bundle)
       │                    │                   ├── AppProfile
       │                    │                   ├── AppModuleGrant
       │                    │                   └── AppModuleConfig
       │                    │
       │                    └── ~/.digitorn/apps/<app_id>/bundle-<hash>/
       │                         ├── app.yaml
       │                         ├── skills/*.md
       │                         └── meta.json
       │
       └── collected_assets  (gathered by the compiler as it reads files)

The bundle is content-addressed by ``bundle_hash``. If an identical
bundle already exists for the same ``app_id``, the sync is a no-op — the
DB rows are not touched and the on-disk bundle stays put. This makes
repeat deploys cheap and deterministic.

Usage::

    syncer = AppSyncer()  # uses the default ~/.digitorn/apps store
    await syncer.sync(compiled_app)
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from digitorn.core.app.bundle_store import BundleDescriptor, BundleStore
from digitorn.core.app.compiler import CompiledApp
from digitorn.core.models import (
    AppBundle,
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
        "security": {
            "default_policy": compiled.security_profile.default_policy,
            "max_risk_level": compiled.security_profile.max_risk_level,
            "granted_permissions": sorted(compiled.security_profile.granted_permissions),
        },
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
    """Sync a CompiledApp to an on-disk bundle + the database.

    Two-level idempotency:

    1. **Bundle hash** — derived from the YAML plus every collected asset
       (skills, agent prompts, …). If a bundle with the same hash already
       exists for this ``app_id``, the sync is a complete no-op: neither
       disk nor DB is touched.
    2. **Metadata hash** — derived from the security profile, modules and
       module configs. If the bundle is new but the metadata matches, the
       DB rows are updated in place to point at the new bundle while
       keeping the existing profile / grants / configs (cheap re-sync).

    Force-mode skips the idempotency checks.
    """

    def __init__(self, bundle_store: BundleStore | None = None) -> None:
        self._bundle_store = bundle_store or BundleStore()

    async def sync(
        self,
        compiled: CompiledApp,
        *,
        force: bool = False,
        scope: str = "system",
        owner_user_id: str = "",
    ) -> bool:
        """Write an AppBundle + upsert all DB records from the compiled app.

        Multi-tenant: the row is keyed by ``(app_id, scope, owner_user_id)``.
        Two users can each install the same app_id alongside a system
        install — the upsert lands on the correct row per scope.

        Args:
            compiled: A validated ``CompiledApp`` with ``collected_assets``
                populated by the compiler.
            force: Re-sync even if the bundle already exists.
            scope: "system" (global install) or "user" (per-user).
            owner_user_id: User owning a ``scope='user'`` install. Must
                be empty when ``scope='system'``.

        Returns:
            True if anything was written, False if the deploy was a no-op.
        """
        # Import at call time — the global is set by init_db() which runs
        # AFTER this module is first imported, so a module-level import
        # would capture a stale ``None`` reference.
        from digitorn.core.database import _session_factory
        if _session_factory is None:
            log.warning("app_sync_skip: database not initialized")
            return False

        # ── Freeze content into a bundle (disk) ────────────────────────
        # This is the single source of truth for reloads, not the source
        # YAML file. If the user later deletes / moves the original, the
        # app keeps working.
        yaml_content = self._resolve_yaml_content(compiled)
        yaml_filename = (
            compiled.source_path.name if compiled.source_path else "app.yaml"
        )
        bundle_hash = BundleStore.compute_hash(yaml_content, compiled.collected_assets)
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

                # Fast path: bundle already active for this app → no-op.
                if (
                    app_row is not None
                    and app_row.current_bundle_id is not None
                    and not force
                ):
                    current_bundle = await session.get(
                        AppBundle, app_row.current_bundle_id,
                    )
                    if (
                        current_bundle is not None
                        and current_bundle.bundle_hash == bundle_hash
                        and app_row.yaml_hash == metadata_hash
                    ):
                        log.info(
                            "app_sync_skip: %s already in sync (bundle=%s)",
                            compiled.app_id, bundle_hash[:12],
                        )
                        return False

                # Slow path: write the bundle to disk (idempotent if the
                # hash already exists — BundleStore.create returns the
                # existing descriptor without rewriting).
                # Scope-aware bundle path: user installs live under
                # `_@<uid>__<app_id>/` so they don't collide with system
                # installs or other users.
                from digitorn.core.app.manager import _scoped_slug
                scoped_key = _scoped_slug(compiled.app_id, scope, owner_user_id)
                descriptor = self._bundle_store.create(
                    app_id=scoped_key,
                    yaml_content=yaml_content,
                    assets=compiled.collected_assets,
                    yaml_filename=yaml_filename,
                )

                bundle_row = await self._upsert_bundle_row(
                    session, compiled.app_id, descriptor,
                    scope=scope, owner_user_id=owner_user_id,
                )

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
                        current_bundle_id=bundle_row.id,
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
                    app_row.current_bundle_id = bundle_row.id

                await self._sync_profile(session, compiled)
                await self._sync_module_grants(session, compiled)
                await self._sync_module_configs(session, compiled)

        log.info(
            "app_sync_ok: %s bundle=%s assets=%d modules=%s",
            compiled.app_id, bundle_hash[:12],
            len(compiled.collected_assets), compiled.module_ids,
        )
        return True

    # ── Bundle helpers ──────────────────────────────────────────────────

    @staticmethod
    def _resolve_yaml_content(compiled: CompiledApp) -> str:
        """Return the raw YAML content to freeze into the bundle.

        The compiler now always populates ``compiled.raw_yaml`` with the
        exact bytes it parsed — whether they came from ``compile_file``
        (read from disk) or ``compile_string`` (already in memory). That
        is the single source of truth for the bundle. We never re-read
        ``source_path`` from disk here because it may no longer exist or
        may have been replaced by the time the sync runs, which would
        silently write an empty bundle and break the next reload.
        """
        if compiled.raw_yaml:
            return compiled.raw_yaml
        # Fallback for legacy compiled apps that predate raw_yaml (unit
        # tests, callers building a CompiledApp by hand). Try the source
        # file as a last resort.
        if compiled.source_path and compiled.source_path.exists():
            try:
                return compiled.source_path.read_text(encoding="utf-8")
            except Exception:
                log.debug(
                    "failed to read YAML source file at %s",
                    compiled.source_path, exc_info=True,
                )
        return ""

    async def _upsert_bundle_row(
        self,
        session: Any,
        app_id: str,
        descriptor: BundleDescriptor,
        *,
        scope: str = "system",
        owner_user_id: str = "",
    ) -> AppBundle:
        """Insert or return the AppBundle row matching the descriptor.

        Bundles are unique on ``(app_id, scope, owner_user_id, bundle_hash)``
        so two users holding the same content get distinct rows and the
        cascade-via-SQL in ``delete_app`` isolates them properly.
        """
        existing = await session.execute(
            select(AppBundle).where(
                AppBundle.app_id == app_id,
                AppBundle.scope == scope,
                AppBundle.owner_user_id == owner_user_id,
                AppBundle.bundle_hash == descriptor.bundle_hash,
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            row.bundle_path = str(descriptor.bundle_path)
            row.asset_count = len(descriptor.asset_paths)
            row.size_bytes = descriptor.size_bytes
            return row

        row = AppBundle(
            app_id=app_id,
            scope=scope,
            owner_user_id=owner_user_id,
            bundle_hash=descriptor.bundle_hash,
            bundle_path=str(descriptor.bundle_path),
            yaml_filename=descriptor.yaml_filename,
            asset_count=len(descriptor.asset_paths),
            size_bytes=descriptor.size_bytes,
        )
        session.add(row)
        await session.flush()
        return row

    async def _sync_profile(self, session: Any, compiled: CompiledApp) -> None:
        """Upsert the AppProfile from the compiled SecurityProfile."""
        sp = compiled.security_profile

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
        """Replace all AppModuleGrant rows from the compiled SecurityProfile."""
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
        """Replace all AppModuleConfig rows from the compiled modules."""
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

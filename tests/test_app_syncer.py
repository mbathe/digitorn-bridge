11
*"""Tests for AppSyncer - YAML → DB synchronisation.

Verifies that a CompiledApp is correctly persisted to the database
and that re-syncing with the same hash is a no-op.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from digitorn.core.app.compiler import CompiledApp, CompiledModuleConfig, CompiledSetupStep
from digitorn.core.app.schema import AppMeta
from digitorn.core.app.syncer import AppSyncer, _compute_yaml_hash
from digitorn.core.database import Base
from digitorn.core.models import AppModuleConfig, AppModuleGrant, AppProfile, Application
from digitorn.core.security import ModuleGrant, SecurityProfile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session():
    """Create an in-memory SQLite database with all tables."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    yield factory

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


def _make_compiled(
    app_id: str = "test-app",
    name: str = "Test App",
    version: str = "1.0",
    modules: dict | None = None,
    security_profile: SecurityProfile | None = None,
    source_path: Path | None = None,
) -> CompiledApp:
    """Build a CompiledApp for testing."""
    meta = AppMeta(
        app_id=app_id,
        name=name,
        version=version,
        description="Test application",
        author="test-author",
        tags=["test", "unit"],
    )
    if modules is None:
        modules = {
            "database": CompiledModuleConfig(
                module_id="database",
                config={"pool_size": 5},
                setup_steps=[
                    CompiledSetupStep(
                        module_id="database",
                        action="connect",
                        resolved_params={"connection_id": "main", "driver": "sqlite"},
                    ),
                ],
                constraints={"allowed_actions": ["fetch_results", "list_tables"]},
            ),
        }
    if security_profile is None:
        security_profile = SecurityProfile(
            app_id=app_id,
            is_active=True,
            default_policy="approve",
            granted_permissions=frozenset({"database:fetch_results"}),
            max_risk_level="medium",
            module_grants={
                "database": ModuleGrant(
                    module_id="database",
                    visibility="full",
                    default_action_policy="approve",
                    action_overrides={"fetch_results": "auto", "execute_query": "block"},
                ),
            },
        )
    return CompiledApp(
        meta=meta,
        modules=modules,
        security_profile=security_profile,
        source_path=source_path,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAppSyncer:

    @pytest.mark.asyncio
    async def test_sync_creates_application(self, db_session, monkeypatch):
        """First sync creates Application row with all YAML fields."""
        monkeypatch.setattr("digitorn.core.app.syncer._session_factory", db_session)

        compiled = _make_compiled(source_path=Path("/app/my-app.yaml"))
        syncer = AppSyncer()
        result = await syncer.sync(compiled)

        assert result is True

        async with db_session() as session:
            app = (await session.execute(
                select(Application).where(Application.app_id == "test-app")
            )).scalar_one()

            assert app.name == "Test App"
            assert app.version == "1.0"
            assert app.author == "test-author"
            assert app.tags == ["test", "unit"]
            assert app.yaml_path == "/app/my-app.yaml"
            assert app.yaml_hash is not None
            assert app.description == "Test application"

    @pytest.mark.asyncio
    async def test_sync_creates_profile(self, db_session, monkeypatch):
        """Sync creates AppProfile with security settings from YAML."""
        monkeypatch.setattr("digitorn.core.app.syncer._session_factory", db_session)

        compiled = _make_compiled()
        syncer = AppSyncer()
        await syncer.sync(compiled)

        async with db_session() as session:
            profile = (await session.execute(
                select(AppProfile).where(AppProfile.app_id == "test-app")
            )).scalar_one()

            assert profile.default_policy == "approve"
            assert profile.max_risk_level == "medium"
            assert profile.is_active is True
            assert "database:fetch_results" in profile.granted_permissions

    @pytest.mark.asyncio
    async def test_sync_creates_module_grants(self, db_session, monkeypatch):
        """Sync creates AppModuleGrant rows from SecurityProfile."""
        monkeypatch.setattr("digitorn.core.app.syncer._session_factory", db_session)

        compiled = _make_compiled()
        syncer = AppSyncer()
        await syncer.sync(compiled)

        async with db_session() as session:
            profile = (await session.execute(
                select(AppProfile).where(AppProfile.app_id == "test-app")
            )).scalar_one()

            grants = (await session.execute(
                select(AppModuleGrant).where(AppModuleGrant.profile_id == profile.id)
            )).scalars().all()

            assert len(grants) == 1
            grant = grants[0]
            assert grant.module_id == "database"
            assert grant.visibility == "full"
            assert grant.action_overrides["fetch_results"] == "auto"
            assert grant.action_overrides["execute_query"] == "block"

    @pytest.mark.asyncio
    async def test_sync_creates_module_configs(self, db_session, monkeypatch):
        """Sync creates AppModuleConfig rows with config and constraints."""
        monkeypatch.setattr("digitorn.core.app.syncer._session_factory", db_session)

        compiled = _make_compiled()
        syncer = AppSyncer()
        await syncer.sync(compiled)

        async with db_session() as session:
            configs = (await session.execute(
                select(AppModuleConfig).where(AppModuleConfig.app_id == "test-app")
            )).scalars().all()

            assert len(configs) == 1
            cfg = configs[0]
            assert cfg.module_id == "database"
            assert cfg.config == {"pool_size": 5}
            assert cfg.constraints == {"allowed_actions": ["fetch_results", "list_tables"]}

    @pytest.mark.asyncio
    async def test_sync_idempotent_same_hash(self, db_session, monkeypatch):
        """Second sync with same compiled app is a no-op (same hash)."""
        monkeypatch.setattr("digitorn.core.app.syncer._session_factory", db_session)

        compiled = _make_compiled()
        syncer = AppSyncer()

        first = await syncer.sync(compiled)
        assert first is True

        second = await syncer.sync(compiled)
        assert second is False  # no-op

    @pytest.mark.asyncio
    async def test_sync_updates_on_change(self, db_session, monkeypatch):
        """Sync updates when the compiled app changes."""
        monkeypatch.setattr("digitorn.core.app.syncer._session_factory", db_session)

        syncer = AppSyncer()

        # First sync
        compiled_v1 = _make_compiled(version="1.0")
        await syncer.sync(compiled_v1)

        # Second sync with changed version
        compiled_v2 = _make_compiled(version="2.0")
        result = await syncer.sync(compiled_v2)
        assert result is True

        async with db_session() as session:
            app = (await session.execute(
                select(Application).where(Application.app_id == "test-app")
            )).scalar_one()
            assert app.version == "2.0"

    @pytest.mark.asyncio
    async def test_sync_force_override(self, db_session, monkeypatch):
        """force=True re-syncs even with same hash."""
        monkeypatch.setattr("digitorn.core.app.syncer._session_factory", db_session)

        compiled = _make_compiled()
        syncer = AppSyncer()

        await syncer.sync(compiled)
        result = await syncer.sync(compiled, force=True)
        assert result is True

    @pytest.mark.asyncio
    async def test_sync_replaces_grants_on_update(self, db_session, monkeypatch):
        """Grants are fully replaced (not appended) on re-sync."""
        monkeypatch.setattr("digitorn.core.app.syncer._session_factory", db_session)

        syncer = AppSyncer()

        # V1: database module
        compiled_v1 = _make_compiled(version="1.0")
        await syncer.sync(compiled_v1)

        # V2: different grants
        sp_v2 = SecurityProfile(
            app_id="test-app",
            module_grants={
                "filesystem": ModuleGrant(
                    module_id="filesystem",
                    action_overrides={"read_file": "auto"},
                ),
            },
        )
        compiled_v2 = _make_compiled(
            version="2.0",
            modules={"filesystem": CompiledModuleConfig(module_id="filesystem")},
            security_profile=sp_v2,
        )
        await syncer.sync(compiled_v2)

        async with db_session() as session:
            profile = (await session.execute(
                select(AppProfile).where(AppProfile.app_id == "test-app")
            )).scalar_one()

            grants = (await session.execute(
                select(AppModuleGrant).where(AppModuleGrant.profile_id == profile.id)
            )).scalars().all()

            # Only filesystem grant, database grant is gone
            assert len(grants) == 1
            assert grants[0].module_id == "filesystem"

    @pytest.mark.asyncio
    async def test_sync_replaces_configs_on_update(self, db_session, monkeypatch):
        """Module configs are fully replaced on re-sync."""
        monkeypatch.setattr("digitorn.core.app.syncer._session_factory", db_session)

        syncer = AppSyncer()

        compiled_v1 = _make_compiled(version="1.0")
        await syncer.sync(compiled_v1)

        compiled_v2 = _make_compiled(
            version="2.0",
            modules={
                "filesystem": CompiledModuleConfig(
                    module_id="filesystem",
                    config={"root": "/data"},
                    constraints={"blocked_actions": ["delete_file"]},
                ),
            },
        )
        await syncer.sync(compiled_v2)

        async with db_session() as session:
            configs = (await session.execute(
                select(AppModuleConfig).where(AppModuleConfig.app_id == "test-app")
            )).scalars().all()

            assert len(configs) == 1
            assert configs[0].module_id == "filesystem"
            assert configs[0].config == {"root": "/data"}

    @pytest.mark.asyncio
    async def test_sync_without_db_returns_false(self, monkeypatch):
        """Sync gracefully returns False if DB not initialized."""
        monkeypatch.setattr("digitorn.core.app.syncer._session_factory", None)

        compiled = _make_compiled()
        syncer = AppSyncer()
        result = await syncer.sync(compiled)
        assert result is False


class TestYamlHash:

    def test_same_input_same_hash(self):
        """Identical compiled apps produce the same hash."""
        a = _make_compiled()
        b = _make_compiled()
        assert _compute_yaml_hash(a) == _compute_yaml_hash(b)

    def test_different_version_different_hash(self):
        """Changing version changes the hash."""
        a = _make_compiled(version="1.0")
        b = _make_compiled(version="2.0")
        assert _compute_yaml_hash(a) != _compute_yaml_hash(b)

    def test_different_modules_different_hash(self):
        """Changing module config changes the hash."""
        a = _make_compiled()
        b = _make_compiled(modules={
            "filesystem": CompiledModuleConfig(module_id="filesystem"),
        })
        assert _compute_yaml_hash(a) != _compute_yaml_hash(b)

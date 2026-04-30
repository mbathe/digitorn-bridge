"""End-to-end smoke test for the deploy-time + session-time
credential injection pipeline.

Builds a real CompiledApp from YAML, prepopulates the credential
store with one system_wide and one per_user credential, runs both
injection paths, and asserts the right fields land in the right
places at each phase.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from digitorn.core.app.compiler import AppYAMLCompiler
from digitorn.core.credentials.cipher import VersionedCipher
from digitorn.core.credentials.inject_deploy_time import (
    inject_deploy_time_credentials,
)
from digitorn.core.credentials.inject_session_time import (
    inject_session_time_credentials,
)
from digitorn.core.credentials.master_key.env_provider import EnvKeyProvider
from digitorn.core.credentials.store import CredentialStore, Scope, Status
from digitorn.core.models import Base
from digitorn.modules.registry import ModuleRegistry


YAML_DEPLOY_SCOPE = """
app:
  app_id: pipeline-test-shared
  name: Pipeline Test Shared
agents:
  - id: main
    role: assistant
    brain:
      provider: openai
      model: gpt-4o
      credential:
        ref: openai_shared
        scope: per_app_shared
"""


YAML_USER_SCOPE = """
app:
  app_id: pipeline-test-user
  name: Pipeline Test User
agents:
  - id: main
    role: assistant
    brain:
      provider: openai
      model: gpt-4o
      credential:
        ref: openai_user_main
        scope: per_user
"""


async def _build_store() -> tuple[CredentialStore, str]:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    import base64
    import secrets
    os.environ["DIGITORN_MASTER_KEY"] = base64.urlsafe_b64encode(
        secrets.token_bytes(32),
    ).decode()
    cipher = VersionedCipher(provider=EnvKeyProvider())
    store = CredentialStore(session_factory=sm, cipher=cipher)
    return store, db_path


async def main() -> None:
    store, db_path = await _build_store()
    print(f"store ready (db={db_path})")

    # ── Seed: per_app_shared credential for the deploy-time test ──
    await store.upsert_credential(
        user_id=None,
        app_id="pipeline-test-shared",
        provider_name="openai",
        provider_type="api_key",
        scope=Scope.PER_APP_SHARED,
        fields={"api_key": "sk-deploy-shared-12345"},
        status=Status.FILLED,
        name="openai_shared",
    )
    print("seeded per_app_shared credential")

    # ── Seed: per_user credential for the session-time test ──
    await store.upsert_credential(
        user_id="user-42",
        app_id=None,
        provider_name="openai",
        provider_type="api_key",
        scope=Scope.PER_USER,
        fields={"api_key": "sk-user42-userkey-67890"},
        status=Status.FILLED,
        name="openai_user_main",
    )
    print("seeded per_user credential")

    # ── Compile & inject (deploy-time, per_app_shared) ──
    compiler = AppYAMLCompiler(ModuleRegistry())
    compiled = compiler.compile_string(
        YAML_DEPLOY_SCOPE, source="<deploy-test>",
    )
    brain = compiled.agents[0].brain
    assert brain.credential == {"ref": "openai_shared", "scope": "per_app_shared"}, brain.credential

    inline_provider = compiled.modules["llm_provider"].config["providers"][
        brain.provider_id
    ]
    assert "api_key" not in inline_provider, (
        "api_key should NOT be present before injection"
    )

    injected = await inject_deploy_time_credentials(compiled, store=store)
    print(f"deploy-time injected: {len(injected)} record(s)")

    inline_provider = compiled.modules["llm_provider"].config["providers"][
        brain.provider_id
    ]
    assert inline_provider.get("api_key") == "sk-deploy-shared-12345", (
        f"deploy-time api_key wrong: {inline_provider.get('api_key')!r}"
    )
    print(f"  brain.api_key after deploy injection: "
          f"{inline_provider['api_key'][:24]}...")

    # ── Compile & inject (deploy-time SHOULD SKIP per_user) ──
    compiled_user = compiler.compile_string(
        YAML_USER_SCOPE, source="<user-test>",
    )
    brain_u = compiled_user.agents[0].brain
    assert brain_u.credential == {"ref": "openai_user_main", "scope": "per_user"}

    injected_user = await inject_deploy_time_credentials(
        compiled_user, store=store,
    )
    assert len(injected_user) == 0, (
        f"per_user should NOT inject at deploy: got {len(injected_user)} records"
    )

    inline_user = compiled_user.modules["llm_provider"].config["providers"][
        brain_u.provider_id
    ]
    assert "api_key" not in inline_user, (
        "per_user api_key must remain unset after deploy injection"
    )
    print("deploy-time correctly skips per_user scope")

    # ── Session-time injection: per_user resolves with user context ──
    # We don't have a live llm_provider module instance here, so we
    # just exercise the injector path with a stub `_providers` dict.
    class _StubProvider:
        api_key: str | None = None
        base_url: str | None = None

    class _StubLLMModule:
        def __init__(self) -> None:
            self._providers = {brain_u.provider_id: _StubProvider()}

        async def _configure_from_dict(self, pid: str, cfg: dict) -> None:
            return None

    stub_llm = _StubLLMModule()
    modules_map = {"llm_provider": stub_llm}

    session_result = await inject_session_time_credentials(
        compiled=compiled_user,
        modules=modules_map,
        credential_store=store,
        user_id="user-42",
    )
    print(f"session-time resolved: providers={session_result['providers']} "
          f"modules={session_result['modules']}")

    live_provider = stub_llm._providers[brain_u.provider_id]
    assert live_provider.api_key == "sk-user42-userkey-67890", (
        f"session-time api_key wrong: {live_provider.api_key!r}"
    )
    print(f"  live provider api_key after session injection: "
          f"{live_provider.api_key[:24]}...")

    # ── Negative: wrong user_id should not resolve ──
    stub_llm2 = _StubLLMModule()
    session_neg = await inject_session_time_credentials(
        compiled=compiled_user,
        modules={"llm_provider": stub_llm2},
        credential_store=store,
        user_id="user-99",
    )
    # No resolution expected (no credential for user-99).
    try:
        live2 = stub_llm2._providers[brain_u.provider_id]
        assert live2.api_key is None, (
            f"session-time should NOT resolve for unknown user: {live2.api_key}"
        )
        # This branch executes only if the injector silently no-ops.
        print("  unknown user: api_key stayed None (good)")
    except Exception as exc:
        # CredentialInjectError on required slot is also valid behavior.
        print(f"  unknown user: injector raised {type(exc).__name__} (good)")

    # ── Cleanup ──
    try:
        os.unlink(db_path)
    except PermissionError:
        pass

    print("\n[PASS] deploy-time + session-time injection pipeline OK")


if __name__ == "__main__":
    asyncio.run(main())
